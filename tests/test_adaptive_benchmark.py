import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from backend.config import Settings
import scripts.adaptive_benchmark as adaptive_benchmark
from scripts import latency_benchmark
from scripts.adaptive_benchmark import (
    _quality_sidecar,
    _mark_watchdog_deadline,
    _tracked_buffer_factory,
    atomic_write_json,
    build_matrix,
    cell_configuration,
    first_caption_per_session,
    hold_reason,
    next_wait_event,
    reserve_listener,
    run_matrix,
    supervise_worker,
    wait_for_checkpoint,
)


def test_matrix_has_legacy_control_then_canonical_adaptive_cartesian_product():
    variants = build_matrix()

    assert len(variants) == 19
    assert variants[0] == {
        'name': 'legacy', 'segmentation_mode': 'legacy',
        'speech_max_seconds': None, 'speech_silence_seconds': None,
    }
    assert variants[1]['name'] == 'adaptive_rms-max2.5-silence0.3'
    assert variants[10]['name'] == 'adaptive_vad-max2.5-silence0.3'
    assert variants[-1]['name'] == 'adaptive_vad-max5-silence0.45'


def test_matrix_filters_preserve_legacy_unless_skipped_and_use_canonical_order():
    variants = build_matrix(
        modes=('adaptive_vad', 'adaptive_rms'), maximums=(5.0, 2.5),
        silences=(0.45, 0.3), include_legacy=True,
    )

    assert [variant['name'] for variant in variants] == [
        'legacy',
        'adaptive_rms-max2.5-silence0.3',
        'adaptive_rms-max2.5-silence0.45',
        'adaptive_rms-max5-silence0.3',
        'adaptive_rms-max5-silence0.45',
        'adaptive_vad-max2.5-silence0.3',
        'adaptive_vad-max2.5-silence0.45',
        'adaptive_vad-max5-silence0.3',
        'adaptive_vad-max5-silence0.45',
    ]
    assert len(build_matrix(modes=('adaptive_vad',), maximums=(3.0,),
                            silences=(0.35,), include_legacy=False)) == 1


@pytest.mark.parametrize('kwargs', [
    {'modes': ('legacy',)},
    {'maximums': (4.0,)},
    {'silences': (0.4,)},
    {'modes': ()},
    {'maximums': (2.5, 2.5)},
])
def test_matrix_rejects_invalid_or_ambiguous_filters(kwargs):
    with pytest.raises(ValueError):
        build_matrix(**kwargs)


def test_too_many_sessions_fail_before_loading_settings_or_models(monkeypatch):
    monkeypatch.setattr(adaptive_benchmark.latency_benchmark, 'load_manifest',
                        lambda path: {'samples': [{'audio_seconds': 1}] * 33})
    args = SimpleNamespace(modes=None, maximums=None, silences=None, skip_legacy=False,
                           repeats=2, timeout=30, manifest=Path('unused.json'))

    def forbidden_settings():
        raise AssertionError('Session bounds must be checked before setup.')

    with pytest.raises(ValueError, match='At most 64 replay sessions'):
        adaptive_benchmark._validate_matrix_inputs(args, settings_loader=forbidden_settings)


def test_adaptive_configuration_reports_the_effective_sample_grid():
    settings = Settings(token='local-only')
    config = cell_configuration({
        'segmentation_mode': 'adaptive_rms', 'speech_max_seconds': 2.5,
        'speech_silence_seconds': 0.3,
    }, settings, {'frame_samples': 2048})

    assert config['speech_min_seconds'] == 0.25
    assert config['speech_overlap_seconds'] == 0.16
    assert config['effective_quantization'] == {
        'window_samples': 512,
        'window_ms': 32.0,
        'limits_seconds': {
            'speech_min_seconds': 0.256,
            'speech_max_seconds': 2.496,
            'speech_silence_seconds': 0.32,
            'speech_overlap_seconds': 0.16,
        },
    }
    legacy = cell_configuration({
        'segmentation_mode': 'legacy', 'speech_max_seconds': None,
        'speech_silence_seconds': None,
    }, settings, {'frame_samples': 2048})
    assert legacy['effective_quantization']['window_ms'] == 128.0
    assert legacy['effective_quantization']['limits_seconds'] is None


