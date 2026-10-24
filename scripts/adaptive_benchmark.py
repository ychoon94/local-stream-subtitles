"""Run a bounded segmentation matrix with one warmed local inference worker."""

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn

from backend.app import create_app
from backend.audio import SAMPLE_RATE
from backend.config import ROOT, SETTINGS_PATH, load_settings
from backend.engine import SubtitleEngine
from scripts import latency_benchmark
from scripts.process_cleanup import terminate_owned_process_tree

backend_app_module = importlib.import_module('backend.app')

DEFAULT_MODES = ('adaptive_rms', 'adaptive_vad')
MODE_ORDER = ('adaptive_rms', 'adaptive_vad')
MAXIMUMS = (2.5, 3.0, 5.0)
SILENCES = (0.3, 0.35, 0.45)
DEFAULT_CHECKPOINT = ROOT / '.runtime' / 'adaptive-benchmark' / 'checkpoint.json'
STARTUP_TIMEOUT_SECONDS = 300
SHUTDOWN_TIMEOUT_SECONDS = 30
GPU_PROBE_TIMEOUT_SECONDS = 5
GPU_MEMORY_HOLD_MIB = 8192
WAIT_POLL_SECONDS = 0.25
ADAPTIVE_WINDOW_SAMPLES = 512
WATCHDOG_ALLOWANCE_SECONDS = 15
WATCHDOG_TERMINATION_TIMEOUT_SECONDS = 10
WORKER_MARKER_ENV = 'ADAPTIVE_BENCHMARK_INTERNAL_WORKER'
MAX_CAPTURED_SESSION_BUFFERS = 64


def _select_values(values, allowed, label):
    selected = tuple(allowed) if values is None else tuple(values)
    if not selected:
        raise ValueError(f'At least one {label} value is required.')
    if len(set(selected)) != len(selected):
        raise ValueError(f'Duplicate {label} values are not allowed.')
    invalid = [value for value in selected if value not in allowed]
    if invalid:
        choices = ', '.join(str(value) for value in allowed)
        raise ValueError(f'Unsupported {label} value {invalid[0]}; choose from {choices}.')
    return selected


def build_matrix(modes=None, maximums=None, silences=None, include_legacy=True):
    """Return variants in canonical order, independent of filter argument order."""
    selected_modes = _select_values(modes, DEFAULT_MODES, 'mode')
    selected_maximums = _select_values(maximums, MAXIMUMS, 'maximum')
    selected_silences = _select_values(silences, SILENCES, 'silence')
    variants = []
    if include_legacy:
        variants.append({'name': 'legacy', 'segmentation_mode': 'legacy',
                         'speech_max_seconds': None, 'speech_silence_seconds': None})
    for mode in MODE_ORDER:
        if mode not in selected_modes:
            continue
        for maximum in MAXIMUMS:
            if maximum not in selected_maximums:
                continue
            for silence in SILENCES:
                if silence not in selected_silences:
                    continue
                variants.append({
                    'name': f'{mode}-max{_number(maximum)}-silence{_number(silence)}',
                    'segmentation_mode': mode,
                    'speech_max_seconds': maximum,
                    'speech_silence_seconds': silence,
                })
    return variants


def _number(value):
    return str(int(value)) if float(value).is_integer() else str(value)


def reserve_listener(host='127.0.0.1', port=8765, socket_factory=socket.socket):
    """Reserve the benchmark endpoint before anything can load a GPU model."""
    listener = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((host, port))
        listener.listen(socket.SOMAXCONN)
        return listener
    except OSError as error:
        listener.close()
        raise RuntimeError(f'Cannot reserve {host}:{port}; a listener may already be using the benchmark port.') from error


