"""Build the small, hash-pinned latency corpus locally; never redistribute audio."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import wave

import numpy as np

from backend.audio import SAMPLE_RATE

SOURCES = {
    'japanese': {
        'url': 'https://huggingface.co/datasets/FluidInference/JSUT-basic5000/resolve/cd13cc9c9fe70d8d4f62ffe6fbbb872aef79835c/basic5000/BASIC5000_4503.wav',
        'sha256': '2cbc696620062993d387a3a7bfe617947d2fd54c8d6a6b57c1881dab7931a236',
        'terms': 'JSUT: personal use/non-commercial research; see source README_en.txt. Not MIT.',
        'reference': '\u7fa8\u307e\u3057\u3044\u307b\u3069\u306e\u843d\u3061\u7740\u304d\u3076\u308a\u3067\u3042\u3063\u305f\u3002',
    },
    'english': {
        'url': 'https://huggingface.co/datasets/Narsil/asr_dummy/resolve/8d141c84e3f84c54cd7bbaa851d24edd0f559734/1.flac',
        'sha256': '30885601173f96b0d8ddd020dc959b055c6c1582b85a33e3fcab8c4b08ed94c2',
        'terms': 'LibriSpeech mirror; CC BY 4.0, Panayotov et al., https://www.openslr.org/12/',
        'reference': None,
    },
}
EXPECTED_PCM_SHA256 = {
    'japanese': 'be776f9e0e2a4f25d9d77d3a934bb3c5bfc28dbfece782e88f6c181bc1ff0b14',
    'english': '1c99901ddaa44ad2f2fcc352a36c40540b7085555d39dd3337d4398b79551bfc',
    'mixed': 'd67fcd7ea518885dfb50c51eca8f93f85a91b2d40857c4719a742389b72e689f',
    'game-audio': '5fcce566968cb4ed007cd4bd76f9a85a20dfc5b517719d826ec773b0efe3e623',
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build_cases(japanese, english):
    """Fixed pauses and analytic effects give identical input PCM on every replay."""
    pause = np.zeros(8192, dtype=np.float32)
    tail = np.zeros(16384, dtype=np.float32)
    mixed = np.concatenate((japanese, pause, english))
    time = np.arange(len(mixed), dtype=np.float64) / SAMPLE_RATE
    # Repeating short electronic tones and low ambience are a synthetic game control.
    envelope = np.maximum(0, 1 - (time % 0.8) / 0.12)
    effects = (0.016 * np.sin(2 * np.pi * 110 * time)
               + 0.055 * envelope * np.sin(2 * np.pi * 880 * time))
    return {
        'japanese': np.concatenate((pause, japanese, tail)),
        'english': np.concatenate((pause, english, tail)),
        'mixed': np.concatenate((pause, mixed, tail)),
        'game-audio': np.concatenate((pause, np.clip(mixed + effects, -1, 1), tail)),
    }


def prepare(output):
    from faster_whisper.audio import decode_audio

    output.mkdir(parents=True, exist_ok=True)
    decoded = {}
    for name, source in SOURCES.items():
        target = output / (name + Path(source['url']).suffix)
        if not target.exists() or sha256(target.read_bytes()) != source['sha256']:
            with urllib.request.urlopen(source['url'], timeout=60) as response:
                data = response.read(10_000_001)
            if sha256(data) != source['sha256']:
                raise ValueError(f'{name}: source checksum mismatch; corpus was not updated.')
            target.write_bytes(data)
        decoded[name] = decode_audio(str(target), sampling_rate=SAMPLE_RATE)
    samples = []
    for name, audio in build_cases(decoded['japanese'], decoded['english']).items():
        pcm = np.rint(np.clip(audio, -1, 32767 / 32768) * 32768).astype('<i2').tobytes()
        if sha256(pcm) != EXPECTED_PCM_SHA256[name]:
            raise ValueError(f'{name}: derived PCM differs from the frozen baseline; check decoder/dependency versions.')
        target = output / f'{name}-replay.wav'
        with wave.open(str(target), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(SAMPLE_RATE)
            stream.writeframes(pcm)
        samples.append({'id': name, 'category': name, 'path': target.name,
                        'sha256': sha256(target.read_bytes()), 'pcm_sha256': sha256(pcm),
                        'audio_seconds': len(pcm) / (2 * SAMPLE_RATE),
                        'synthetic': name in ('mixed', 'game-audio')})
    manifest = {'schema_version': 1, 'recipe': 'latency-smoke-v1', 'sample_rate': SAMPLE_RATE,
                'frame_samples': 2048, 'sources': SOURCES, 'samples': samples,
                'limits': 'Small latency smoke set, not an accuracy or natural gaming benchmark. '
                          'Mixed speech is concatenated; game effects are synthesized. '
                          'English reference has not been independently reviewed.'}
    path = output / 'manifest.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.runtime/latency-baseline'))
    args = parser.parse_args()
    print(prepare(args.output))