def test_first_caption_metrics_use_lowest_rendered_segment_id_per_session():
    report = first_caption_per_session([
        {'sample_id': 'japanese', 'iteration': 1, 'events': [
            {'segment_id': 3, 'timings': {'first_readable_ms': 30, 'asr_ms': 9}},
            {'segment_id': 1, 'timings': {'asr_ms': 1}},
            {'segment_id': 2, 'timings': {'first_readable_ms': 20, 'asr_ms': 7}},
        ]},
        {'sample_id': 'english', 'iteration': 1, 'events': [
            {'segment_id': 1, 'timings': {'first_readable_ms': None}},
        ]},
    ])

    assert report['count'] == 1
    assert report['sessions'][0]['segment_id'] == 2
    assert report['latency_ms']['first_readable_ms'] == {'count': 1, 'p50': 20, 'p95': 20}
    assert report['by_sample']['japanese']['count'] == 1


def test_quality_mapping_keeps_samples_aligned_across_zero_segment_sessions():
    engine = SimpleNamespace(
        quality_session_ordinal=3,
        quality_buffers=[
            {'session_ordinal': 1, 'requested_mode': 'adaptive_rms',
             'buffer': SimpleNamespace(mode='adaptive_rms')},
            {'session_ordinal': 2, 'requested_mode': 'adaptive_rms',
             'buffer': SimpleNamespace(mode='adaptive_rms')},
            {'session_ordinal': 3, 'requested_mode': 'adaptive_rms',
             'buffer': SimpleNamespace(mode='adaptive_rms')},
        ],
        quality_segments=[
            {'session_ordinal': 1, 'segment_id': 1, 'audio_seconds': 1.0,
             'endpoint': 'silence', 'endpoint_mode': 'adaptive_rms', 'original': 'first'},
            {'session_ordinal': 3, 'segment_id': 1, 'audio_seconds': 0.5,
             'endpoint': 'source_eof', 'endpoint_mode': 'adaptive_rms', 'original': 'third'},
        ],
    )
    report = {'records': [
        {'sample_id': 'source-a', 'iteration': 1,
         'counters': {'emitted': 1, 'empty_asr': 0}},
        {'sample_id': 'silent-control', 'iteration': 1,
         'counters': {'emitted': 0, 'empty_asr': 0}},
        {'sample_id': 'source-c', 'iteration': 1,
         'counters': {'emitted': 0, 'empty_asr': 1}},
    ]}

    sidecar = _quality_sidecar(engine, report,
                               {'segmentation_mode': 'adaptive_rms'}, cell_index=2)

    assert sidecar['sample_mapping'] == 'verified-session-ordinal'
    assert [session['sample_id'] for session in sidecar['sessions']] == [
        'source-a', 'silent-control', 'source-c',
    ]
    assert sidecar['sessions'][1]['captured_segments'] == 0
    assert sidecar['segments'][1]['sample_id'] == 'source-c'


def test_buffer_tracker_preserves_original_factory_and_records_live_buffer_reference(monkeypatch):
    buffer = SimpleNamespace(mode='adaptive_vad')
    calls = []
    engine = SimpleNamespace(quality_session_ordinal=4, quality_buffers=[])

    def original_factory(settings, observe=False):
        calls.append((settings, observe))
        return buffer

    monkeypatch.setattr(adaptive_benchmark.backend_app_module,
                        'create_speech_buffer', original_factory)
    settings = SimpleNamespace(segmentation_mode='adaptive_vad')
    tracked = _tracked_buffer_factory(engine, session_limit=5)

    assert tracked(settings, observe=True) is buffer
    buffer.mode = 'adaptive_rms'
    assert calls == [(settings, True)]
    assert engine.quality_buffers == [{
        'session_ordinal': 4,
        'requested_mode': 'adaptive_vad',
        'buffer': buffer,
    }]