def atomic_write_json(path, payload):
    """Replace a checkpoint or sidecar atomically on the same filesystem."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class RecordingEngine(SubtitleEngine):
    """Capture local quality evidence while leaving the live inference path intact."""

    def __init__(self, settings):
        super().__init__(settings)
        self.quality_segments = []
        self.quality_buffers = []
        self.quality_session_ordinal = 0

    async def check_translation(self):
        await super().check_translation()
        self.quality_session_ordinal += 1

    def transcribe(self, samples, mode):
        transcription = super().transcribe(samples, mode)
        self._last_transcription = transcription
        return transcription

    async def process(self, utterance, mode, history):
        result = await super().process(utterance, mode, history)
        trace = utterance.trace or {}
        transcription = getattr(self, '_last_transcription', None)
        self.quality_segments.append({
            'session_ordinal': self.quality_session_ordinal,
            'segment_id': trace.get('segment_id'),
            'audio_seconds': round(len(utterance.samples) / 16000, 3),
            'endpoint': trace.get('endpoint'),
            'endpoint_mode': trace.get('endpoint_mode'),
            'outcome': trace.get('outcome'),
            'asr_text': transcription.text if transcription else None,
            'asr_language': transcription.language if transcription else None,
            'original': result.get('original', '') if result else '',
            'translated': result.get('translated', '') if result else '',
        })
        return result


def _tracked_buffer_factory(engine, session_limit):
    original_factory = backend_app_module.create_speech_buffer

    def factory(settings, observe=False):
        if len(engine.quality_buffers) >= session_limit:
            raise RuntimeError('Replay exceeded the bounded speech-buffer session limit.')
        buffer = original_factory(settings, observe=observe)
        engine.quality_buffers.append({
            'session_ordinal': engine.quality_session_ordinal,
            'requested_mode': settings.segmentation_mode,
            'buffer': buffer,
        })
        return buffer

    return factory


def _quality_sidecar(engine, report, variant, cell_index):
    records = report.get('records', [])
    segments = []
    segment_ordinals = {}
    captured_by_session = {}
    for captured in engine.quality_segments:
        session_ordinal = captured.get('session_ordinal')
        if not isinstance(session_ordinal, int) or session_ordinal < 1:
            session_ordinal = None
        segment_ordinals[session_ordinal] = segment_ordinals.get(session_ordinal, 0) + 1
        captured_by_session[session_ordinal] = captured_by_session.get(session_ordinal, 0) + 1
        segments.append({
            **captured,
            'session_ordinal': session_ordinal,
            'session_segment_ordinal': segment_ordinals[session_ordinal],
        })

    buffers_by_session = {}
    buffer_observations = []
    for captured in engine.quality_buffers:
        session_ordinal = captured.get('session_ordinal')
        requested_mode = captured.get('requested_mode')
        buffer = captured.get('buffer')
        effective_mode = getattr(buffer, 'mode', 'legacy' if requested_mode == 'legacy' else 'unknown')
        if not isinstance(session_ordinal, int) or session_ordinal < 1:
            session_ordinal = None
        buffers_by_session.setdefault(session_ordinal, []).append(effective_mode)
        buffer_observations.append({
            'session_ordinal': session_ordinal,
            'requested_mode': requested_mode,
            'effective_mode': effective_mode,
            'vad_fallback': requested_mode == 'adaptive_vad' and effective_mode != 'adaptive_vad',
        })

    mapping_available = (
        engine.quality_session_ordinal == len(records)
        and len(engine.quality_buffers) == len(records)
        and all(ordinal is not None and 1 <= ordinal <= len(records)
                for ordinal in captured_by_session)
    )
    session_rows = []
    for ordinal, record in enumerate(records, start=1):
        counters = record.get('counters', {})
        expected_segments = counters.get('emitted', 0) + counters.get('empty_asr', 0)
        captured_segments = captured_by_session.get(ordinal, 0)
        effective_modes = sorted(set(buffers_by_session.get(ordinal, [])))
        if (captured_segments != expected_segments
                or len(buffers_by_session.get(ordinal, [])) != 1):
            mapping_available = False
        session_rows.append({
            'session_ordinal': ordinal,
            'sample_id': None,
            'iteration': None,
            'captured_segments': captured_segments,
            'expected_processed_segments': expected_segments,
            'requested_mode': variant['segmentation_mode'],
            'effective_endpoint_modes': effective_modes,
            'vad_fallback': variant['segmentation_mode'] == 'adaptive_vad'
            and any(mode != 'adaptive_vad' for mode in effective_modes),
            'counters': counters,
        })
    if mapping_available:
        records_by_ordinal = {index: record for index, record in enumerate(records, start=1)}
        for session in session_rows:
            record = records_by_ordinal[session['session_ordinal']]
            session['sample_id'] = record.get('sample_id')
            session['iteration'] = record.get('iteration')
        for segment in segments:
            record = records_by_ordinal.get(segment['session_ordinal'])
            if record is not None:
                segment['sample_id'] = record.get('sample_id')
                segment['iteration'] = record.get('iteration')

    effective_modes = sorted({mode for modes in buffers_by_session.values() for mode in modes
                              if isinstance(mode, str)})
    return {
        'schema_version': 1,
        'cell': cell_index,
        'requested_mode': variant['segmentation_mode'],
        'effective_endpoint_modes': effective_modes,
        'sample_mapping': 'verified-session-ordinal' if mapping_available else 'unavailable',
        'sessions': session_rows,
        'speech_buffers': buffer_observations,
        'segments': segments,
    }


def hold_reason(report, gpu_report, variant, quality):
    """Return the first stop condition after a measured cell, if any."""
    if not gpu_report.get('available') or not report.get('gpu', {}).get('available'):
        return 'GPU usage could not be measured.'
    boards = gpu_report.get('boards', [])
    peak_used = sum(float(board.get('peak_memory_used_mib', 0)) for board in boards)
    if peak_used >= GPU_MEMORY_HOLD_MIB:
        return f'Peak total GPU memory use reached {GPU_MEMORY_HOLD_MIB} MiB.'

    counters = report.get('summary', {}).get('counters', {})
    if counters.get('dropped_queue', 0) or counters.get('dropped_audio_frames', 0) or counters.get('dropped_audio_samples', 0):
        return 'The replay dropped queued or incoming audio.'
    if counters.get('skipped_stale', 0):
        return 'The replay skipped stale segments.'
    if counters.get('asr_failed', 0):
        return 'Speech recognition failed for a segment.'
    if not report.get('measurement_complete', False):
        return 'The latency replay did not complete its measurements.'
    if quality.get('sample_mapping') != 'verified-session-ordinal':
        return 'Quality evidence could not be mapped to replay sessions.'

    if variant['segmentation_mode'] == 'adaptive_vad':
        sessions = quality.get('sessions', [])
        effective_modes = quality.get('effective_endpoint_modes', [])
        if (not sessions or any(session.get('vad_fallback')
                                or 'adaptive_vad' not in session.get('effective_endpoint_modes', [])
                                for session in sessions)
                or not effective_modes):
            observed = ', '.join(effective_modes) if effective_modes else 'none observed'
            return f'adaptive_vad was not consistently observed (effective modes: {observed}).'
    return None


def _ceil_to_window(samples, window):
    return ((samples + window - 1) // window) * window


def cell_configuration(variant, settings, manifest):
    """Describe configured limits and the endpoint's effective sample grid."""
    mode = variant['segmentation_mode']
    frame_samples = manifest.get('frame_samples', 2048)
    window = ADAPTIVE_WINDOW_SAMPLES if mode != 'legacy' else frame_samples
    adaptive = mode != 'legacy'
    # Legacy ignores all adaptive tuning fields; describe its actual control.
    maximum = variant['speech_max_seconds'] if adaptive else 5.0
    silence = variant['speech_silence_seconds'] if adaptive else 0.45
    effective = None
    if adaptive:
        max_samples = int(maximum * SAMPLE_RATE) // ADAPTIVE_WINDOW_SAMPLES * ADAPTIVE_WINDOW_SAMPLES
        min_samples = _ceil_to_window(math.ceil(settings.speech_min_seconds * SAMPLE_RATE), ADAPTIVE_WINDOW_SAMPLES)
        silence_samples = _ceil_to_window(math.ceil(silence * SAMPLE_RATE), ADAPTIVE_WINDOW_SAMPLES)
        overlap_samples = int(settings.speech_overlap_seconds * SAMPLE_RATE) // ADAPTIVE_WINDOW_SAMPLES * ADAPTIVE_WINDOW_SAMPLES
        effective = {
            'speech_min_seconds': round(min_samples / SAMPLE_RATE, 3),
            'speech_max_seconds': round(max_samples / SAMPLE_RATE, 3),
            'speech_silence_seconds': round(silence_samples / SAMPLE_RATE, 3),
            'speech_overlap_seconds': round(overlap_samples / SAMPLE_RATE, 3),
        }
    return {
        'segmentation_mode': mode,
        'speech_min_seconds': settings.speech_min_seconds if adaptive else 0.25,
        'speech_max_seconds': maximum,
        'speech_silence_seconds': silence,
        'speech_overlap_seconds': settings.speech_overlap_seconds if adaptive else 0.0,
        'effective_quantization': {
            'window_samples': window,
            'window_ms': round(window / SAMPLE_RATE * 1000, 3),
            'limits_seconds': effective,
        },
    }


