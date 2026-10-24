import asyncio
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
import threading

import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app, wait_for_queue_drain
from backend.audio import SAMPLE_RATE, SpeechBuffer, Utterance
from backend.config import Settings
from backend.engine import SubtitleEngine, Transcription
from backend.observability import SessionMetrics, clock_ms, trace_durations

ORIGIN = 'chrome-extension://' + 'a' * 32
SETTINGS = Settings(token='unit-test-pairing-token', latency_observability=True)


def pcm(seconds, amplitude=0.0):
    return (np.ones(int(SAMPLE_RATE * seconds)) * amplitude * 32767).astype('<i2').tobytes()


def test_speech_timestamps_and_stage_durations_use_one_high_resolution_clock_domain(monkeypatch):
    import backend.audio as audio
    import backend.observability as observability

    runtime_clock = SimpleNamespace(monotonic=lambda: 10.0)
    trace_clock = SimpleNamespace(perf_counter=lambda: 100.0)
    monkeypatch.setattr(audio, 'time', runtime_clock)
    monkeypatch.setattr(observability, 'time', trace_clock)
    buffer = SpeechBuffer(observe=True)
    buffer.feed(pcm(0.128))
    buffer.feed(pcm(0.128, 0.1))
    buffer.feed(pcm(0.128, 0.1))
    assert buffer.started_at == 9.872
    assert buffer.trace['speech_started_ms'] == 99872.0
    assert buffer.trace['speech_detected_ms'] == 100000.0

    runtime_clock.monotonic = lambda: 10.512
    trace_clock.perf_counter = lambda: 100.512
    result = None
    for _ in range(4):
        result = buffer.feed(pcm(0.128)) or result
    assert result is not None
    assert result.trace['segmented_ms'] == 100512.0
    result.trace.update({
        'enqueued_ms': 100513.0,
        'dequeued_ms': 100517.0,
        'inference_started_ms': 100519.0,
        'asr_finished_ms': 100600.0,
        'translation_started_ms': 100601.0,
        'translation_finished_ms': 100621.0,
        'result_ready_ms': 100622.0,
    })
    assert trace_durations(result.trace) == {
        'speech_to_segment_ms': 640.0,
        'queue_ms': 4.0,
        'lock_ms': 2.0,
        'asr_ms': 81.0,
        'translation_ms': 20.0,
        'server_total_ms': 750.0,
    }


def test_render_timing_rejects_unordered_or_unbounded_clock_values():
    metrics = SessionMetrics()
    trace = {
        'segment_id': 1,
        'speech_started_ms': 1000.0,
        'result_ready_ms': 1100.0,
        'websocket_sent_ms': 1101.0,
    }
    metrics.record(trace, 'emitted')
    invalid = {
        'segment_id': 1,
        'browser_received_server_ms': 1102,
        'first_readable_server_ms': 1101,
        'final_server_ms': 1103,
        'clock_uncertainty_ms': 1,
    }
    assert metrics.rendered(invalid, 1101) is None
    assert metrics.rendered({**invalid, 'first_readable_server_ms': 1104,
                             'final_server_ms': 700_000}, 1101) is None
    assert metrics.events[1]['outcome'] == 'emitted'
    timings = metrics.rendered({**invalid, 'first_readable_server_ms': 1104,
                                'final_server_ms': 1104,
                                'render_method': 'fully_visible_animation_frame'}, 1101)['timings']
    assert {key: timings[key] for key in ('delivery_ms', 'display_ms', 'first_readable_ms', 'final_ms')} == {
        'delivery_ms': 1,
        'display_ms': 2,
        'first_readable_ms': 104,
        'final_ms': 104,
    }
    assert metrics.events[1]['render_method'] == 'fully_visible_animation_frame'
    assert metrics.rendered({**invalid, 'browser_received_server_ms': 1105,
                             'first_readable_server_ms': 1106, 'final_server_ms': 1106}, 1101) is None
    assert metrics.events[1]['timestamps']['browser_received_ms'] == 1102