@pytest.mark.parametrize('fallback, expected_hold', [
    (False, None),
    (True, 'adaptive_vad was not consistently observed (effective modes: adaptive_rms, adaptive_vad).'),
])
def test_zero_output_session_is_checked_for_actual_vad_fallback(fallback, expected_hold):
    engine = SimpleNamespace(
        quality_session_ordinal=3,
        quality_buffers=[
            {'session_ordinal': 1, 'requested_mode': 'adaptive_vad',
             'buffer': SimpleNamespace(mode='adaptive_vad')},
            {'session_ordinal': 2, 'requested_mode': 'adaptive_vad',
             'buffer': SimpleNamespace(mode='adaptive_rms' if fallback else 'adaptive_vad')},
            {'session_ordinal': 3, 'requested_mode': 'adaptive_vad',
             'buffer': SimpleNamespace(mode='adaptive_vad')},
        ],
        quality_segments=[
            {'session_ordinal': 1, 'segment_id': 1, 'endpoint_mode': 'adaptive_vad'},
            {'session_ordinal': 3, 'segment_id': 1, 'endpoint_mode': 'adaptive_vad'},
        ],
    )
    report = {
        'gpu': {'available': True}, 'measurement_complete': True,
        'summary': {'counters': {}},
        'records': [
            {'sample_id': 'speech-a', 'iteration': 1,
             'counters': {'emitted': 1, 'empty_asr': 0}},
            {'sample_id': 'negative-control', 'iteration': 1,
             'counters': {'emitted': 0, 'empty_asr': 0}},
            {'sample_id': 'speech-c', 'iteration': 1,
             'counters': {'emitted': 0, 'empty_asr': 1}},
        ],
    }
    quality = _quality_sidecar(engine, report,
                              {'segmentation_mode': 'adaptive_vad'}, cell_index=1)

    assert quality['sample_mapping'] == 'verified-session-ordinal'
    assert quality['sessions'][1]['sample_id'] == 'negative-control'
    assert quality['sessions'][1]['captured_segments'] == 0
    assert quality['sessions'][1]['vad_fallback'] is fallback
    assert hold_reason(report, {'available': True, 'boards': []},
                       {'segmentation_mode': 'adaptive_vad'}, quality) == expected_hold


def test_hold_rules_stop_on_drops_and_vad_fallback_but_allow_empty_asr():
    complete_report = {
        'gpu': {'available': True},
        'measurement_complete': True,
        'summary': {'counters': {'empty_asr': 2, 'dropped_queue': 0}},
    }
    gpu_report = {'available': True, 'boards': [{'peak_memory_used_mib': 4096}]}

    assert hold_reason(complete_report, gpu_report,
                       {'segmentation_mode': 'adaptive_rms'},
                       {'sample_mapping': 'verified-session-ordinal',
                        'effective_endpoint_modes': ['adaptive_rms']}) is None
    complete_report['summary']['counters']['dropped_queue'] = 1
    assert hold_reason(complete_report, gpu_report,
                       {'segmentation_mode': 'adaptive_rms'},
                       {'effective_endpoint_modes': ['adaptive_rms']}) == 'The replay dropped queued or incoming audio.'

    complete_report['summary']['counters']['dropped_queue'] = 0
    assert hold_reason(complete_report, gpu_report,
                       {'segmentation_mode': 'adaptive_vad'},
                       {'sample_mapping': 'verified-session-ordinal',
                        'effective_endpoint_modes': ['adaptive_rms']}) == (
                           'adaptive_vad was not consistently observed (effective modes: adaptive_rms).')


def test_hold_rules_include_unavailable_gpu_and_memory_ceiling():
    report = {'gpu': {'available': True}, 'measurement_complete': True,
              'summary': {'counters': {}}}
    quality = {'effective_endpoint_modes': ['adaptive_rms']}

    assert hold_reason(report, {'available': False},
                       {'segmentation_mode': 'adaptive_rms'}, quality) == 'GPU usage could not be measured.'
    assert hold_reason(report, {'available': True, 'boards': [
        {'peak_memory_used_mib': 8192},
    ]}, {'segmentation_mode': 'adaptive_rms'}, quality) == (
        'Peak total GPU memory use reached 8192 MiB.')


