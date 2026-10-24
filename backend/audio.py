from collections import deque
from dataclasses import dataclass
import time

import numpy as np

from .observability import clock_ms

SAMPLE_RATE = 16000
MAX_FRAME_BYTES = SAMPLE_RATE * 2  # At most one second per WebSocket message.


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
