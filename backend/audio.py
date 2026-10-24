from collections import deque
from dataclasses import dataclass
import logging
import math
import time

import numpy as np

from .observability import clock_ms

SAMPLE_RATE = 16000
MAX_FRAME_BYTES = SAMPLE_RATE * 2  # At most one second per WebSocket message.
logger = logging.getLogger('subtitles')


@dataclass
class Utterance:
    samples: np.ndarray
    started_at: float
    trace: dict | None = None


class SpeechBuffer:
    """Energy gate plus pre-roll; Whisper's VAD performs the final speech filtering."""

    def __init__(self, max_seconds=5.0, silence_seconds=0.45, threshold=0.008, observe=False):
        self.max_samples = int(max_seconds * SAMPLE_RATE)
        self.silence_limit = int(silence_seconds * SAMPLE_RATE)
        self.threshold = threshold
        self.pre_roll = deque(maxlen=3)
        self.frames = []
        self.total = 0
        self.silence = 0
        self.voiced = 0
        self.started_at = 0.0
        self.observe = observe
        self.trace = None

    def feed(self, raw: bytes) -> Utterance | None:
        if not raw or len(raw) % 2 or len(raw) > MAX_FRAME_BYTES:
            raise ValueError('Expected a non-empty PCM16 frame of at most one second.')
        samples = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
        speech = float(np.sqrt(np.mean(samples ** 2))) >= self.threshold
        if not self.frames:
            if not speech:
                self.pre_roll.append(samples)
                return None
            self.frames = list(self.pre_roll)
            self.total = sum(map(len, self.frames))
            self.pre_roll.clear()
            self.started_at = time.monotonic() - self.total / SAMPLE_RATE
            if self.observe:
                detected_at = clock_ms()
                self.trace = {
                    'speech_started_ms': round(detected_at - len(samples) / SAMPLE_RATE * 1000, 3),
                    'speech_detected_ms': round(detected_at, 3),
                }
        self.frames.append(samples)
        self.total += len(samples)
        self.voiced += len(samples) if speech else 0
        self.silence = 0 if speech else self.silence + len(samples)
        if self.total >= self.max_samples or self.silence >= self.silence_limit:
            result = self.flush()
            return result
        return None

    def flush(self) -> Utterance | None:
        result = None
        if self.frames and self.voiced >= SAMPLE_RATE * 0.25:
            trace = self.trace
            if trace is not None:
                trace['segmented_ms'] = round(clock_ms(), 3)
            result = Utterance(np.concatenate(self.frames), self.started_at, trace)
        self.frames = []
        self.total = self.silence = self.voiced = 0
        self.pre_roll.clear()
        self.trace = None
        return result

    def feed_many(self, raw: bytes) -> list[Utterance]:
        result = self.feed(raw)
        return [result] if result is not None else []


