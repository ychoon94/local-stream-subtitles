import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
import hmac
import json
import logging
import re
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audio import SpeechBuffer
from .config import load_settings
from .engine import SubtitleEngine
from .observability import SessionMetrics, clock_ms, clock_resolution_ms, trace_durations

logger = logging.getLogger('subtitles')
EXTENSION_ORIGIN = re.compile(r'^chrome-extension://[a-p]{32}$')


def socket_state_json(message):
    text = message.get('text')
    if not isinstance(text, str):
        return None
    try:
        command = json.loads(text)
    except json.JSONDecodeError:
        return None
    return command if isinstance(command, dict) else None


async def wait_for_queue_drain(queue, worker):
    """Drain queued jobs while still surfacing a worker failure promptly."""
    drained = asyncio.create_task(queue.join())
    try:
        done, _ = await asyncio.wait({drained, worker}, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
            raise RuntimeError('Subtitle worker stopped before queued audio was drained.')
        await drained
        if worker.done():
            await worker
    finally:
        if not drained.done():
            drained.cancel()
            with suppress(asyncio.CancelledError):
                await drained


def allowed_origin(origin: str | None) -> bool:
    return bool(origin and EXTENSION_ORIGIN.fullmatch(origin))


def create_app(engine=None, settings=None):
    settings = settings or load_settings()
    engine = engine or SubtitleEngine(settings)

    @asynccontextmanager
    async def lifespan(app):
        if engine.model is None:
            await asyncio.to_thread(engine.load)
            await engine.warmup()
        yield
        await engine.client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    app.state.active = False

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and not allowed_origin(origin):
            return JSONResponse({'error': 'Browser page access is disabled.'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/health')
    async def health():
        return {
            'ok': engine.model is not None, 'active': app.state.active,
            'profile': f'{settings.whisper_model} / {settings.translation_model}',
            'device': settings.device,
            'supported_modes': ['context'] if settings.whisper_model in ('turbo', 'large-v3-turbo') else ['context', 'direct'],
        }

    @app.websocket('/listen')
    async def listen(socket: WebSocket):
        # Both origin and token are required. Never expose the token in a URL,
        # HTTP endpoint, logs, or a message forwarded to the YouTube page.
        if not allowed_origin(socket.headers.get('origin')):
            await socket.close(code=1008)
            return
        await socket.accept()
        owns_session = False
        worker = None
        receive = None
        send_lock = asyncio.Lock()
        metrics = SessionMetrics()
        observing = False
        segment_sequence = 0

        async def send(message):
            async with send_lock:
                await socket.send_json(message)

        try:
            config = await asyncio.wait_for(socket.receive_json(), timeout=5)
            if not isinstance(config, dict):
                raise ValueError('Invalid session configuration.')
            token = config.get('token')
            if not isinstance(token, str) or not hmac.compare_digest(token, settings.token):
                raise ValueError('Pairing failed. Rerun setup.ps1 and reload the extension.')
            mode = config.get('mode', 'context')
            if mode not in ('context', 'direct'):
                raise ValueError('Unknown translation mode.')
            if mode == 'direct' and settings.whisper_model in ('turbo', 'large-v3-turbo'):
                raise ValueError('Use Context mode for the installed turbo speech model.')
            if app.state.active:
                raise ValueError('Another tab is already being translated. Stop it first.')
            app.state.active = owns_session = True
            observing = settings.latency_observability and config.get('observe') is True
            if mode == 'context':
                await engine.check_translation()
            await send({'type': 'ready', 'mode': mode,
                        'profile': f'{settings.whisper_model} / {settings.translation_model}',
                        **({'observability': True} if observing else {})})
            queue = asyncio.Queue(maxsize=1)
            history = deque(maxlen=3)
            buffer = SpeechBuffer(observe=observing)
            sequence = 0

            async def emit_trace(trace, outcome):
                metrics.counters[outcome] += 1
                await send({'type': 'metrics', 'event': metrics.record(trace, outcome),
                            'counters': dict(metrics.counters)})

            async def enqueue(utterance, source='live'):
                nonlocal segment_sequence
                if not utterance:
                    return
                segment_sequence += 1
                trace = utterance.trace
                if trace is not None:
                    trace.update({'segment_id': segment_sequence, 'source': source})
                    trace['enqueued_ms'] = round(clock_ms(), 3)
                if queue.full():
                    dropped = queue.get_nowait()
                    queue.task_done()
                    if observing and dropped.trace is not None:
                        await emit_trace(dropped.trace, 'dropped_queue')
                    await send({'type': 'status', 'state': 'processing', 'message': 'Skipping queued audio to stay live.'})
                queue.put_nowait(utterance)
                if observing:
                    metrics.counters['segmented'] += 1

            async def consume():
                nonlocal sequence
                while True:
                    utterance = await queue.get()
                    try:
                        if time.monotonic() - utterance.started_at > 12:
                            if observing and utterance.trace is not None:
                                utterance.trace['result_ready_ms'] = round(clock_ms(), 3)
                                await emit_trace(utterance.trace, 'skipped_stale')
                            await send({'type': 'status', 'state': 'listening', 'message': 'Skipped delayed audio to stay live.'})
                            continue
                        await send({'type': 'status', 'state': 'processing', 'message': 'Translating locally…'})
                        try:
                            result = await engine.process(utterance, mode, history)
                        except Exception:
                            if observing and utterance.trace is not None:
                                utterance.trace['result_ready_ms'] = round(clock_ms(), 3)
                                await emit_trace(utterance.trace, 'asr_failed')
                            raise
                        trace = utterance.trace
                        if not result and observing and trace is not None:
                            await emit_trace(trace, 'empty_asr')
                        if result and time.monotonic() - utterance.started_at <= 20:
                            sequence += 1
                            if observing and trace is not None:
                                trace['websocket_sent_ms'] = round(clock_ms(), 3)
                                result['telemetry'] = {
                                    'segment_id': trace['segment_id'],
                                    'speech_started_ms': trace['speech_started_ms'],
                                    'result_ready_ms': trace['result_ready_ms'],
                                    'websocket_sent_ms': trace['websocket_sent_ms'],
                                }
                                result['timings'] = trace_durations(trace)
                                await send({**result, 'id': sequence})
                                await emit_trace(trace, 'emitted')
                            else:
                                await send({**result, 'id': sequence})
                        elif result and observing and trace is not None:
                            trace['result_ready_ms'] = round(clock_ms(), 3)
                            await emit_trace(trace, 'skipped_stale')
                        await send({'type': 'status', 'state': 'listening', 'message': 'Listening for Japanese speech'})
                    finally:
                        queue.task_done()

            worker = asyncio.create_task(consume())
            while True:
                receive = asyncio.create_task(socket.receive())
                done, _ = await asyncio.wait({receive, worker}, return_when=asyncio.FIRST_COMPLETED)
                if worker in done:
                    receive.cancel()
                    with suppress(asyncio.CancelledError):
                        await receive
                    await worker  # Propagate inference failures immediately.
                message = await receive
                if message['type'] == 'websocket.disconnect':
                    break
                raw = message.get('bytes')
                if raw is None and observing:
                    command = socket_state_json(message)
                    if command is None:
                        raise ValueError('Audio must use binary PCM16 frames.')
                    if command.get('type') == 'clock_sync':
                        client_sent = command.get('client_sent_ms')
                        if isinstance(client_sent, (int, float)) and not isinstance(client_sent, bool) and 0 <= client_sent <= 1e15:
                            server_received = round(clock_ms(), 3)
                            response = {'type': 'clock_sync', 'client_sent_ms': client_sent,
                                        'server_received_ms': server_received,
                                        'server_sent_ms': round(clock_ms(), 3),
                                        'clock_domain': 'server_perf_counter',
                                        'clock_resolution_ms': clock_resolution_ms()}
                            await send(response)
                        continue
                    if command.get('type') == 'audio_dropped':
                        metrics.increment_dropped_audio(command.get('frames'), command.get('samples'))
                        continue
                    if command.get('type') == 'metrics_snapshot':
                        await send({'type': 'metrics_snapshot', **metrics.payload()})
                        continue
                    if command.get('type') == 'rendered':
                        segment_id = command.get('segment_id')
                        trace = metrics.events.get(segment_id) if isinstance(segment_id, int) else None
                        sent_at = trace['timestamps'].get('websocket_sent_ms') if trace else None
                        event = metrics.rendered(command, sent_at) if sent_at is not None else None
                        if event:
                            await send({'type': 'metrics', 'event': event,
                                        'counters': dict(metrics.counters)})
                        continue
                    if command.get('type') == 'benchmark_end':
                        tail = buffer.flush()
                        if tail and tail.trace is not None:
                            await enqueue(tail, 'source_eof')
                        await wait_for_queue_drain(queue, worker)
                        await send({'type': 'benchmark_complete', **metrics.payload()})
                        continue
                    continue
                if raw is None:
                    raise ValueError('Audio must use binary PCM16 frames.')
                utterance = buffer.feed(raw)
                if utterance:
                    if observing:
                        await enqueue(utterance)
                    else:
                        if queue.full():
                            queue.get_nowait()
                            queue.task_done()
                            await send({'type': 'status', 'state': 'processing', 'message': 'Skipping queued audio to stay live.'})
                        queue.put_nowait(utterance)
        except WebSocketDisconnect:
            pass
        except Exception as error:
            # Avoid printing full request/exception objects containing user text.
            logger.warning('Session failed (%s)', type(error).__name__)
            if isinstance(error, (ValueError, RuntimeError)):
                message = str(error)
            elif isinstance(error, (ConnectionError, OSError)) or 'httpx' in type(error).__module__:
                message = 'Local translation unavailable. Check that Ollama is running, then retry.'
            else:
                message = 'Local inference failed. Check the service log and available GPU memory, then retry.'
            with suppress(Exception):
                await send({'type': 'error', 'message': message, 'fatal': True})
        finally:
            if receive and not receive.done():
                receive.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await receive
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await worker
            if owns_session:
                app.state.active = False
            with suppress(Exception):
                await socket.close()

    return app
