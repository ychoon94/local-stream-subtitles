"""Replay fixed PCM frames through the real service and summarize numeric events."""
import asyncio
from contextlib import suppress
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlparse
import wave

from backend.audio import SAMPLE_RATE
from backend.config import load_settings
from scripts.process_cleanup import terminate_owned_subprocess_tree

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'chrome-extension://' + 'a' * 32
METRICS = ('speech_to_segment_ms', 'queue_ms', 'lock_ms', 'asr_ms', 'translation_ms',
           'server_total_ms', 'delivery_ms', 'display_ms', 'first_readable_ms', 'final_ms',
           'clock_uncertainty_ms', 'delivery_lower_ms', 'delivery_upper_ms',
           'first_readable_lower_ms', 'first_readable_upper_ms', 'final_lower_ms', 'final_upper_ms')


def distribution(values):
    """Nearest-rank percentiles. Missing observations stay null, never zero."""
    values = sorted(v for v in values if isinstance(v, (int, float))
                    and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
    if not values:
        return {'count': 0, 'p50': None, 'p95': None}
    return {'count': len(values),
            'p50': round(values[math.ceil(len(values) * .50) - 1], 3),
            'p95': round(values[math.ceil(len(values) * .95) - 1], 3)}


def calibrate_clock(client_sent, server_received, server_sent, client_received, resolution_ms=0):
    uncertainty = ((client_received - client_sent) - (server_sent - server_received)) / 2
    if not all(math.isfinite(v) for v in (client_sent, server_received, server_sent, client_received, resolution_ms)) or uncertainty < 0 or resolution_ms < 0:
        raise ValueError('Invalid clock calibration.')
    return {'offset_ms': ((server_received - client_sent) + (server_sent - client_received)) / 2,
            'uncertainty_ms': uncertainty + resolution_ms}


def load_manifest(path):
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('sample_rate') != SAMPLE_RATE:
        raise ValueError('Expected version 1, mono 16 kHz PCM baseline manifest.')
    if not manifest.get('samples'):
        raise ValueError('Manifest has no samples.')
    sample_ids = [sample.get('id') for sample in manifest['samples']]
    if (any(not isinstance(identifier, str) or not identifier.strip() for identifier in sample_ids)
            or len(set(sample_ids)) != len(sample_ids)):
        raise ValueError('Manifest requires unique non-empty sample IDs.')
    for sample in manifest['samples']:
        target = (path.parent / sample['path']).resolve()
        if hashlib.sha256(target.read_bytes()).hexdigest() != sample['sha256']:
            raise ValueError(f"Checksum mismatch: {sample['id']}")
        with wave.open(str(target), 'rb') as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, SAMPLE_RATE, 'NONE'):
                raise ValueError(f"Expected mono PCM16 at 16 kHz: {sample['id']}")
            pcm = stream.readframes(stream.getnframes())
        if hashlib.sha256(pcm).hexdigest() != sample['pcm_sha256']:
            raise ValueError(f"PCM checksum mismatch: {sample['id']}")
        sample['absolute_path'] = str(target)
    return manifest


def replay_frames(pcm, frame_samples=2048):
    if not 1 <= frame_samples <= SAMPLE_RATE or not pcm or len(pcm) % 2:
        raise ValueError('Replay needs PCM16 and frames of at most one second.')
    for start in range(0, len(pcm), frame_samples * 2):
        end = min(start + frame_samples * 2, len(pcm))
        yield end / (2 * SAMPLE_RATE), pcm[start:end]