@pytest.mark.parametrize('mode', ['legacy', 'adaptive_rms', 'adaptive_vad'])
def test_unmapped_quality_evidence_holds_every_segmentation_mode(mode):
    report = {'gpu': {'available': True}, 'measurement_complete': True,
              'summary': {'counters': {}}}
    quality = {'sample_mapping': 'unavailable', 'effective_endpoint_modes': [mode],
               'sessions': [{'effective_endpoint_modes': [mode], 'vad_fallback': False}]}
    assert hold_reason(report, {'available': True, 'boards': []},
                       {'segmentation_mode': mode}, quality) == (
                           'Quality evidence could not be mapped to replay sessions.')


@pytest.mark.parametrize('orphan_ordinal', [None, 0, 2])
def test_unattributed_asr_output_cannot_receive_verified_quality_mapping(orphan_ordinal):
    engine = SimpleNamespace(
        quality_session_ordinal=1,
        quality_buffers=[{'session_ordinal': 1, 'requested_mode': 'adaptive_rms',
                          'buffer': SimpleNamespace(mode='adaptive_rms')}],
        quality_segments=[{'session_ordinal': orphan_ordinal, 'asr_text': 'invented text'}],
    )
    report = {'records': [{'sample_id': 'silence', 'iteration': 1, 'counters': {}}]}
    quality = _quality_sidecar(engine, report, {'segmentation_mode': 'adaptive_rms'}, 1)
    assert quality['sample_mapping'] == 'unavailable'


def test_legacy_configuration_reports_fixed_runtime_limits_when_adaptive_settings_differ():
    settings = Settings(token='local-only', speech_min_seconds=.5, speech_max_seconds=3,
                        speech_silence_seconds=.35, speech_overlap_seconds=.32)
    config = cell_configuration(build_matrix()[0], settings, {'frame_samples': 2048})
    assert config['speech_min_seconds'] == .25
    assert config['speech_max_seconds'] == 5
    assert config['speech_silence_seconds'] == .45
    assert config['speech_overlap_seconds'] == 0


def test_gpu_telemetry_failure_after_success_invalidates_availability(monkeypatch):
    class Probe:
        def __init__(self, returncode):
            self.returncode = returncode

        async def communicate(self):
            return b'0, Test GPU, 8192, 4096, 25\n', b''

    probes = iter([Probe(0), Probe(1)])

    async def create_probe(*_args, **_kwargs):
        return next(probes)

    async def next_probe(_delay):
        pass

    monkeypatch.setattr(latency_benchmark.asyncio, 'create_subprocess_exec', create_probe)
    monkeypatch.setattr(latency_benchmark.asyncio, 'sleep', next_probe)
    gpu = latency_benchmark.GPUUsage()
    asyncio.run(gpu.run())
    assert gpu.report['samples'] == 1
    assert gpu.report['boards'][0]['peak_memory_used_mib'] == 4096
    assert gpu.report['available'] is False
    report = {'gpu': gpu.report, 'measurement_complete': True, 'summary': {'counters': {}}}
    assert hold_reason(report, gpu.report, {'segmentation_mode': 'adaptive_rms'},
                       {'sample_mapping': 'verified-session-ordinal'}) == (
                           'GPU usage could not be measured.')


def test_wait_event_returns_only_next_cell_or_terminal_status():
    checkpoint = {
        'status': 'running', 'completed_cells': 2,
        'cells': [
            {'index': 1, 'status': 'complete', 'name': 'legacy'},
            {'index': 2, 'status': 'complete', 'name': 'adaptive_rms-a'},
            {'index': 3, 'status': 'running', 'name': 'adaptive_rms-b'},
        ],
    }

    assert next_wait_event(checkpoint, 0) == {
        'event': 'cell_complete', 'cell': 1, 'variant': 'legacy'}
    assert next_wait_event(checkpoint, 2) is None
    checkpoint['cells'][2].update(status='HOLD', hold_reason='drop observed')
    checkpoint.update(status='HOLD', hold_reason='drop observed')
    assert next_wait_event(checkpoint, 2) == {
        'event': 'HOLD', 'cell': 3, 'reason': 'drop observed'}


