"""Run a local Japanese sample through exactly the live inference pipeline."""
import argparse
import asyncio
from collections import deque
import json
import time
from pathlib import Path

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
    parser.add_argument('audio', nargs='?')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--live', action='store_true', help='Replay PCM frames through the live service')
    modes.add_argument('--browser', action='store_true', help='Replay through isolated Chrome capture and rendering')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--url', default='ws://127.0.0.1:8765/listen')
    parser.add_argument('--settings', type=Path, help='Local pairing settings; never printed')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.live or args.browser:
        from scripts.latency_benchmark import run
        try:
            report = asyncio.run(run(args))
        except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as error:
            raise SystemExit(str(error)) from None
        output = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding='utf-8')
            print(args.output)
        else:
            print(output)
    elif args.audio:
        asyncio.run(main(args.audio))
    else:
        parser.error('Supply an audio file, --live --manifest, or --browser --manifest.')
