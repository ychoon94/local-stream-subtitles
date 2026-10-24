import json
import math
import os
import secrets
from dataclasses import asdict, dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / '.runtime' / 'settings.json'
DEFAULT_TRANSLATION_MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
os.environ.setdefault('HF_HOME', str(ROOT / 'models' / 'hf-cache'))
os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')


@dataclass(frozen=True)
class Settings:
    token: str
    whisper_model: str = 'turbo'
    translation_model: str = DEFAULT_TRANSLATION_MODEL
    device: str = 'cuda'
    compute_type: str = 'int8_float16'
    sample_rate: int = 16000
    latency_observability: bool = False
    segmentation_mode: str = 'legacy'
    speech_min_seconds: float = 0.25
    speech_max_seconds: float = 5.0
    speech_silence_seconds: float = 0.45
    speech_overlap_seconds: float = 0.16

    def __post_init__(self):
        if self.segmentation_mode not in ('legacy', 'adaptive_rms', 'adaptive_vad'):
            raise ValueError('Unknown segmentation_mode.')
        limits = {
            'speech_min_seconds': (0.032, 2.0),
            'speech_max_seconds': (1.0, 10.0),
            'speech_silence_seconds': (0.1, 2.0),
            'speech_overlap_seconds': (0.0, 0.5),
        }
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name} must be finite and between {low} and {high}.')
        if self.speech_min_seconds + self.speech_overlap_seconds + 0.3 >= self.speech_max_seconds:
            raise ValueError('Maximum speech segment must leave room for minimum speech, pre-roll and overlap.')


def load_settings() -> Settings:
    if not SETTINGS_PATH.exists():
        raise RuntimeError('Run setup.ps1 first to create the local pairing configuration.')
    return Settings(**json.loads(SETTINGS_PATH.read_text(encoding='utf-8-sig')))


def initialize_settings(translation_model: str | None = None) -> Settings:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        settings = load_settings()
    else:
        settings = Settings(token=secrets.token_urlsafe(32))
    if translation_model:
        settings = replace(settings, translation_model=translation_model)
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2), encoding='utf-8')
    (ROOT / 'extension' / 'local-config.json').write_text(
        json.dumps({'token': settings.token}), encoding='utf-8'
    )
    return settings


def configure_gpu_libraries():
    """Windows resolves CTranslate2's CUDA DLLs from the project environment."""
    import site

    handles = []
    for base in site.getsitepackages():
        for component in ('cublas', 'cudnn', 'cuda_runtime'):
            directory = Path(base) / 'nvidia' / component / 'bin'
            if directory.exists():
                os.environ['PATH'] = str(directory) + os.pathsep + os.environ.get('PATH', '')
                if hasattr(os, 'add_dll_directory'):
                    handles.append(os.add_dll_directory(str(directory)))
    return handles
