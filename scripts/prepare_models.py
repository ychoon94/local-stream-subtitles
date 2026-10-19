"""Download and warm the local speech model without sending audio off-device."""
import asyncio
import json
import os
import time

from backend.config import ROOT, initialize_settings

os.environ.setdefault('HF_HOME', str(ROOT / 'models' / 'hf-cache'))
os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')

from backend.engine import SubtitleEngine


async def main():
    settings = initialize_settings()
    engine = SubtitleEngine(settings)
    started = time.monotonic()
    print('Loading local speech model; the initial download can take several minutes.', flush=True)
    await asyncio.to_thread(engine.load)
    print(json.dumps({'speech_model': settings.whisper_model, 'device': settings.device,
                      'compute_type': settings.compute_type,
                      'load_seconds': round(time.monotonic() - started, 2)}), flush=True)
    await engine.check_translation()
    await engine.client.aclose()


if __name__ == '__main__':
    asyncio.run(main())