def test_atomic_checkpoint_and_wait_report_a_completed_cell(tmp_path):
    path = tmp_path / 'checkpoint.json'
    atomic_write_json(path, {
        'status': 'running', 'completed_cells': 1,
        'cells': [{'index': 1, 'status': 'complete', 'name': 'legacy'}],
    })

    assert json.loads(path.read_text(encoding='utf-8'))['completed_cells'] == 1
    assert asyncio.run(wait_for_checkpoint(path, after_cell=0, timeout=0)) == {
        'event': 'cell_complete', 'cell': 1, 'variant': 'legacy'}
    assert asyncio.run(wait_for_checkpoint(path, after_cell=1, timeout=0)) == {
        'event': 'timeout', 'after_cell': 1}


def test_occupied_listener_is_refused_before_engine_construction(tmp_path, monkeypatch):
    class OccupiedSocket:
        closed = False

        def setsockopt(self, *_args):
            pass

        def bind(self, _address):
            raise OSError('in use')

        def listen(self, _backlog):
            raise AssertionError('listen must not follow a failed bind')

        def close(self):
            self.closed = True

    occupied = OccupiedSocket()
    with pytest.raises(RuntimeError, match='Cannot reserve'):
        reserve_listener(socket_factory=lambda *_args: occupied)
    assert occupied.closed

    monkeypatch.setattr('scripts.adaptive_benchmark.latency_benchmark.load_manifest',
                        lambda _path: {'samples': [{'audio_seconds': 1.0}]})
    engine_calls = []

    def forbidden_engine(_settings):
        engine_calls.append(True)
        raise AssertionError('engine construction must follow successful port reservation')

    args = SimpleNamespace(
        modes=('adaptive_rms',), maximums=(2.5,), silences=(0.3,), skip_legacy=True,
        repeats=1, timeout=1, manifest=Path('unused.json'), output=tmp_path / 'checkpoint.json',
        browser=False,
    )
    with pytest.raises(RuntimeError, match='Cannot reserve'):
        asyncio.run(run_matrix(
            args,
            listener_reserver=lambda: reserve_listener(socket_factory=lambda *_args: occupied),
            engine_factory=forbidden_engine,
            settings_loader=lambda: Settings(token='not-written', latency_observability=True),
        ))
    assert engine_calls == []
    checkpoint = json.loads(args.output.read_text(encoding='utf-8'))
    assert checkpoint['status'] == 'failed'
    assert 'not-written' not in args.output.read_text(encoding='utf-8')