FIRST_CAPTION_METRICS = (
    'speech_to_segment_ms', 'queue_ms', 'lock_ms', 'asr_ms', 'translation_ms',
    'server_total_ms', 'delivery_ms', 'display_ms', 'first_readable_ms', 'final_ms',
)


def first_caption_per_session(records):
    """Summarize each replay session's lowest-ID caption with a visible timestamp."""
    sessions = []
    for record in records:
        candidates = [event for event in record.get('events', [])
                      if isinstance(event.get('segment_id'), int)
                      and event.get('timings', {}).get('first_readable_ms') is not None]
        if not candidates:
            continue
        first = min(candidates, key=lambda event: event['segment_id'])
        timings = first.get('timings', {})
        sessions.append({
            'sample_id': record.get('sample_id'),
            'iteration': record.get('iteration'),
            'segment_id': first['segment_id'],
            'latency_ms': {metric: timings.get(metric) for metric in FIRST_CAPTION_METRICS},
        })

    def summarize(rows):
        return {metric: latency_benchmark.distribution(
            row.get('latency_ms', {}).get(metric) for row in rows)
                for metric in FIRST_CAPTION_METRICS}

    by_sample = {}
    for session in sessions:
        sample_id = session.get('sample_id') or 'unknown'
        by_sample.setdefault(sample_id, []).append(session)
    return {
        'criterion': 'minimum segment_id with observed first_readable_ms in each replay record',
        'count': len(sessions),
        'latency_ms': summarize(sessions),
        'by_sample': {sample_id: {'count': len(rows), 'latency_ms': summarize(rows)}
                      for sample_id, rows in by_sample.items()},
        'sessions': sessions,
    }


