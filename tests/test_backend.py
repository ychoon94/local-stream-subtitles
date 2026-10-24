import asyncio
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import allowed_origin, create_app
from backend.audio import MAX_FRAME_BYTES, SAMPLE_RATE, SpeechBuffer, Utterance, create_speech_buffer
from backend.config import Settings
from backend.engine import SubtitleEngine, Transcription
from backend.observability import clock_ms

ORIGIN = 'chrome-extension://' + 'a' * 32
SETTINGS = Settings(token='unit-test-pairing-token')


def pcm(seconds, amplitude=0.0):
    return (np.ones(int(SAMPLE_RATE * seconds)) * amplitude * 32767).astype('<i2').tobytes()


def test_long_silence_stays_bounded_and_speech_has_preroll_until_pause():
    buffer = SpeechBuffer()
    for _ in range(200):
        assert buffer.feed(pcm(0.1)) is None
    for _ in range(4):
        assert buffer.feed(pcm(0.1, 0.1)) is None
    result = None
    for _ in range(5):
        result = buffer.feed(pcm(0.1)) or result
    assert result is not None
    assert len(result.samples) == SAMPLE_RATE * 1.2


def test_continuous_audio_is_bounded():
    buffer = SpeechBuffer(max_seconds=2)
    results = [buffer.feed(pcm(0.1, 0.1)) for _ in range(100)]
    assert sum(item is not None for item in results) == 5
    assert all(len(item.samples) <= SAMPLE_RATE * 2 for item in results if item)


@pytest.mark.parametrize('raw', [b'', b'1', bytes(MAX_FRAME_BYTES + 2)], ids=['empty', 'odd', 'oversized'])
@pytest.mark.parametrize('mode', ['legacy', 'adaptive_rms'])
def test_invalid_frames_rejected(raw, mode):
    with pytest.raises(ValueError):
        create_speech_buffer(Settings(token='frame-limit-test', segmentation_mode=mode)).feed_many(raw)


@pytest.mark.parametrize('origin', [None, 'null', 'https://www.youtube.com',
                                    'http://localhost:8765', ORIGIN + '.evil.test'])
def test_web_origins_rejected(origin):
    assert not allowed_origin(origin)


class FakeEngine:
    def __init__(self):
        self.model = object()
        self.client = SimpleNamespace(aclose=self.close)
        self.calls = 0

    async def close(self):
        pass

    async def check_translation(self):
        pass

    async def process(self, utterance, mode, history):
        self.calls += 1
        return {'type': 'subtitle', 'original': 'こんにちは', 'translated': 'Hello', 'latency_ms': 100}


def test_http_rejects_webpage_origin_and_bad_host():
    with TestClient(create_app(FakeEngine(), SETTINGS)) as client:
        assert client.get('/health').json()['ok']
        assert client.get('/health', headers={'Origin': 'https://evil.test'}).status_code == 403
        assert client.get('/health', headers={'Host': 'evil.test'}).status_code == 400


def test_unauthorized_socket_never_starts_inference():
    engine = FakeEngine()
    with TestClient(create_app(engine, SETTINGS)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/listen', headers={'Origin': 'https://evil.test'}):
                pass
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'type': 'start', 'token': 'wrong'})
            assert socket.receive_json()['fatal']
    assert engine.calls == 0


def test_pcm_stream_produces_subtitle_and_releases_session():
    app = create_app(FakeEngine(), SETTINGS)
    with TestClient(app) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'type': 'start', 'token': SETTINGS.token, 'mode': 'context'})
            assert socket.receive_json()['type'] == 'ready'
            socket.send_bytes(pcm(0.5, 0.1))
            socket.send_bytes(pcm(0.5))
            assert socket.receive_json()['state'] == 'processing'
            result = socket.receive_json()
            assert result['translated'] == 'Hello'
            assert result['id'] == 1
            assert socket.receive_json()['state'] == 'listening'
        # Joining a new socket must succeed after the old socket has closed.
        for _ in range(50):
            if not app.state.active:
                break
            time.sleep(0.01)
        assert not app.state.active


def test_second_tab_cannot_steal_active_session():
    with TestClient(create_app(FakeEngine(), SETTINGS)) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as first:
            first.send_json({'token': SETTINGS.token})
            assert first.receive_json()['type'] == 'ready'
            with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as second:
                second.send_json({'token': SETTINGS.token})
                assert 'Another tab' in second.receive_json()['message']