def test_matrix_reuses_one_engine_and_changes_only_selected_segmentation_settings(tmp_path, monkeypatch):
    class Listener:
        closed = False

        def close(self):
            self.closed = True

    class GPUUsage:
        def __init__(self):
            self.report = {'available': True, 'samples': 1,
                           'boards': [{'peak_memory_used_mib': 1024}]}

        async def run(self):
            await asyncio.Event().wait()

    class FakeConfig:
        def __init__(self, app, **_kwargs):
            self.app = app

    class FakeServer:
        def __init__(self, _config):
            self.started = False
            self.should_exit = False

        async def serve(self, sockets):
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.001)
            for listener in sockets:
                listener.close()

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings
            self.quality_segments = []
            self.quality_buffers = []
            self.quality_session_ordinal = 0
            self.client = SimpleNamespace(aclose=self.close_client)
            self.client_closed = False

        async def close_client(self):
            self.client_closed = True

    listener = Listener()
    engines = []
    replay_settings = []
    monkeypatch.setattr(adaptive_benchmark.latency_benchmark, 'GPUUsage', GPUUsage)
    monkeypatch.setattr(adaptive_benchmark.latency_benchmark, 'load_manifest',
                        lambda _path: {'frame_samples': 2048, 'samples': [
                            {'id': 'sample', 'audio_seconds': 1.0},
                        ]})
    monkeypatch.setattr(adaptive_benchmark.uvicorn, 'Config', FakeConfig)
    monkeypatch.setattr(adaptive_benchmark.uvicorn, 'Server', FakeServer)
    monkeypatch.setattr(adaptive_benchmark, 'create_app',
                        lambda engine, settings: SimpleNamespace(
                            state=SimpleNamespace(active=False)))

    def engine_factory(settings):
        engine = FakeEngine(settings)
        engines.append(engine)
        return engine

    async def replay(_args):
        engine = engines[0]
        settings = engine.settings
        engine.quality_session_ordinal += 1
        adaptive_benchmark.backend_app_module.create_speech_buffer(settings, observe=True)
        engine.quality_segments.append({'session_ordinal': 1, 'segment_id': 1,
                                        'asr_text': '', 'outcome': 'empty_asr'})
        replay_settings.append((
            settings.segmentation_mode,
            settings.speech_max_seconds,
            settings.speech_silence_seconds,
            settings.speech_min_seconds,
            settings.speech_overlap_seconds,
            settings.translation_model,
        ))
        return {
            'gpu': {'available': True},
            'measurement_complete': True,
            'summary': {'counters': {'empty_asr': 1}},
            'per_sample': {'sample': {}},
            'records': [{'sample_id': 'sample', 'iteration': 1, 'events': [],
                         'counters': {'empty_asr': 1},
                         'playback_to_first_readable_ms': 125}],
        }

    monkeypatch.setattr(adaptive_benchmark.latency_benchmark, 'run', replay)
    args = SimpleNamespace(
        modes=('adaptive_rms',), maximums=(2.5,), silences=(0.3,), skip_legacy=False,
        repeats=1, timeout=1, manifest=Path('unused.json'), output=tmp_path / 'checkpoint.json',
        browser=False,
    )

    checkpoint = asyncio.run(adaptive_benchmark.run_matrix(
        args,
        listener_reserver=lambda: listener,
        engine_factory=engine_factory,
        settings_loader=lambda: Settings(token='local-only', latency_observability=True),
    ))

    assert checkpoint['status'] == 'complete'
    assert len(engines) == 1
    assert [row[:3] for row in replay_settings] == [
        ('legacy', 5.0, 0.45), ('adaptive_rms', 2.5, 0.3),
    ]
    assert all(row[3:] == (0.25, 0.16, Settings(token='x').translation_model)
               for row in replay_settings)
    assert checkpoint['cells'][1]['configuration']['effective_quantization']['window_ms'] == 32.0
    assert listener.closed
    report = json.loads(Path(checkpoint['cells'][1]['report_path']).read_text(encoding='utf-8'))
    assert report['summary']['playback_to_first_readable_ms'] == {
        'count': 1, 'p50': 125, 'p95': 125,
    }
    assert report['per_sample']['sample']['playback_to_first_readable_ms']['p50'] == 125
    assert 'controlled sample audio playing event' in report['definitions']['playback_to_first_readable_ms']


def test_startup_failure_closes_listener_client_and_gpu_monitor(tmp_path, monkeypatch):
    class Listener:
        closed = False

        def close(self):
            self.closed = True

    class GPUUsage:
        def __init__(self):
            self.report = {'available': True, 'samples': 1, 'boards': []}
            self.stopped = False

        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped = True

    class FakeConfig:
        def __init__(self, _app, **_kwargs):
            pass

    class FailingServer:
        def __init__(self, _config):
            self.started = False
            self.should_exit = False

        async def serve(self, sockets):
            raise RuntimeError('startup failed')

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings
            self.quality_segments = []
            self.quality_buffers = []
            self.quality_session_ordinal = 0
            self.client = Client()

    listener = Listener()
    gpu_monitors = []
    engines = []
    monkeypatch.setattr(adaptive_benchmark.latency_benchmark, 'load_manifest',
                        lambda _path: {'frame_samples': 2048, 'samples': [
                            {'id': 'sample', 'audio_seconds': 1.0},
                        ]})
    monkeypatch.setattr(adaptive_benchmark.uvicorn, 'Config', FakeConfig)
    monkeypatch.setattr(adaptive_benchmark.uvicorn, 'Server', FailingServer)
    monkeypatch.setattr(adaptive_benchmark, 'create_app',
                        lambda engine, settings: SimpleNamespace(
                            state=SimpleNamespace(active=False)))

    def gpu_factory():
        monitor = GPUUsage()
        gpu_monitors.append(monitor)
        return monitor

    monkeypatch.setattr(adaptive_benchmark.latency_benchmark, 'GPUUsage', gpu_factory)

    def engine_factory(settings):
        engine = FakeEngine(settings)
        engines.append(engine)
        return engine

    args = SimpleNamespace(
        modes=('adaptive_rms',), maximums=(2.5,), silences=(0.3,), skip_legacy=True,
        repeats=1, timeout=1, manifest=Path('unused.json'), output=tmp_path / 'checkpoint.json',
        browser=False,
    )

    with pytest.raises(RuntimeError, match='startup failed'):
        asyncio.run(adaptive_benchmark.run_matrix(
            args,
            listener_reserver=lambda: listener,
            engine_factory=engine_factory,
            settings_loader=lambda: Settings(token='local-only', latency_observability=True),
        ))

    assert len(engines) == 1
    assert listener.closed
    assert engines[0].client.closed
    assert gpu_monitors[0].stopped
    assert json.loads(args.output.read_text(encoding='utf-8'))['status'] == 'failed'