class SpeechDetector:
    """CPU Silero decisions over a bounded lookback, without shared recurrent state."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self.model = get_vad_model()
        self.history = np.empty(0, dtype=np.float32)
        self.speaking = False

    def __call__(self, samples):
        # Use the packaged callable rather than private ONNX input/state names.
        self.history = np.concatenate((self.history, samples))[-(SAMPLE_RATE * 2 // 512 * 512):]
        probability = float(np.asarray(self.model(self.history.copy())).reshape(-1)[-1])
        if not math.isfinite(probability):
            raise ValueError('Invalid speech probability.')
        self.speaking = probability >= (0.35 if self.speaking else 0.5)
        return self.speaking


class AdaptiveSpeechBuffer:
    """Sample-count endpoints with up to 300 ms pre-roll and forced-cut overlap.

    Minimum duration counts newly detected speech, excluding copied overlap.
    Fixed 32 ms decisions make boundaries independent of WebSocket packet sizes.
    """

    window_samples = 512
    pre_roll_samples = 4608  # 288 ms: whole decisions, at most 300 ms.

    def __init__(self, settings, observe=False, detector=None):
        self.mode = settings.segmentation_mode
        self.max_samples = int(settings.speech_max_seconds * SAMPLE_RATE) // 512 * 512
        self.min_samples = math.ceil(settings.speech_min_seconds * SAMPLE_RATE)
        self.silence_limit = math.ceil(settings.speech_silence_seconds * SAMPLE_RATE)
        self.overlap_samples = int(settings.speech_overlap_seconds * SAMPLE_RATE) // 512 * 512
        self.detector = detector
        self.observe = observe
        self.pending = np.empty(0, dtype=np.float32)
        self.pre_roll = np.empty(0, dtype=np.float32)
        self.frames = []
        self.total = self.voiced = self.silence = 0
        self.started_at = 0.0
        self.trace = None
        self.continuation = False

    def _speech(self, samples):
        if self.detector is not None:
            try:
                decision_samples = np.pad(samples, (0, self.window_samples - len(samples)))
                return self.detector(decision_samples)
            except Exception:
                # Preserve all buffered audio and continue with the energy gate.
                logger.warning('Speech endpoint detector unavailable; using adaptive RMS.')
                self.detector = None
                self.mode = 'adaptive_rms'
        return float(np.sqrt(np.mean(samples ** 2))) >= 0.008

    def feed_many(self, raw: bytes) -> list[Utterance]:
        if not raw or len(raw) % 2 or len(raw) > MAX_FRAME_BYTES:
            raise ValueError('Expected a non-empty PCM16 frame of at most one second.')
        samples = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
        self.pending = np.concatenate((self.pending, samples))
        results = []
        # Backdate within this completed packet in each clock's own domain.
        runtime_end, trace_end = time.monotonic(), clock_ms() if self.observe else 0
        while len(self.pending) >= self.window_samples:
            window, self.pending = self.pending[:512], self.pending[512:]
            remaining_seconds = len(self.pending) / SAMPLE_RATE
            result = self._feed_window(window, runtime_end - remaining_seconds,
                                       trace_end - remaining_seconds * 1000)
            if result is not None:
                results.append(result)
        return results

    def _feed_window(self, samples, runtime_end, trace_end):
        speech = self._speech(samples)
        if not self.frames:
            if not speech:
                self.pre_roll = np.concatenate((self.pre_roll, samples))[-self.pre_roll_samples:]
                self.silence += len(samples)
                if self.silence >= self.silence_limit:
                    self.continuation = False
                return None
            self.frames = [self.pre_roll]
            self.total = len(self.pre_roll)
            self.pre_roll = np.empty(0, dtype=np.float32)
            self.started_at = runtime_end - (self.total + len(samples)) / SAMPLE_RATE
            if self.observe:
                self.trace = {'speech_started_ms': round(trace_end - len(samples) / SAMPLE_RATE * 1000, 3),
                              'speech_detected_ms': round(clock_ms(), 3)}
        self.frames.append(samples)
        self.total += len(samples)
        self.voiced += len(samples) if speech else 0
        self.silence = 0 if speech else self.silence + len(samples)
        if self.total >= self.max_samples:
            return self._finish('maximum')
        if self.silence >= self.silence_limit:
            return self._finish('silence')
        return None

    def _finish(self, reason):
        samples = np.concatenate(self.frames) if self.frames else np.empty(0, dtype=np.float32)
        result = None
        if self.voiced >= self.min_samples or (self.continuation and self.voiced > 0):
            if self.trace is not None:
                self.trace.update(segmented_ms=round(clock_ms(), 3), endpoint=reason,
                                  endpoint_mode=self.mode)
            result = Utterance(samples, self.started_at, self.trace)
        retained = self.overlap_samples if reason == 'maximum' else self.pre_roll_samples
        self.pre_roll = samples[-retained:].copy() if retained else np.empty(0, dtype=np.float32)
        self.frames = []
        self.total = self.voiced = 0
        self.silence = self.silence if reason == 'maximum' else 0
        self.trace = None
        self.continuation = reason == 'maximum' and result is not None and self.silence < self.silence_limit
        return result

    def flush(self) -> Utterance | None:
        # Classify an EOF tail with detector-only padding, keeping original PCM.
        # This also retains a short continuation immediately after a forced cut.
        result = None
        if len(self.pending):
            result = self._feed_window(self.pending, time.monotonic(),
                                       clock_ms() if self.observe else 0)
        result = result or self._finish('source_eof')
        self.pending = self.pre_roll = np.empty(0, dtype=np.float32)
        return result


def create_speech_buffer(settings, observe=False):
    if settings.segmentation_mode == 'legacy':
        return SpeechBuffer(observe=observe)
    detector = None
    if settings.segmentation_mode == 'adaptive_vad':
        try:
            detector = SpeechDetector()
        except Exception:
            logger.warning('Speech endpoint detector unavailable; using adaptive RMS.')
    buffer = AdaptiveSpeechBuffer(settings, observe=observe, detector=detector)
    if detector is None:
        buffer.mode = 'adaptive_rms'
    return buffer