def _add_playback_summary(report):
    records = report.get('records', [])
    summary = report.setdefault('summary', {})
    summary['playback_to_first_readable_ms'] = latency_benchmark.distribution(
        record.get('playback_to_first_readable_ms') for record in records)
    for sample_id, sample_report in report.get('per_sample', {}).items():
        sample_report['playback_to_first_readable_ms'] = latency_benchmark.distribution(
            record.get('playback_to_first_readable_ms') for record in records
            if record.get('sample_id') == sample_id)


def _origin_definition(variant, quality, manifest):
    if variant['segmentation_mode'] == 'legacy':
        frame_samples = manifest.get('frame_samples', 2048)
        frame_ms = round(frame_samples / SAMPLE_RATE * 1000, 3)
        return (f'Legacy energy endpoint: estimated onset is the first energy-positive '
                f'{frame_samples}-sample PCM input frame at server ({frame_ms} ms cadence).')
    modes = quality.get('effective_endpoint_modes', [])
    if not modes:
        return f'No effective endpoint mode was observed for requested {variant["segmentation_mode"]}.'
    descriptions = {
        'adaptive_rms': 'first RMS-positive 32 ms decision window',
        'adaptive_vad': 'first detector-positive 32 ms decision window',
    }
    return '; '.join(
        f'{mode}: estimated onset is the {descriptions.get(mode, "reported endpoint mode")} at server'
        for mode in modes
    ) + '.'


def next_wait_event(checkpoint, after_cell):
    """Select one user-visible checkpoint event without exposing raw reports."""
    for cell in checkpoint.get('cells', []):
        index = cell.get('index')
        if not isinstance(index, int) or index <= after_cell:
            continue
        status = cell.get('status')
        if status == 'complete':
            return {'event': 'cell_complete', 'cell': index, 'variant': cell.get('name')}
        if status == 'HOLD':
            return {'event': 'HOLD', 'cell': index, 'reason': cell.get('hold_reason')}
        if status == 'failed':
            return {'event': 'failure', 'cell': index, 'error': cell.get('error')}
    status = checkpoint.get('status')
    if status == 'HOLD':
        return {'event': 'HOLD', 'cell': checkpoint.get('completed_cells', 0),
                'reason': checkpoint.get('hold_reason')}
    if status == 'failed':
        return {'event': 'failure', 'cell': checkpoint.get('completed_cells', 0),
                'error': checkpoint.get('error')}
    if status == 'complete':
        return {'event': 'complete', 'completed_cells': checkpoint.get('completed_cells', 0)}
    return None


async def wait_for_checkpoint(path, after_cell=0, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            checkpoint = None
        except (OSError, json.JSONDecodeError):
            return {'event': 'failure', 'error': 'Checkpoint could not be read.'}
        if checkpoint is not None:
            event = next_wait_event(checkpoint, after_cell)
            if event:
                return event
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'event': 'timeout', 'after_cell': after_cell}
        await asyncio.sleep(min(WAIT_POLL_SECONDS, remaining))


def _cell_deadline_seconds(manifest, repeats, timeout):
    durations = sum(float(sample.get('audio_seconds', 0)) for sample in manifest['samples']) * repeats
    sessions = len(manifest['samples']) * repeats
    if not math.isfinite(durations):
        raise ValueError('Manifest audio durations must be finite.')
    # Account for audio playback, bounded per-session I/O, and local startup/
    # drain work while keeping a hard ceiling for every matrix cell.
    return min(1800.0, max(60.0, durations + sessions * timeout + 30.0))


