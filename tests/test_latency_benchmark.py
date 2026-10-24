import asyncio
import hashlib
import json
import socket
import threading
import time
import wave

import numpy as np
import pytest
import uvicorn

from backend.app import create_app
from backend.audio import SpeechBuffer
from backend.config import Settings
from backend.engine import SubtitleEngine, Transcription
from scripts.latency_benchmark import calibrate_clock, distribution, load_manifest, replay_frames, replay_sample, summarize


def test_percentiles_merge_render_updates_without_counting_a_caption_twice():
    records = [{'events': [
        {'segment_id': 2, 'outcome': 'dropped_queue', 'timings': {'asr_ms': None}},
        {'segment_id': 1, 'outcome': 'emitted', 'timings': {'asr_ms': 50, 'translation_ms': 20}},
        {'segment_id': 1, 'outcome': 'rendered', 'timings': {'asr_ms': 50, 'first_readable_ms': 1000, 'final_ms': 1000}},
        {'segment_id': 3, 'outcome': 'emitted', 'timings': {'asr_ms': 90, 'translation_ms': 0}},
    ], 'counters': {'segmented': 3, 'emitted': 2, 'dropped_queue': 1}}]
    report = summarize(records)
    assert report['latency_ms']['asr_ms'] == {'count': 2, 'p50': 50, 'p95': 90}
    assert report['latency_ms']['translation_ms'] == {'count': 2, 'p50': 0, 'p95': 20}
    assert report['latency_ms']['first_readable_ms'] == {'count': 1, 'p50': 1000, 'p95': 1000}
    assert report['latency_ms']['delivery_ms'] == {'count': 0, 'p50': None, 'p95': None}
    assert report['counters']['dropped_queue'] == 1
    assert report['display_coverage'] == {'emitted': 2, 'rendered': 1, 'unobserved': 1}
    assert distribution([float('nan'), None, -1, True])['count'] == 0


def test_clock_calibration_excludes_server_processing_and_handles_separate_origins():
    # Browser starts at 10s, server at 100s; 2ms each way and 5ms processing.
    result = calibrate_clock(10_000, 100_002, 100_007, 10_009)
    assert result == {'offset_ms': 90_000, 'uncertainty_ms': 2}
    assert 10_015 + result['offset_ms'] == 100_015
    with pytest.raises(ValueError):
        calibrate_clock(100, 1000, 1020, 101)


def test_pcm_replay_schedule_is_byte_exact_and_timestamps_completed_frames():
    pcm = bytes(range(256)) * 33  # Two full frames and a short tail.
    frames = list(replay_frames(pcm))
    assert b''.join(frame for _, frame in frames) == pcm
    assert [len(frame) for _, frame in frames] == [4096, 4096, 256]
    assert [offset for offset, _ in frames] == [0.128, 0.256, 0.264]
    assert list(replay_frames(pcm)) == frames


def test_observing_does_not_change_segmentation_samples_or_boundaries():
    observed, legacy = SpeechBuffer(observe=True), SpeechBuffer()
    frames = [bytes(4096)] * 6 + [np.full(2048, 5000, dtype='<i2').tobytes()] * 46 + [bytes(4096)] * 6
    observed_cues, legacy_cues = [], []
    for frame in frames:
        first, second = observed.feed(frame), legacy.feed(frame)
        if first:
            observed_cues.append(first.samples)
        if second:
            legacy_cues.append(second.samples)
    assert len(observed_cues) == len(legacy_cues) == 2
    for first, second in zip(observed_cues, legacy_cues):
        np.testing.assert_array_equal(first, second)


def test_manifest_rejects_changed_audio_before_replay(tmp_path):
    target = tmp_path / 'sample.wav'
    with wave.open(str(target), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(bytes(4096))
    manifest = {'schema_version': 1, 'sample_rate': 16000, 'samples': [{
        'id': 'frozen', 'path': target.name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'pcm_sha256': hashlib.sha256(bytes(4096)).hexdigest()}]}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    assert load_manifest(path)['samples'][0]['absolute_path'] == str(target)
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='Checksum mismatch'):
        load_manifest(path)


@pytest.mark.parametrize('identifiers', [['japanese', 'japanese'], [''], [None], [3]])
def test_manifest_requires_unique_nonempty_sample_ids(tmp_path, identifiers):
    # ID-keyed reports must never combine distinct recordings or lose their identity.
    target = tmp_path / 'sample.wav'
    pcm = bytes(4096)
    with wave.open(str(target), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(pcm)
    manifest = {'schema_version': 1, 'sample_rate': 16000,
                'samples': [{'id': identifier, 'path': target.name,
                             'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                             'pcm_sha256': hashlib.sha256(pcm).hexdigest()}
                            for identifier in identifiers]}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='unique non-empty sample IDs'):
        load_manifest(path)


def test_real_websocket_replay_records_asr_and_no_fake_browser_latency(tmp_path):
    settings = Settings(token='replay-test-token', latency_observability=True)
    engine = SubtitleEngine(settings)
    engine.model = object()
    engine.transcribe = lambda *args: Transcription('Hello', 'en', .99)

    async def check_translation():
        pass

    engine.check_translation = check_translation
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(engine, settings), log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        pcm = np.concatenate((np.full(8192, 5000, dtype='<i2'), np.zeros(8192, dtype='<i2'))).tobytes()
        target = tmp_path / 'live.wav'
        with wave.open(str(target), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(pcm)
        record = asyncio.run(replay_sample(f'ws://127.0.0.1:{port}/listen', settings.token,
                                          {'id': 'live', 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                                           'absolute_path': str(target)}, timeout=5))
        assert record['counters']['segmented'] == record['counters']['emitted'] == 1
        assert record['counters']['dropped_queue'] == 0
        report = summarize([record])
        assert report['latency_ms']['asr_ms']['count'] == 1
        assert report['latency_ms']['translation_ms']['p50'] == 0
        assert report['latency_ms']['first_readable_ms']['p50'] is None
        assert report['client_delivery_ms']['count'] == 1
        assert settings.token not in json.dumps(record)
        assert 'Hello' not in json.dumps(record)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
