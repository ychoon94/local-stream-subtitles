"""Run a local Japanese sample through exactly the live inference pipeline."""
import argparse
import asyncio
from collections import deque
import json
import time

from faster_whisper.audio import decode_audio

from backend.audio import SAMPLE_RATE, Utterance
from backend.config import load_settings
from backend.engine import SubtitleEngine


async def main(path):
    engine = SubtitleEngine(load_settings())
    await asyncio.to_thread(engine.load)
    await engine.warmup()
    samples = decode_audio(path, sampling_rate=SAMPLE_RATE)
    results = []
    history = deque(maxlen=3)
    # Include two runs after startup warmup. Keep the original Japanese and English
    # for manual assessment; never report this as a broad accuracy benchmark.
    for label in ('first', 'repeat'):
        started = time.monotonic()
        result = await engine.process(Utterance(samples, started), 'context', history)
        results.append({'run': label, 'audio_seconds': round(len(samples) / SAMPLE_RATE, 3),
                        'inference_seconds': round(time.monotonic() - started, 3), 'result': result})
    await engine.client.aclose()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('audio')
    asyncio.run(main(parser.parse_args().audio))
