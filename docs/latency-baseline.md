# PR 01 baseline — RTX 3070

Measured on 24 October 2026 using the frozen
[four-category corpus](../benchmarks/baseline.json), two rounds, eight fresh
sessions and 18 finalized segments. All 18 emitted captions returned a fully
visible foreground-frame acknowledgement. No queue eviction, stale skip,
empty ASR, processing failure or dropped capture frame was observed. This is a
small reproducible baseline, not evidence of sustained-load or broad quality
parity. No latency optimization is included in this PR.

The [numeric report](../benchmarks/results/pr01-rtx3070.json) contains every timing
event, sample hash, count and uncertainty bound, without subtitle text or tokens.
The corpus's mixed conversation and game effects are synthetic controls. Source
audio stays local under its original terms.

## Configuration and method

- NVIDIA RTX 3070, 8,192 MiB; Windows; Python 3.12.15.
- Whisper `turbo`, CUDA `int8_float16`, beam 3; Ollama
  `qwen3:4b-instruct-2507-q4_K_M`; existing prompt, context and English bypass.
- faster-whisper 1.2.1, CTranslate2 4.8.3, PyAV 16.0.1, NumPy 2.5.4,
  websockets 17.2; Playwright 1.62.1 driving installed Chrome.
- Original `main` policy: 5 s segment cap, 450 ms RMS silence gate, three-frame
  pre-roll, 2,048-sample PCM frames, one pending segment and one native inference
  at a time. The separate uncommitted 300 ms experiment was not used.
- Models warmed before collection; fresh extension profile; normal-speed recorded
  audio routed through actual Chrome tab capture, inference and the player overlay.
  Session history resets for each recording. Two repetitions do not constitute
  eight independent speech sources.
- Observability uses the high-resolution performance clock, calibrated to browser
  timestamps. The first trial exposed a Windows low-resolution-clock issue and
  was discarded. Only the corrected, complete run appears in the report.

Reproduce after enabling the observation setting and starting the warmed service:

```powershell
.venv\Scripts\python.exe -m scripts.prepare_baseline
.venv\Scripts\python.exe -m scripts.benchmark --browser --manifest benchmarks/baseline.json --repeats 2 --timeout 30 --output test-results/latency-baseline-browser.json
```

See [measurement setup and definitions](latency-observability.md) for prerequisites,
clock domains, developer controls, the PCM-only alternative and rollback.

## Measured latency

Milliseconds, nearest-rank percentiles, 18 segment observations per row. Buffering
includes spoken duration. First-readable/final are measured from estimated first
energy-positive PCM-frame onset; they are not speech-end delay.

| Stage | P50 | P95 |
| --- | ---: | ---: |
| Speech onset through segmentation | 4,737.866 | 5,127.880 |
| Queue/status handoff | 0.168 | 0.190 |
| Inference lock wait | 0.004 | 0.004 |
| ASR | 633.177 | 717.818 |
| Translation, including English bypass | 0.000 | 86.518 |
| Speech onset through server result | 5,336.898 | 5,800.651 |
| WebSocket delivery estimate | 0.297 | 0.408 |
| Browser relay and fully visible frame | 32.500 | 206.400 |
| First readable subtitle | **5,498.599** | **5,831.854** |
| Final subtitle | **5,498.599** | **5,831.854** |

Final and first-readable are identical because the existing runtime sends only
final captions. Most segments use English bypass, so pooled translation P50 is
zero; this does not mean actual translation requests complete instantly. Stage
percentiles are separate distributions and must not be summed to derive total
percentiles.

Calibration uncertainty was P50 1.198 ms / P95 1.247 ms. Small delivery midpoint
estimates are below that uncertainty: their corresponding P95 upper bound is
1.556 ms. The first-readable P95 lower/upper estimates are 5,830.656–5,833.051 ms.
These clock bounds exclude audio-frame onset uncertainty and browser presentation
uncertainty.

| Category | Captions across two rounds | First-readable P50 | P95 |
| --- | ---: | ---: | ---: |
| Japanese | 2 | 3,755.142 | 3,895.227 |
| English | 4 | 5,521.902 | 5,831.854 |
| Mixed | 6 | 5,305.169 | 5,821.871 |
| Game audio (synthetic effects) | 6 | 5,618.666 | 5,738.842 |

GPU telemetry captured 95 samples at approximately one-second intervals. Peak
whole-board usage was **6,018 MiB / 8,192 MiB**, with peak sampled utilization 100%.
This includes desktop and other GPU allocations and can miss brief peaks. There
was no observed OOM or queue backlog during this run; it does not prove stability
under sustained speech or simultaneous GPU-heavy gameplay.

## Interpretation and next gate

The roadmap's initial first-readable targets (P50 2.5 s / P95 4 s) and final P50
target (4 s) are not met by this baseline. Buffering dominates these onset-based
measurements; ASR is the next largest measured stage. PR 02 should compare adaptive
segmentation against these exact hashes and retain the quality guardrails before
claiming an improvement.

The reported onset excludes upstream capture/packetization/socket delay and has
frame-level uncertainty. The visible-frame marker is a conservative rendering
proxy after the existing fade, not proof of compositor presentation. Japanese CER,
English WER, terminology/meaning preservation and natural gameplay remain
unmeasured. Expand and independently review the corpus before using it as an
accuracy or release gate.

The merge review added CSS/layout visibility and failed-translation regressions.
The recorded run had visible captions and no failed translations, so those fixes
do not change its archived events or numbers.

Validation for this PR also includes backend tests with warnings treated as errors,
extension tests, launcher isolation tests, real Chrome capture/fullscreen/stop/
restart/navigation checks, and the separate browser-only Vimeo integration checks.
