from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from backend.audio import AdaptiveSpeechBuffer, SAMPLE_RATE, SpeechBuffer, create_speech_buffer
from backend.config import Settings


SETTINGS = Settings(token='adaptive-unit-test', segmentation_mode='adaptive_rms',
                    speech_max_seconds=2.5, speech_silence_seconds=0.3)


def raw(windows, amplitude=4000):
    return np.full(windows * 512, amplitude, dtype='<i2').tobytes()


def replay(buffer, pcm, packet_samples=2048):
    results = []
    for start in range(0, len(pcm), packet_samples * 2):
        results.extend(buffer.feed_many(pcm[start:start + packet_samples * 2]))
    tail = buffer.flush()
    if tail is not None:
        results.append(tail)
    return results


def test_legacy_settings_keep_the_original_buffer_and_ignore_adaptive_tuning():
    buffer = create_speech_buffer(replace(SETTINGS, segmentation_mode='legacy'))
    assert isinstance(buffer, SpeechBuffer)
    # Explicitly changing adaptive fields must not silently enable the feature.
    assert not buffer.feed_many(raw(25))
    assert not buffer.feed_many(raw(10, 0))  # 320 ms < legacy 450 ms.
    assert buffer.feed_many(raw(5, 0))


@pytest.mark.parametrize('field,value', [
    ('segmentation_mode', 'automatic'), ('speech_max_seconds', float('nan')),
    ('speech_max_seconds', True), ('speech_min_seconds', 0),
    ('speech_silence_seconds', 0), ('speech_overlap_seconds', -0.1),
    ('speech_overlap_seconds', 0.6), ('speech_min_seconds', 2.1),
])
def test_invalid_or_unbounded_segmentation_settings_fail_before_capture(field, value):
    with pytest.raises(ValueError):
        replace(SETTINGS, **{field: value})


def test_maximum_must_fit_minimum_speech_and_retained_context():
    with pytest.raises(ValueError, match='leave room'):
        replace(SETTINGS, speech_max_seconds=1, speech_min_seconds=.6)


def test_silence_endpoint_waits_for_configured_pause_and_retains_preroll():
    buffer = AdaptiveSpeechBuffer(SETTINGS)
    for _ in range(100):
        assert not buffer.feed_many(raw(31, 0))
    assert len(buffer.pre_roll) <= SAMPLE_RATE * .3
    assert len(buffer.pending) < 512
    assert not buffer.feed_many(raw(16))  # 512 ms of speech.
    assert not buffer.feed_many(raw(9, 0))
    result, = buffer.feed_many(raw(1, 0))  # First 32 ms decision beyond 300 ms.
    assert len(result.samples) == (9 + 16 + 10) * 512
    np.testing.assert_array_equal(result.samples[9 * 512:25 * 512],
                                  np.full(16 * 512, 4000 / 32768, dtype=np.float32))


def test_minimum_new_speech_is_configurable_and_shorter_noise_is_discarded():
    pcm = raw(5) + raw(12, 0)  # 160 ms utterance + endpoint silence.
    assert not replay(AdaptiveSpeechBuffer(SETTINGS), pcm)
    cues = replay(AdaptiveSpeechBuffer(replace(SETTINGS, speech_min_seconds=.1)), pcm)
    assert len(cues) == 1


def test_continuous_speech_has_exact_overlap_no_gaps_and_keeps_short_final_continuation():
    settings = replace(SETTINGS, speech_max_seconds=1.0)
    # A distinctive nonzero ramp detects dropped/reordered/duplicated samples.
    samples = (1000 + np.arange(61 * 512 + 127) % 10000).astype('<i2')
    cues = replay(AdaptiveSpeechBuffer(settings), samples.tobytes(), packet_samples=16000)
    assert len(cues) == 3
    assert all(len(cue.samples) <= SAMPLE_RATE for cue in cues)
    overlap = 2560  # Explicit 160 ms retained at each forced boundary.
    np.testing.assert_array_equal(cues[0].samples[-overlap:], cues[1].samples[:overlap])
    np.testing.assert_array_equal(cues[1].samples[-overlap:], cues[2].samples[:overlap])
    reconstructed = np.concatenate([cues[0].samples] + [cue.samples[overlap:] for cue in cues[1:]])
    np.testing.assert_array_equal(reconstructed, samples.astype(np.float32) / 32768)
    # Last continuation is below the 250 ms minimum; dropping it would clip speech.
    assert 0 < len(cues[-1].samples) - overlap < .25 * SAMPLE_RATE


