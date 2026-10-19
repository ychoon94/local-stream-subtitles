# Third-party software and model terms

The MIT License in `LICENSE` covers this project's own source. Third-party code,
model weights, downloaded binaries, and recordings retain their own terms.
This repository distributes source and installer scripts; it does not bundle
models, Python environments, NVIDIA DLLs, Ollama, or browser binaries.

| Component | Upstream license or terms | Source |
| --- | --- | --- |
| faster-whisper 1.2.1 | MIT | [License](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/LICENSE) |
| OpenAI Whisper turbo weights | MIT | [Model card](https://huggingface.co/openai/whisper-large-v3-turbo), [license](https://github.com/openai/whisper/blob/main/LICENSE) |
| Faster Whisper turbo CTranslate2 conversion | Model card identifies MIT; downloaded separately | [Conversion model](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo), [faster-whisper model map](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/utils.py) |
| Qwen3-4B-Instruct-2507, default translation weights | Apache-2.0 | [Author model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507), [Ollama quantized tag](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M) |
| Ollama 0.40.6 source | MIT; downloaded runtime includes separately licensed components | [License](https://github.com/ollama/ollama/blob/main/LICENSE) |
| CTranslate2 | MIT | [License](https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE) |
| PyAV 16.0.1 source | BSD-3-Clause; binary wheels also bundle FFmpeg | [PyAV](https://github.com/PyAV-Org/PyAV), [FFmpeg licensing](https://ffmpeg.org/legal.html) |
| NVIDIA cuBLAS / cuDNN wheels | Proprietary NVIDIA terms; not covered by this project's MIT License | [CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html), [cuDNN terms](https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html) |
| FastAPI | MIT | [Upstream](https://github.com/fastapi/fastapi) |
| Uvicorn | BSD-3-Clause | [Upstream](https://github.com/Kludex/uvicorn) |
| HTTPX | BSD-style | [License](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| uv | MIT or Apache-2.0 | [Upstream](https://github.com/astral-sh/uv) |
| CPython | Python Software Foundation license and included notices | [License](https://docs.python.org/3/license.html) |
| Playwright, development only | Apache-2.0; downloaded browsers have separate notices | [Upstream](https://github.com/microsoft/playwright) |

## Earlier preview model

The initial local prototype used `qwen2.5:3b`. That exact model uses the
[Qwen Research License](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/blob/main/LICENSE),
which restricts its grant to research/evaluation uses and requires a separate
license for commercial use. It is not the default for new installations here.
Existing local settings are preserved unless a model is explicitly selected;
see the README migration command. Installing or choosing another model requires
checking that model's terms independently.

## Distribution boundaries

Setup fetches dependencies from their upstream providers. Keep upstream license
and notice files with those installations. If you redistribute a prebuilt runtime,
Python wheel, model, or full offline bundle later, review the exact artifacts and
their notices first. In particular, a PyAV wheel's FFmpeg build can have obligations
not described by PyAV's source license, and NVIDIA runtime redistribution has its
own conditions. This file is not an exhaustive audit of every transitive package.

The historical test sample is from the [JSUT corpus mirror](https://huggingface.co/datasets/FluidInference/JSUT-basic5000).
The installer and repository do not distribute the audio. Public test screenshots
show this synthetic test page, not a creator's video or likeness.