def _validate_matrix_inputs(args, settings_loader=load_settings):
    variants = build_matrix(args.modes, args.maximums, args.silences, not args.skip_legacy)
    if not variants:
        raise ValueError('The selected filters produced no benchmark cells.')
    if (isinstance(args.repeats, bool) or not isinstance(args.repeats, int) or args.repeats < 1
            or isinstance(args.timeout, bool) or not isinstance(args.timeout, (int, float))
            or not math.isfinite(args.timeout) or args.timeout <= 0):
        raise ValueError('Replay requires positive --repeats and finite positive --timeout.')
    manifest = latency_benchmark.load_manifest(args.manifest)
    if len(manifest['samples']) * args.repeats > MAX_CAPTURED_SESSION_BUFFERS:
        raise ValueError(f'At most {MAX_CAPTURED_SESSION_BUFFERS} replay sessions per cell are supported.')
    for sample in manifest['samples']:
        duration = sample.get('audio_seconds', 0)
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration < 0):
            raise ValueError('Manifest audio durations must be finite and non-negative.')
    settings = settings_loader()
    if not settings.latency_observability:
        raise RuntimeError('Enable latency_observability in the local settings before running the matrix.')
    for variant in variants:
        validated = {'segmentation_mode': variant['segmentation_mode']}
        if variant['speech_max_seconds'] is not None:
            validated['speech_max_seconds'] = variant['speech_max_seconds']
            validated['speech_silence_seconds'] = variant['speech_silence_seconds']
        replace(settings, **validated)
    _cell_deadline_seconds(manifest, args.repeats, args.timeout)
    return variants, manifest, settings


def _gpu_peak_memory(gpu_report):
    return sum(float(board.get('peak_memory_used_mib', 0))
               for board in gpu_report.get('boards', []))


async def _wait_for_gpu_probe(gpu, task):
    deadline = time.monotonic() + GPU_PROBE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if gpu.report.get('samples', 0) or task.done():
            return gpu.report.get('available', False)
        await asyncio.sleep(0.05)
    return False