def test_decisions_are_packet_size_independent_and_eof_keeps_subwindow_samples():
    pcm = raw(100) + raw(15, 0) + raw(12) + bytes(154)
    first = replay(AdaptiveSpeechBuffer(SETTINGS), pcm, packet_samples=2048)
    second = replay(AdaptiveSpeechBuffer(SETTINGS), pcm, packet_samples=777)
    assert len(first) == len(second)
    for left, right in zip(first, second):
        np.testing.assert_array_equal(left.samples, right.samples)
    assert len(first[-1].samples) % 512 == 77


def test_copied_overlap_alone_never_becomes_a_second_subtitle():
    settings = replace(SETTINGS, speech_max_seconds=1.0)
    cues = replay(AdaptiveSpeechBuffer(settings), raw(31) + raw(20, 0))
    assert len(cues) == 1


@pytest.mark.parametrize('extra_quiet_windows,expected_cues', [(0, 2), (2, 1)])
def test_forced_cut_in_quiet_keeps_cumulative_pause_for_short_speech_minimum(extra_quiet_windows, expected_cues):
    settings = replace(SETTINGS, speech_max_seconds=1)
    # The cap falls after 288 ms quiet. Two more decisions complete a real
    # 352 ms pause, so subsequent 160 ms speech is a new sub-minimum utterance.
    pcm = raw(22) + raw(9 + extra_quiet_windows, 0) + raw(5) + raw(10, 0)
    cues = replay(AdaptiveSpeechBuffer(settings), pcm)
    assert len(cues) == expected_cues


@pytest.mark.parametrize('tail_samples', [1, 127, 511])
@pytest.mark.parametrize('overlap_seconds', [0, .16])
def test_eof_immediately_after_forced_cut_preserves_new_speech(tail_samples, overlap_seconds):
    settings = replace(SETTINGS, speech_max_seconds=1, speech_overlap_seconds=overlap_seconds)
    samples = np.full(31 * 512 + tail_samples, 4000, dtype='<i2')
    cues = replay(AdaptiveSpeechBuffer(settings), samples.tobytes(), packet_samples=16000)
    assert len(cues) == 2
    assert len(cues[1].samples) == int(overlap_seconds * SAMPLE_RATE) + tail_samples
    reconstructed = np.concatenate((cues[0].samples, cues[1].samples[int(overlap_seconds * SAMPLE_RATE):]))
    np.testing.assert_array_equal(reconstructed, samples.astype(np.float32) / 32768)


def test_speech_classifier_decision_controls_gate_and_failure_preserves_audio():
    # These injected labels establish endpoint behavior, not real speech quality.
    assert not replay(AdaptiveSpeechBuffer(SETTINGS, detector=lambda samples: False), raw(100))
    assert replay(AdaptiveSpeechBuffer(SETTINGS, detector=lambda samples: True), raw(16, 0))

    calls = 0

    def fails_during_speech(samples):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RuntimeError('endpoint detector failed')
        return True

    buffer = AdaptiveSpeechBuffer(SETTINGS, detector=fails_during_speech)
    cues = replay(buffer, raw(16) + raw(10, 0))
    expected = replay(AdaptiveSpeechBuffer(SETTINGS), raw(16) + raw(10, 0))
    assert buffer.mode == 'adaptive_rms'
    assert calls == 5
    np.testing.assert_array_equal(cues[0].samples, expected[0].samples)


def test_detector_initialization_failure_falls_back_without_changing_legacy(monkeypatch):
    import backend.audio as audio

    def unavailable():
        raise RuntimeError('missing VAD runtime')

    monkeypatch.setattr(audio, 'SpeechDetector', unavailable)
    buffer = create_speech_buffer(replace(SETTINGS, segmentation_mode='adaptive_vad'))
    assert buffer.mode == 'adaptive_rms'
    assert replay(buffer, raw(16) + raw(10, 0))


def test_adaptive_trace_backdates_packets_without_mixing_clock_domains(monkeypatch):
    import backend.audio as audio

    monkeypatch.setattr(audio, 'time', SimpleNamespace(monotonic=lambda: 10.0))
    monkeypatch.setattr(audio, 'clock_ms', lambda: 100_000.0)
    buffer = AdaptiveSpeechBuffer(SETTINGS, observe=True)
    buffer.feed_many(raw(4, 0))
    buffer.feed_many(raw(8))
    assert buffer.started_at == pytest.approx(10.0 - .256 - .128)
    assert buffer.trace['speech_started_ms'] == 99_744.0
    assert buffer.trace['speech_detected_ms'] == 100_000.0
    buffer.feed_many(raw(10, 0))
    # Retained pre-roll affects stale scheduling only; onset excludes pre-roll.
    assert buffer.trace is None
