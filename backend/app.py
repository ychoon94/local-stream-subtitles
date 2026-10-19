import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
import hmac
import logging
import re
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audio import SpeechBuffer
from .config import load_settings
from .engine import SubtitleEngine

logger = logging.getLogger('subtitles')
EXTENSION_ORIGIN = re.compile(r'^chrome-extension://[a-p]{32}$')


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
            if mode == 'context':
                await engine.check_translation()
            await send({'type': 'ready', 'mode': mode,
                        'profile': f'{settings.whisper_model} / {settings.translation_model}'})
            queue = asyncio.Queue(maxsize=1)
            history = deque(maxlen=3)
            buffer = SpeechBuffer()
            sequence = 0

            async def consume():
                nonlocal sequence
                while True:
                    utterance = await queue.get()
                    if time.monotonic() - utterance.started_at > 12:
                        await send({'type': 'status', 'state': 'listening', 'message': 'Skipped delayed audio to stay live.'})
                        continue
                    await send({'type': 'status', 'state': 'processing', 'message': 'Translating locally…'})
                    result = await engine.process(utterance, mode, history)
                    if result and time.monotonic() - utterance.started_at <= 20:
                        sequence += 1
                        await send({**result, 'id': sequence})
                    await send({'type': 'status', 'state': 'listening', 'message': 'Listening for Japanese speech'})

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
                if raw is None:
                    raise ValueError('Audio must use binary PCM16 frames.')
                utterance = buffer.feed(raw)
                if utterance:
                    if queue.full():
                        queue.get_nowait()  # Prefer current speech over an old backlog.
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