async def _wait_for_server(server, task, timeout=STARTUP_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while not server.started:
        if task.done():
            await task
            raise RuntimeError('Local benchmark server stopped during startup.')
        if time.monotonic() >= deadline:
            raise asyncio.TimeoutError('Local benchmark server startup timed out.')
        await asyncio.sleep(0.05)


async def _wait_until_inactive(app, timeout):
    deadline = time.monotonic() + timeout
    while app.state.active:
        if time.monotonic() >= deadline:
            raise RuntimeError('Replay session did not return to idle before the next cell.')
        await asyncio.sleep(0.05)


async def _cleanup_runner(server, server_task, engine, factory_patch,
                          reserved_listener, gpu_task):
    errors = []
    try:
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            try:
                if server_task.done():
                    await server_task
                else:
                    await asyncio.wait_for(server_task, timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    errors.append(error)
            except asyncio.CancelledError:
                pass
            except Exception as error:
                errors.append(error)
    except Exception as error:
        errors.append(error)
    finally:
        try:
            if factory_patch is not None:
                factory_patch.stop()
        except Exception as error:
            errors.append(error)
        finally:
            try:
                if engine is not None:
                    await engine.client.aclose()
            except Exception as error:
                errors.append(error)
            finally:
                try:
                    if reserved_listener is not None:
                        reserved_listener.close()
                except Exception as error:
                    errors.append(error)
                finally:
                    if gpu_task is not None:
                        if not gpu_task.done():
                            gpu_task.cancel()
                        try:
                            await gpu_task
                        except asyncio.CancelledError:
                            pass
                        except Exception as error:
                            errors.append(error)
    return errors


async def run_matrix(args, *, listener_reserver=reserve_listener,
                     engine_factory=RecordingEngine, settings_loader=load_settings):
    variants, manifest, settings = _validate_matrix_inputs(args, settings_loader)

    checkpoint_path = args.output
    checkpoint = {
        'schema_version': 1,
        'status': 'running',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'completed_cells': 0,
        'total_cells': len(variants),
        'cells': [],
        'gpu_usage': {'available': False, 'samples': 0},
        'quality_sidecars': [],
    }
    atomic_write_json(checkpoint_path, checkpoint)

    reserved_listener = None
    engine = None
    app = None
    server = None
    server_task = None
    factory_patch = None
    gpu = latency_benchmark.GPUUsage()
    gpu_task = None
    primary_error = False
    try:
        # Keep the reservation socket open and hand it directly to uvicorn. This
        # prevents a foreign service from being displaced or a startup race.
        reserved_listener = listener_reserver()
        gpu_task = asyncio.create_task(gpu.run())
        if not await _wait_for_gpu_probe(gpu, gpu_task):
            checkpoint.update(status='HOLD', hold_reason='GPU usage is unavailable.', gpu_usage=gpu.report)
            atomic_write_json(checkpoint_path, checkpoint)
            return checkpoint
        if _gpu_peak_memory(gpu.report) >= GPU_MEMORY_HOLD_MIB:
            reason = f'Peak total GPU memory use reached {GPU_MEMORY_HOLD_MIB} MiB.'
            checkpoint.update(status='HOLD', hold_reason=reason, gpu_usage=gpu.report)
            atomic_write_json(checkpoint_path, checkpoint)
            return checkpoint

        mutable_settings = SimpleNamespace(**asdict(settings))
        engine = engine_factory(mutable_settings)
        engine.quality_buffers.clear()
        app = create_app(engine=engine, settings=mutable_settings)
        session_limit = min(MAX_CAPTURED_SESSION_BUFFERS, len(manifest['samples']) * args.repeats)
        factory_patch = patch.object(
            backend_app_module, 'create_speech_buffer',
            _tracked_buffer_factory(engine, session_limit),
        )
        factory_patch.start()
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8765,
                                               log_level='error', access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[reserved_listener]))
        await _wait_for_server(server, server_task)

        cell_timeout = _cell_deadline_seconds(manifest, args.repeats, args.timeout)
        overall_deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS + cell_timeout * len(variants)
        for index, variant in enumerate(variants, start=1):
            if app.state.active:
                raise RuntimeError('A replay session is still active before the next cell.')
            mutable_settings.segmentation_mode = variant['segmentation_mode']
            if variant['speech_max_seconds'] is not None:
                mutable_settings.speech_max_seconds = variant['speech_max_seconds']
                mutable_settings.speech_silence_seconds = variant['speech_silence_seconds']
            engine.quality_segments.clear()
            engine.quality_buffers.clear()
            engine.quality_session_ordinal = 0

            cell = {
                'index': index,
                'name': variant['name'],
                'status': 'running',
                'configuration': cell_configuration(variant, mutable_settings, manifest),
                'report_path': str(checkpoint_path.parent / f'{index:02d}-{variant["name"]}.json'),
                'quality_path': str(checkpoint_path.parent / f'{index:02d}-{variant["name"]}.quality.json'),
            }
            checkpoint['cells'].append(cell)
            checkpoint['gpu_usage'] = gpu.report
            atomic_write_json(checkpoint_path, checkpoint)

            cell_report_path = Path(cell['report_path'])
            raw_browser_output = cell_report_path.with_suffix('.browser.json')
            replay_args = argparse.Namespace(
                manifest=args.manifest,
                url='ws://127.0.0.1:8765/listen',
                repeats=args.repeats,
                timeout=args.timeout,
                browser=args.browser,
                settings=SETTINGS_PATH,
                output=cell_report_path if args.browser else None,
            )
            try:
                remaining_overall = overall_deadline - time.monotonic()
                if remaining_overall <= 0:
                    raise asyncio.TimeoutError('Overall matrix deadline expired.')
                report = await asyncio.wait_for(
                    latency_benchmark.run(replay_args),
                    timeout=min(cell_timeout, remaining_overall),
                )
                await _wait_until_inactive(app, min(30.0, max(1.0, args.timeout)))
            finally:
                raw_browser_output.unlink(missing_ok=True)

            quality = _quality_sidecar(engine, report, variant, index)
            report.setdefault('definitions', {})['origin'] = _origin_definition(variant, quality, manifest)
            report['definitions']['playback_to_first_readable_ms'] = (
                'Elapsed browser performance time from the controlled sample audio playing event '
                'to the earliest trusted visible-caption timestamp for a rendered segment; null '
                'means no rendered caption was observed. This reference does not depend on the '
                'server speech-onset estimate.')
            _add_playback_summary(report)
            report['first_caption_per_session'] = first_caption_per_session(report.get('records', []))
            atomic_write_json(cell['report_path'], report)
            atomic_write_json(cell['quality_path'], quality)
            cell['numeric_summary'] = report.get('summary')
            cell['measurement_complete'] = report.get('measurement_complete', False)
            cell['effective_endpoint_modes'] = quality['effective_endpoint_modes']
            cell['report_path'] = str(Path(cell['report_path']).resolve())
            cell['quality_path'] = str(Path(cell['quality_path']).resolve())
            checkpoint['quality_sidecars'].append(cell['quality_path'])
            checkpoint['gpu_usage'] = gpu.report

            reason = hold_reason(report, gpu.report, variant, quality)
            if reason:
                cell.update(status='HOLD', hold_reason=reason)
                checkpoint.update(status='HOLD', completed_cells=index, hold_reason=reason)
                atomic_write_json(checkpoint_path, checkpoint)
                return checkpoint

            cell['status'] = 'complete'
            checkpoint['completed_cells'] = index
            atomic_write_json(checkpoint_path, checkpoint)

        checkpoint['status'] = 'complete'
        atomic_write_json(checkpoint_path, checkpoint)
        return checkpoint
    except asyncio.CancelledError:
        primary_error = True
        checkpoint['status'] = 'failed'
        checkpoint['error'] = 'CancelledError'
        if checkpoint['cells'] and checkpoint['cells'][-1].get('status') == 'running':
            checkpoint['cells'][-1].update(status='failed', error='CancelledError')
        checkpoint['gpu_usage'] = gpu.report
        atomic_write_json(checkpoint_path, checkpoint)
        raise
    except Exception as error:
        primary_error = True
        checkpoint['status'] = 'failed'
        checkpoint['error'] = type(error).__name__
        if checkpoint['cells'] and checkpoint['cells'][-1].get('status') == 'running':
            checkpoint['cells'][-1].update(status='failed', error=checkpoint['error'])
        checkpoint['gpu_usage'] = gpu.report
        atomic_write_json(checkpoint_path, checkpoint)
        raise
    finally:
        cleanup_errors = await _cleanup_runner(
            server, server_task, engine, factory_patch, reserved_listener, gpu_task)
        if cleanup_errors and not primary_error:
            checkpoint['status'] = 'failed'
            checkpoint['error'] = f'CleanupError: {type(cleanup_errors[0]).__name__}'
            atomic_write_json(checkpoint_path, checkpoint)
            raise RuntimeError(checkpoint['error'])


