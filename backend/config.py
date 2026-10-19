import json
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