def summarize(records):
    observations, totals = [], {}
    for record in records:
        segments = {}
        for event in record.get('events', []):
            segment = segments.setdefault(event['segment_id'], {})
            segment.update(event.get('timings', {}))
        observations.extend(segments.values())
        for key, value in record.get('counters', {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    rendered_count = sum(1 for row in observations if row.get('first_readable_ms') is not None)
    return {'latency_ms': {key: distribution(row.get(key) for row in observations) for key in METRICS},
            'client_delivery_ms': distribution(v for record in records for v in record.get('client_delivery_ms', [])),
            'replay_lateness_ms': distribution(v for record in records for v in record.get('replay_lateness_ms', [])),
            'counters': totals,
            'display_coverage': {'emitted': totals.get('emitted', 0), 'rendered': rendered_count,
                                 'unobserved': max(0, totals.get('emitted', 0) - rendered_count)}}


class GPUUsage:
    """Sample total board usage, including other processes, without NVML dependencies."""
    def __init__(self):
        self.report = {'available': False, 'samples': 0, 'scope': 'whole GPU, including desktop/other processes'}

    async def run(self):
        while True:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    'nvidia-smi', '--query-gpu=index,name,memory.total,memory.used,utilization.gpu',
                    '--format=csv,noheader,nounits', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3)
                if process.returncode:
                    raise ValueError('GPU query failed')
                boards = []
                previous = {b['index']: b for b in self.report.get('boards', [])}
                for line in stdout.decode().splitlines():
                    index, name, total, used, utilization = [part.strip() for part in line.split(',')]
                    board = {'index': int(index), 'name': name, 'memory_total_mib': float(total),
                             'memory_used_mib': float(used), 'utilization_percent': float(utilization)}
                    old = previous.get(board['index'], {})
                    board['peak_memory_used_mib'] = max(board['memory_used_mib'], old.get('peak_memory_used_mib', 0))
                    board['peak_utilization_percent'] = max(board['utilization_percent'], old.get('peak_utilization_percent', 0))
                    boards.append(board)
                self.report.update(available=True, samples=self.report['samples'] + 1, boards=boards)
            except asyncio.CancelledError:
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            except (OSError, ValueError, asyncio.TimeoutError):
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
                self.report.update(available=False,
                                   unavailable_reason='nvidia-smi unavailable or unsupported counters')
                return
            await asyncio.sleep(1)


async def replay_sample(url, token, sample, frame_samples=2048, timeout=60):
    from websockets.asyncio.client import connect

    record = {'sample_id': sample['id'], 'sha256': sample['sha256'], 'events': [],
              'client_delivery_ms': [], 'replay_lateness_ms': [], 'counters': {}}
    with wave.open(sample['absolute_path'], 'rb') as stream:
        pcm = stream.readframes(stream.getnframes())
    async with connect(url, origin=ORIGIN, max_size=65536, proxy=None) as socket:
        await socket.send(json.dumps({'token': token, 'mode': 'context', 'observe': True}))
        ready = json.loads(await asyncio.wait_for(socket.recv(), timeout))
        if ready.get('type') != 'ready' or not ready.get('observability'):
            raise RuntimeError('Replay needs an idle server with latency_observability enabled.')
        record['profile'] = ready.get('profile')
        calibration = None
        for _ in range(3):
            sent = time.perf_counter() * 1000
            await socket.send(json.dumps({'type': 'clock_sync', 'client_sent_ms': sent}))
            response = json.loads(await asyncio.wait_for(socket.recv(), timeout))
            resolution_ms = response.get('clock_resolution_ms', 0) + time.get_clock_info('perf_counter').resolution * 1000
            candidate = calibrate_clock(sent, response['server_received_ms'], response['server_sent_ms'], time.perf_counter() * 1000, resolution_ms)
            if calibration is None or candidate['uncertainty_ms'] < calibration['uncertainty_ms']:
                calibration = candidate
        record['clock_uncertainty_ms'] = calibration['uncertainty_ms']

        async def receive():
            while True:
                message = json.loads(await socket.recv())
                received = time.perf_counter() * 1000
                if message['type'] == 'metrics':
                    if message.get('event'):
                        record['events'].append(message['event'])
                    record['counters'] = message.get('counters', record['counters'])
                elif message['type'] == 'subtitle' and message.get('telemetry'):
                    record['client_delivery_ms'].append(max(0, received + calibration['offset_ms'] - message['telemetry']['websocket_sent_ms']))
                elif message['type'] == 'benchmark_complete':
                    record['counters'] = message['counters']
                    return
                elif message['type'] == 'error':
                    raise RuntimeError(message.get('message', 'Replay failed.'))

        receiver = asyncio.create_task(receive())
        try:
            started = time.monotonic()
            for offset, frame in replay_frames(pcm, frame_samples):
                await asyncio.sleep(max(0, started + offset - time.monotonic()))
                if receiver.done():
                    await receiver
                    raise RuntimeError('Replay ended before audio completed.')
                record['replay_lateness_ms'].append(max(0, (time.monotonic() - started - offset) * 1000))
                await socket.send(frame)
            await socket.send(json.dumps({'type': 'benchmark_end'}))
            await asyncio.wait_for(receiver, timeout)
        finally:
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver
    return record


async def run(args):
    if not args.manifest or args.repeats < 1 or args.timeout <= 0:
        raise ValueError('Replay requires --manifest, positive --repeats and --timeout.')
    parsed = urlparse(args.url)
    if parsed.scheme != 'ws' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username or parsed.password:
        raise ValueError('Replay connects only to a local ws://127.0.0.1 or ws://localhost server.')
    manifest = load_manifest(args.manifest)
    gpu = GPUUsage()
    monitor = asyncio.create_task(gpu.run())
    try:
        if args.browser:
            raw_path = args.output.with_suffix('.browser.json') if args.output else ROOT / 'test-results/latency-browser.json'
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            process = await asyncio.create_subprocess_exec(
                'node', str(ROOT / 'scripts/benchmark-browser.cjs'), '--manifest', str(args.manifest.resolve()),
                '--output', str(raw_path.resolve()), '--repeats', str(args.repeats), '--url', args.url,
                '--timeout', str(args.timeout), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, stderr = await process.communicate()
            except BaseException:
                await terminate_owned_subprocess_tree(process)
                raise
            if process.returncode:
                raise RuntimeError(stderr.decode(errors='replace')[-2000:] or 'Browser replay failed.')
            records = json.loads(raw_path.read_text(encoding='utf-8'))['records']
        else:
            settings = json.loads(args.settings.read_text(encoding='utf-8-sig')) if args.settings else {'token': load_settings().token}
            records = []
            for iteration in range(args.repeats):
                for sample in manifest['samples']:
                    record = await replay_sample(args.url, settings['token'], sample, manifest.get('frame_samples', 2048), args.timeout)
                    record['iteration'] = iteration + 1
                    records.append(record)
        return {'schema_version': 1, 'mode': 'browser' if args.browser else 'websocket_pcm',
                'corpus_recipe': manifest.get('recipe'), 'repeats': args.repeats,
                'measurement_complete': all(record.get('measurement_complete', True) for record in records),
                'definitions': {'percentiles': 'nearest rank; each segment counts once per metric',
                                'origin': 'estimated onset of first energy-positive PCM frame at server',
                                'readable': 'foreground animation frame, CSS-visible caption box intersects frame viewport, cumulative opacity >= 0.99',
                                'final': 'same visible caption as first-readable in final-only pipeline',
                                'missing': 'null means unobserved; WebSocket replay has no display measurements'},
                'summary': summarize(records),
                'per_sample': {sample['id']: summarize([r for r in records if r['sample_id'] == sample['id']])
                               for sample in manifest['samples']},
                'gpu': gpu.report, 'records': records}
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