def _make_matrix_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--browser', action='store_true', help='Replay through the local browser capture and rendering path.')
    modes.add_argument('--live', action='store_true', help='Replay PCM frames through the local WebSocket service.')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'benchmarks' / 'baseline.json')
    parser.add_argument('--modes', nargs='+', choices=DEFAULT_MODES, default=list(DEFAULT_MODES),
                        help='Adaptive modes to include; legacy is included unless --skip-legacy is set.')
    parser.add_argument('--maximums', nargs='+', type=float, choices=MAXIMUMS, default=list(MAXIMUMS))
    parser.add_argument('--silences', nargs='+', type=float, choices=SILENCES, default=list(SILENCES))
    parser.add_argument('--skip-legacy', action='store_true')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=30, help='Per replay operation timeout in seconds.')
    parser.add_argument('--output', type=Path, default=DEFAULT_CHECKPOINT,
                        help='Atomic checkpoint JSON; cell reports and text sidecars are written beside it.')
    return parser


def _make_wait_parser():
    parser = argparse.ArgumentParser(prog='adaptive_benchmark.py wait',
                                     description='Wait for one matrix checkpoint event.')
    parser.add_argument('checkpoint', type=Path)
    parser.add_argument('--after-cell', type=int, default=0)
    parser.add_argument('--timeout', type=float, default=60)
    return parser