def test_authenticated_live_socket_uses_server_adaptive_limits_without_observation():
    class Capturing(FakeEngine):
        async def process(self, utterance, mode, history):
            self.samples = utterance.samples
            assert utterance.trace is None
            return await super().process(utterance, mode, history)

    settings = replace(SETTINGS, segmentation_mode='adaptive_rms', speech_max_seconds=1)
    engine = Capturing()
    with TestClient(create_app(engine, settings)) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            # Client-supplied limits cannot override the trusted local settings.
            socket.send_json({'token': settings.token, 'speech_max_seconds': 10})
            assert socket.receive_json()['type'] == 'ready'
            socket.send_bytes(pcm(1, .1))
            assert socket.receive_json()['state'] == 'processing'
            assert socket.receive_json()['translated'] == 'Hello'
            assert socket.receive_json()['state'] == 'listening'
    assert engine.calls == 1
    assert len(engine.samples) == 31 * 512  # Hard cap at last full 32 ms decision.


def test_multiple_endpoints_in_one_legal_packet_do_not_evict_an_idle_worker():
    class Observed(FakeEngine):
        async def process(self, utterance, mode, history):
            utterance.trace['result_ready_ms'] = clock_ms()
            return await super().process(utterance, mode, history)

    settings = replace(SETTINGS, segmentation_mode='adaptive_rms', speech_max_seconds=1,
                       latency_observability=True)
    engine = Observed()
    with TestClient(create_app(engine, settings)) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'token': settings.token, 'observe': True})
            socket.receive_json()
            for _ in range(6):
                socket.send_bytes(pcm(1, .1))
                socket.send_json({'type': 'metrics_snapshot'})
                while True:
                    message = socket.receive_json()
                    if message['type'] == 'metrics_snapshot':
                        break
            socket.send_json({'type': 'benchmark_end'})
            while True:
                message = socket.receive_json()
                if message['type'] == 'benchmark_complete':
                    break
            # Seven capped segments plus the 16 ms EOF continuation.
            assert message['counters']['segmented'] == 8
            assert message['counters']['emitted'] == 8
            assert message['counters']['dropped_queue'] == 0
    assert engine.calls == 8


def test_inference_error_is_sent_without_waiting_for_next_frame():
    class Failing(FakeEngine):
        async def process(self, *args):
            raise RuntimeError('GPU test failure')
    with TestClient(create_app(Failing(), SETTINGS)) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'token': SETTINGS.token})
            socket.receive_json()
            socket.send_bytes(pcm(0.5, 0.1))
            socket.send_bytes(pcm(0.5))
            socket.receive_json()
            assert socket.receive_json() == {'type': 'error', 'message': 'GPU test failure', 'fatal': True}


def test_native_inference_cancellation_keeps_gpu_lock_until_finished():
    import threading

    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        engine = SubtitleEngine(SETTINGS)

        def transcribe(*args):
            entered.set()
            release.wait(timeout=2)
            return Transcription('', None, 0.0)

        engine.transcribe = transcribe
        job = asyncio.create_task(engine.process(Utterance(np.zeros(100), time.monotonic()), 'context', deque()))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        job.cancel()
        await asyncio.sleep(0.02)
        assert engine.lock.locked()
        job.cancel()
        await asyncio.sleep(0.02)
        assert engine.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not engine.lock.locked()
        await engine.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize('payload,expected', [
    ({'message': {'content': 'Hello'}, 'done': True, 'done_reason': 'stop'}, 'Hello'),
    ({'message': {'content': '<think>reasoning</think>Hello'}, 'done': True}, 'Hello'),
    ({'message': {'content': '<think>unfinished'}, 'done': True}, None),
    ({'message': {'content': '<think>reasoning</think>'}, 'done': True}, None),
    ({'message': {'content': 'Hello, I wanted to'}, 'done_reason': 'length'}, None),
    ({'message': {'content': 'Hello'}, 'done': False}, None),
])
def test_translation_accepts_only_complete_subtitles(payload, expected):
    import httpx

    async def scenario():
        engine = SubtitleEngine(SETTINGS)
        await engine.client.aclose()
        engine.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
            base_url='http://127.0.0.1:11434',
        )
        history = deque(maxlen=3)
        engine.transcribe = lambda *args: Transcription('こんにちは', 'ja', 0.99)
        if expected is None:
            with pytest.raises(RuntimeError):
                await engine.process(Utterance(np.zeros(100), time.monotonic()), 'context', history)
            assert not history
        else:
            result = await engine.process(Utterance(np.zeros(100), time.monotonic()), 'context', history)
            assert result['translated'] == expected
        await engine.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(('mode', 'task'), [
    ('context', 'transcribe'),
    ('direct', 'translate'),
])
def test_whisper_transcription_auto_detects_each_utterance_language(mode, task):
    settings = SETTINGS if mode == 'context' else Settings(token=SETTINGS.token, whisper_model='medium')
    engine = SubtitleEngine(settings)
    calls = []

    class Model:
        def transcribe(self, samples, **kwargs):
            calls.append(kwargs)
            segment = SimpleNamespace(text=' hello', avg_logprob=-0.2)
            info = SimpleNamespace(language='en', language_probability=0.94)
            return iter([segment]), info

    engine.model = Model()
    result = engine.transcribe(np.zeros(100), mode)

    assert result == Transcription('hello', 'en', 0.94)
    assert calls[0]['language'] is None
    assert calls[0]['task'] == task
    asyncio.run(engine.client.aclose())