def test_clock_uncertainty_bounds_cross_process_delivery_and_subtitle_latency():
    metrics = SessionMetrics()
    metrics.record({
        'segment_id': 1,
        'speech_started_ms': 900.0,
        'result_ready_ms': 999.0,
        'websocket_sent_ms': 1000.0,
    }, 'emitted')

    rendered = metrics.rendered({
        'segment_id': 1,
        'browser_received_server_ms': 996.0,
        'first_readable_server_ms': 1195.0,
        'final_server_ms': 1195.0,
        'clock_uncertainty_ms': 5.0,
        'render_method': 'fully_visible_animation_frame',
    }, 1000.0)
    assert rendered is not None
    assert rendered['timings']['delivery_ms'] == 0.0
    assert rendered['timings']['delivery_lower_ms'] == 0.0
    assert rendered['timings']['delivery_upper_ms'] == 1.0
    assert rendered['timings']['first_readable_ms'] == 295.0
    assert rendered['timings']['first_readable_lower_ms'] == 290.0
    assert rendered['timings']['first_readable_upper_ms'] == 300.0
    assert rendered['timings']['clock_uncertainty_ms'] == 5.0

    too_early = SessionMetrics()
    too_early.record({
        'segment_id': 2,
        'speech_started_ms': 1003.0,
        'result_ready_ms': 1000.0,
        'websocket_sent_ms': 1000.0,
    }, 'emitted')
    assert too_early.rendered({
        'segment_id': 2,
        'browser_received_server_ms': 996.0,
        'first_readable_server_ms': 997.0,
        'final_server_ms': 997.0,
        'clock_uncertainty_ms': 5.0,
    }, 1000.0) is None


class ObservedEngine:
    model = object()

    class Client:
        async def aclose(self):
            pass

    client = Client()

    async def check_translation(self):
        pass

    def finish_trace(self, utterance):
        now = clock_ms()
        if utterance.trace is not None:
            utterance.trace.update({
                'dequeued_ms': now,
                'inference_started_ms': now + 1,
                'asr_finished_ms': now + 3,
                'translation_started_ms': now + 3,
                'translation_finished_ms': now + 5,
                'result_ready_ms': now + 6,
            })

    async def process(self, utterance, mode, history):
        self.finish_trace(utterance)
        return {'type': 'subtitle', 'original': 'こんにちは', 'translated': 'Hello'}


def test_authenticated_opt_in_reports_stages_clock_render_and_drops():
    app = create_app(ObservedEngine(), SETTINGS)
    with TestClient(app) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'token': SETTINGS.token, 'mode': 'context', 'observe': True})
            assert socket.receive_json()['observability'] is True

            socket.send_json({'type': 'clock_sync', 'client_sent_ms': 123.0})
            clock = socket.receive_json()
            assert clock['client_sent_ms'] == 123.0
            assert clock['server_sent_ms'] >= clock['server_received_ms']

            socket.send_bytes(pcm(0.5, 0.1))
            socket.send_bytes(pcm(0.5))
            assert socket.receive_json()['state'] == 'processing'
            subtitle = socket.receive_json()
            assert subtitle['translated'] == 'Hello'
            assert subtitle['telemetry']['segment_id'] == 1
            assert subtitle['timings']['asr_ms'] == 2
            assert socket.receive_json()['type'] == 'metrics'
            assert socket.receive_json()['state'] == 'listening'

            sent = subtitle['telemetry']['websocket_sent_ms']
            socket.send_json({
                'type': 'rendered',
                'segment_id': 1,
                'browser_received_server_ms': sent + 1,
                'first_readable_server_ms': sent + 30,
                'final_server_ms': sent + 30,
                'clock_uncertainty_ms': 2,
                'render_method': 'transition_end',
            })
            rendered = socket.receive_json()['event']
            assert rendered['outcome'] == 'rendered'
            assert rendered['timings']['first_readable_ms'] >= rendered['timings']['server_total_ms']

            socket.send_json({'type': 'audio_dropped', 'frames': 3, 'samples': 6144})
            socket.send_json({'type': 'audio_dropped', 'frames': 0, 'samples': 1})
            socket.send_json({'type': 'metrics_snapshot'})
            snapshot = socket.receive_json()
            assert snapshot['counters']['segmented'] == 1
            assert snapshot['counters']['emitted'] == 1
            assert snapshot['counters']['dropped_audio_frames'] == 3
            assert snapshot['counters']['dropped_audio_samples'] == 6144

            socket.send_json({'type': 'benchmark_end'})
            complete = socket.receive_json()
            assert complete['type'] == 'benchmark_complete'
            assert complete['events'][0]['outcome'] == 'rendered'