def test_watchdog_keeps_completed_worker_exit_code_without_termination():
    class CompletedWorker:
        pid = 456
        returncode = 7

        def wait(self, timeout):
            assert timeout == 12
            return self.returncode

        def poll(self):
            return self.returncode

    worker = CompletedWorker()
    terminated = []
    result = supervise_worker(
        ['worker'], 12,
        popen_factory=lambda _command, **_kwargs: worker,
        tree_terminator=lambda process: terminated.append(process),
    )

    assert result == {'timed_out': False, 'returncode': 7}
    assert terminated == []


def test_watchdog_terminates_only_a_still_live_worker_after_deadline():
    class TimedWorker:
        pid = 789
        returncode = None

        def __init__(self):
            self.wait_count = 0
            self.killed = False

        def wait(self, timeout):
            self.wait_count += 1
            if self.wait_count == 1:
                assert timeout == 12
                raise subprocess.TimeoutExpired('worker', timeout)
            self.returncode = -9
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True

    worker = TimedWorker()
    terminated = []

    def terminate(process):
        terminated.append(process)

    result = supervise_worker(
        ['worker'], 12,
        popen_factory=lambda _command, **_kwargs: worker,
        tree_terminator=terminate,
    )

    assert result == {'timed_out': True, 'returncode': -9}
    assert terminated == [worker]
    assert not worker.killed


def test_watchdog_deadline_marks_the_running_cell_failed(tmp_path):
    path = tmp_path / 'checkpoint.json'
    atomic_write_json(path, {
        'status': 'running', 'completed_cells': 2,
        'cells': [{'index': 3, 'status': 'running', 'name': 'adaptive_vad'}],
    })

    checkpoint = _mark_watchdog_deadline(path, total_cells=19)

    assert checkpoint['status'] == 'failed'
    assert checkpoint['error'] == 'WatchdogDeadlineExceeded'
    assert checkpoint['cells'][0]['status'] == 'failed'


def test_browser_replay_awaits_owned_process_cleanup_before_propagating_cancellation(
        tmp_path, monkeypatch):
    class GPUUsage:
        report = {'available': True, 'samples': 1, 'boards': []}

        async def run(self):
            await asyncio.Event().wait()

    class BrowserProcess:
        returncode = None

        async def communicate(self):
            communicating.set()
            await asyncio.Event().wait()

    communicating = asyncio.Event()
    cleanup_finished = asyncio.Event()
    process = BrowserProcess()
    cleanup_calls = []

    async def create_browser_process(*_args, **_kwargs):
        return process

    async def cleanup_owned_process(received_process):
        cleanup_calls.append(received_process)
        await asyncio.sleep(0)
        cleanup_finished.set()

    monkeypatch.setattr(latency_benchmark, 'GPUUsage', GPUUsage)
    monkeypatch.setattr(latency_benchmark, 'load_manifest', lambda _path: {'samples': []})
    monkeypatch.setattr(latency_benchmark.asyncio, 'create_subprocess_exec', create_browser_process)
    monkeypatch.setattr(latency_benchmark, 'terminate_owned_subprocess_tree', cleanup_owned_process)
    args = SimpleNamespace(
        manifest=tmp_path / 'manifest.json', repeats=1, timeout=1,
        url='ws://127.0.0.1:8765/listen', browser=True,
        output=tmp_path / 'report.json', settings=None,
    )

    async def cancel_replay():
        task = asyncio.create_task(latency_benchmark.run(args))
        await asyncio.wait_for(communicating.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup_calls == [process]
        assert cleanup_finished.is_set()

    asyncio.run(cancel_replay())