def supervise_worker(command, timeout, *, popen_factory=None, tree_terminator=None,
                     environment=None):
    """Wait for the owned worker, killing its process tree only after deadline."""
    popen_factory = subprocess.Popen if popen_factory is None else popen_factory
    tree_terminator = terminate_owned_process_tree if tree_terminator is None else tree_terminator
    options = {
        'cwd': str(ROOT),
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
    }
    if os.name != 'nt':
        options['start_new_session'] = True
    if environment is not None:
        options['env'] = environment
    process = popen_factory(command, **options)
    try:
        return {'timed_out': False, 'returncode': process.wait(timeout=timeout)}
    except KeyboardInterrupt:
        if process.poll() is None:
            tree_terminator(process)
            try:
                process.wait(timeout=WATCHDOG_TERMINATION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    tree_terminator(process)
                    if process.poll() is None:
                        process.kill()
        raise
    except subprocess.TimeoutExpired:
        if process.poll() is not None:
            return {'timed_out': False, 'returncode': process.returncode}
        tree_terminator(process)
        try:
            process.wait(timeout=WATCHDOG_TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if process.poll() is not None:
                return {'timed_out': True, 'returncode': process.returncode}
            tree_terminator(process)
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return {'timed_out': True, 'returncode': None}
        return {'timed_out': True, 'returncode': process.returncode}


def _worker_command(args):
    command = [sys.executable, '-m', 'scripts.adaptive_benchmark', '--worker']
    command.append('--browser' if args.browser else '--live')
    command.extend(['--manifest', str(args.manifest.resolve())])
    command.extend(['--modes', *args.modes])
    command.extend(['--maximums', *(_number(value) for value in args.maximums)])
    command.extend(['--silences', *(_number(value) for value in args.silences)])
    command.extend(['--repeats', str(args.repeats), '--timeout', str(args.timeout)])
    command.extend(['--output', str(args.output.resolve())])
    if args.skip_legacy:
        command.append('--skip-legacy')
    return command


def _mark_watchdog_deadline(checkpoint_path, total_cells):
    try:
        checkpoint = json.loads(Path(checkpoint_path).read_text(encoding='utf-8'))
        if not isinstance(checkpoint, dict):
            checkpoint = {}
    except (OSError, json.JSONDecodeError):
        checkpoint = {}
    checkpoint.setdefault('schema_version', 1)
    checkpoint.setdefault('completed_cells', 0)
    checkpoint.setdefault('cells', [])
    checkpoint['total_cells'] = total_cells
    checkpoint['status'] = 'failed'
    checkpoint['error'] = 'WatchdogDeadlineExceeded'
    if checkpoint['cells'] and checkpoint['cells'][-1].get('status') == 'running':
        checkpoint['cells'][-1].update(status='failed', error='WatchdogDeadlineExceeded')
    atomic_write_json(checkpoint_path, checkpoint)
    return checkpoint


def _worker_main(argv):
    args = _make_matrix_parser().parse_args(argv)
    try:
        checkpoint = asyncio.run(run_matrix(args))
    except (ValueError, RuntimeError, OSError, asyncio.TimeoutError):
        return 1
    return 0 if checkpoint['status'] in ('complete', 'HOLD') else 1


def _public_matrix_main(args):
    variants, manifest, _settings = _validate_matrix_inputs(args)
    per_cell_budget = _cell_deadline_seconds(manifest, args.repeats, args.timeout)
    absolute_budget = (STARTUP_TIMEOUT_SECONDS + per_cell_budget * len(variants)
                       + SHUTDOWN_TIMEOUT_SECONDS + WATCHDOG_ALLOWANCE_SECONDS)
    worker_wait_budget = max(1.0, absolute_budget - WATCHDOG_TERMINATION_TIMEOUT_SECONDS)
    environment = os.environ.copy()
    environment[WORKER_MARKER_ENV] = '1'
    result = supervise_worker(
        _worker_command(args), worker_wait_budget, environment=environment)
    if result['timed_out']:
        checkpoint = _mark_watchdog_deadline(args.output, len(variants))
        print(json.dumps({
            'status': 'failed',
            'error': 'WatchdogDeadlineExceeded',
            'completed_cells': checkpoint['completed_cells'],
            'total_cells': checkpoint['total_cells'],
            'checkpoint': str(args.output.resolve()),
        }))
        return 1

    try:
        checkpoint = json.loads(args.output.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        checkpoint = {'status': 'failed', 'completed_cells': 0, 'total_cells': len(variants)}
    if result['returncode'] != 0 and checkpoint.get('status') != 'failed':
        checkpoint['status'] = 'failed'
        checkpoint['error'] = 'WorkerExitedWithError'
        if checkpoint.get('cells') and checkpoint['cells'][-1].get('status') == 'running':
            checkpoint['cells'][-1].update(status='failed', error='WorkerExitedWithError')
        atomic_write_json(args.output, checkpoint)
    summary = {
        'status': checkpoint.get('status', 'failed'),
        'completed_cells': checkpoint.get('completed_cells', 0),
        'total_cells': checkpoint.get('total_cells', len(variants)),
        'checkpoint': str(args.output.resolve()),
    }
    if summary['status'] == 'failed':
        summary['error'] = checkpoint.get('error', 'WorkerExitedWithError')
    print(json.dumps(summary))
    if result['returncode'] != 0 or checkpoint.get('status') not in ('complete', 'HOLD'):
        return 1
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'wait':
        args = _make_wait_parser().parse_args(argv[1:])
        if args.after_cell < 0 or not 0 <= args.timeout <= 60:
            raise SystemExit('--after-cell must be non-negative and --timeout must be between 0 and 60 seconds.')
        event = asyncio.run(wait_for_checkpoint(args.checkpoint, args.after_cell, args.timeout))
        print(json.dumps(event, ensure_ascii=False))
        return 1 if event['event'] == 'failure' else 0

    if argv and argv[0] == '--worker':
        if os.environ.get(WORKER_MARKER_ENV) != '1':
            raise SystemExit('--worker is reserved for the bounded runner.')
        return _worker_main(argv[1:])

    args = _make_matrix_parser().parse_args(argv)
    try:
        return _public_matrix_main(args)
    except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    raise SystemExit(main())