def test_observability_requires_both_server_setting_and_start_opt_in():
    settings = replace(SETTINGS, latency_observability=False)
    app = create_app(ObservedEngine(), settings)
    with TestClient(app) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'token': settings.token, 'mode': 'context', 'observe': True})
            ready = socket.receive_json()
            assert 'observability' not in ready

            socket.send_bytes(pcm(0.5, 0.1))
            socket.send_bytes(pcm(0.5))
            assert socket.receive_json()['state'] == 'processing'
            subtitle = socket.receive_json()
            assert subtitle['translated'] == 'Hello'
            assert 'telemetry' not in subtitle
            assert 'timings' not in subtitle
            assert socket.receive_json()['state'] == 'listening'


def test_queue_evictions_are_counted_without_releasing_inflight_work_early():
    release = asyncio.Event()
    loop_holder = {}
    timers = []

    class SlowEngine(ObservedEngine):
        async def process(self, utterance, mode, history):
            loop_holder['loop'] = asyncio.get_running_loop()
            if not release.is_set():
                timer = threading.Timer(0.15, lambda: loop_holder['loop'].call_soon_threadsafe(release.set))
                timers.append(timer)
                timer.start()
                await release.wait()
            self.finish_trace(utterance)
            return {'type': 'subtitle', 'original': '', 'translated': 'Hello'}

    with TestClient(create_app(SlowEngine(), SETTINGS)) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'token': SETTINGS.token, 'mode': 'context', 'observe': True})
            socket.receive_json()
            for _ in range(4):
                socket.send_bytes(pcm(0.5, 0.1))
                socket.send_bytes(pcm(0.5))
            socket.send_json({'type': 'benchmark_end'})
            while True:
                message = socket.receive_json()
                if message['type'] == 'benchmark_complete':
                    break
            assert message['counters']['segmented'] == 4
            assert message['counters']['dropped_queue'] == 2
            assert message['counters']['emitted'] == 2
            assert sorted(event['segment_id'] for event in message['events']) == [1, 2, 3, 4]
            for timer in timers:
                timer.join(timeout=1)


def test_empty_asr_attempt_has_a_timing_record_and_does_not_emit_caption():
    class EmptyEngine(ObservedEngine):
        async def process(self, utterance, mode, history):
            self.finish_trace(utterance)
            utterance.trace['result_ready_ms'] = utterance.trace['asr_finished_ms']
            return None

    with TestClient(create_app(EmptyEngine(), SETTINGS)) as client:
        with client.websocket_connect('/listen', headers={'Origin': ORIGIN}) as socket:
            socket.send_json({'token': SETTINGS.token, 'mode': 'context', 'observe': True})
            socket.receive_json()
            socket.send_bytes(pcm(0.5, 0.1))
            socket.send_bytes(pcm(0.5))
            assert socket.receive_json()['state'] == 'processing'
            event = socket.receive_json()
            assert event['type'] == 'metrics'
            assert event['event']['outcome'] == 'empty_asr'
            assert event['event']['timings']['asr_ms'] == 2
            assert socket.receive_json()['state'] == 'listening'


def test_failed_worker_unblocks_benchmark_drain_when_pending_queue_cannot_finish():
    async def scenario():
        queue = asyncio.Queue()
        queue.put_nowait('inflight')
        queue.put_nowait('pending')

        async def fail_while_processing():
            queue.get_nowait()
            queue.task_done()
            raise RuntimeError('inference failed')

        worker = asyncio.create_task(fail_while_processing())
        try:
            await asyncio.wait_for(wait_for_queue_drain(queue, worker), timeout=0.2)
        except RuntimeError as error:
            assert str(error) == 'inference failed'
        else:
            raise AssertionError('A failed worker must interrupt the queue drain.')

    asyncio.run(scenario())


def test_translation_duration_is_zero_when_skipped_and_unknown_for_empty_asr():
    async def scenario():
        engine = SubtitleEngine(SETTINGS)
        engine.transcribe = lambda samples, mode: Transcription('Hello', 'en', 0.99)
        engine.translate = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('English should bypass translation.'))
        trace = {'speech_started_ms': 1000.0}
        try:
            result = await engine.process(
                Utterance(np.zeros(100, dtype=np.float32), 1.0, trace), 'context', deque()
            )
            assert result['timings']['translation_ms'] == 0.0

            engine.transcribe = lambda samples, mode: Transcription('', None, 0.0)
            empty_trace = {'speech_started_ms': 1000.0}
            empty = await engine.process(
                Utterance(np.zeros(100, dtype=np.float32), 1.0, empty_trace), 'context', deque()
            )
            assert empty is None
            assert trace_durations(empty_trace)['translation_ms'] is None
        finally:
            await engine.client.aclose()

    asyncio.run(scenario())