def test_japanese_transcription_is_translated_and_recorded_with_source_language():
    async def scenario():
        engine = SubtitleEngine(SETTINGS)
        engine.transcribe = lambda *args: Transcription('こんにちは', 'ja', 0.98)
        calls = []

        async def translate(text, history):
            calls.append((text, list(history)))
            return 'Hello.'

        engine.translate = translate
        history = deque(maxlen=3)
        result = await engine.process(Utterance(np.zeros(100), time.monotonic()), 'context', history)

        assert calls == [('こんにちは', [])]
        assert result['original'] == 'こんにちは'
        assert result['translated'] == 'Hello.'
        assert list(history) == [{
            'original': 'こんにちは', 'source_language': 'ja', 'en': 'Hello.'
        }]
        await engine.client.aclose()

    asyncio.run(scenario())


def test_confident_english_passes_through_without_translation_or_duplicate_overlay():
    async def scenario():
        engine = SubtitleEngine(SETTINGS)
        text = 'Good morning, everyone!'
        engine.transcribe = lambda *args: Transcription(text, 'en', 0.8)

        async def unexpected_translation(*args):
            raise AssertionError('Confident English should not call Ollama.')

        engine.translate = unexpected_translation
        history = deque(maxlen=3)
        result = await engine.process(Utterance(np.zeros(100), time.monotonic()), 'context', history)

        assert result['original'] == ''
        assert result['translated'] == text
        assert list(history) == [{
            'original': text, 'source_language': 'en', 'en': text
        }]
        await engine.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize('transcription', [
    Transcription('Good morning, everyone!', 'en', 0.79),
    Transcription('Hello こんにちは', 'en', 0.97),
])
def test_uncertain_or_mixed_english_uses_translator(transcription):
    async def scenario():
        engine = SubtitleEngine(SETTINGS)
        engine.transcribe = lambda *args: transcription
        calls = []

        async def translate(text, history):
            calls.append(text)
            return 'Translated subtitle.'

        engine.translate = translate
        history = deque(maxlen=3)
        result = await engine.process(Utterance(np.zeros(100), time.monotonic()), 'context', history)

        assert calls == [transcription.text]
        assert result['translated'] == 'Translated subtitle.'
        assert list(history) == [{
            'original': transcription.text,
            'source_language': transcription.language,
            'en': 'Translated subtitle.',
        }]
        await engine.client.aclose()

    asyncio.run(scenario())


def test_empty_transcription_does_not_create_subtitle_or_history():
    async def scenario():
        engine = SubtitleEngine(SETTINGS)
        engine.transcribe = lambda *args: Transcription('', 'ja', 0.99)

        async def unexpected_translation(*args):
            raise AssertionError('Empty transcription should not call Ollama.')

        engine.translate = unexpected_translation
        history = deque(maxlen=3)
        result = await engine.process(Utterance(np.zeros(100), time.monotonic()), 'context', history)

        assert result is None
        assert not history
        await engine.client.aclose()

    asyncio.run(scenario())


def test_direct_mode_keeps_single_translation_overlay_and_no_context_history():
    async def scenario():
        engine = SubtitleEngine(SETTINGS)
        engine.transcribe = lambda *args: Transcription('Hello there.', 'en', 0.99)
        history = deque(maxlen=3)
        result = await engine.process(Utterance(np.zeros(100), time.monotonic()), 'direct', history)

        assert result['original'] == ''
        assert result['translated'] == 'Hello there.'
        assert not history
        await engine.client.aclose()

    asyncio.run(scenario())
