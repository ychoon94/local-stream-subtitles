# Latency measurement (PR 01)

This adds an opt-in baseline, not a latency optimization. The default five-second
buffer, 450 ms silence gate, recognition/translation models, one-item pending
queue, local-only inference, pairing and native-inference cancellation rules stay
the same. Existing final-only messages still work. No streaming profiles or
provisional captions are introduced.

## Enable and roll back

After normal setup, add `"latency_observability": true` to
`.runtime/settings.json`, restart the local service, reload the unpacked extension,
and refresh the player tab. The field defaults to false when absent. The extension
requests observation, but only an authenticated session on an enabled server gets
timing messages. Do not share settings or `extension/local-config.json`: they
contain your pairing token.

To stop collecting measurements, set the field to false (or remove it) and restart
the service. No models or extension preferences need migration. The normal overlay
does not display diagnostic information.

## Repeatable input

Run from the repository root with its installed Python and npm dependencies:

```powershell
.venv\Scripts\python.exe -m scripts.prepare_baseline
.venv\Scripts\python.exe -m scripts.benchmark --live --manifest benchmarks/baseline.json --repeats 2 --output test-results/latency-websocket.json
.venv\Scripts\python.exe -m scripts.benchmark --browser --manifest benchmarks/baseline.json --repeats 2 --output test-results/latency-browser.json
```

Start the enabled, warmed service before either replay. Stop any existing capture;
the benchmark does not interrupt an active session. The browser path needs recent
Chrome supporting unpacked-extension CDP controls (or Playwright Chromium); it
auto-detects installed Windows Chrome, and accepts `CHROME_PATH`. It uses a
disposable profile and intercepts a controlled YouTube-origin page locally. Audio
is played once at normal speed through real tab capture, resampling, segmentation,
the live queue, inference and the overlay. It does not open a personal profile or
contact YouTube for the fixture. Each sample starts a new session/history.

`--live` sends identical PCM16 frames at a fixed 2,048-sample/128 ms cadence through
the authenticated WebSocket. Send times represent completed audio frames. The last
short frame is retained without extra speech padding. Scheduling lateness is
reported separately; actual OS timing and model outputs are not deterministic.
It measures inference and delivery to the replay client, and leaves browser
display measurements null. `--settings` may point to a local pairing settings file;
the token is never printed or put in a URL. `--url` permits loopback WebSocket
addresses for the PCM path; Chrome uses the extension's fixed port 8765.

The original `python -m scripts.benchmark <audio>` inference-only mode is retained.
It is not a live segmentation or browser latency benchmark.

## Corpus and provenance

`benchmarks/baseline.json` freezes source revisions and SHA-256 hashes, decoded PCM
hashes, sample categories, durations and preparation recipe. Preparation downloads
two recordings, verifies their source hashes, resamples using the pinned decoder,
adds fixed leading/trailing silence, and checks derived PCM hashes. A mismatch
fails instead of silently replacing the baseline. Keep the same frozen manifest
for every optimization comparison. Downloads happen only during preparation;
inference stays local.

| Category | Content | Duration |
| --- | --- | ---: |
| Japanese | JSUT BASIC5000_4503 | 4.356 s |
| English | LibriSpeech mirror recording `1.flac` | 11.971 s |
| Mixed | The two recordings concatenated with a fixed pause | 15.303 s |
| Game audio | The mixed recording plus deterministic electronic effects/ambience | 15.303 s |

The Japanese recording is from Sonobe, Takamichi and Saruwatari's
[JSUT corpus](https://sites.google.com/site/shinnosuketakamichi/publication/jsut),
using a pinned [mirror](https://huggingface.co/datasets/FluidInference/JSUT-basic5000).
Its [audio terms](https://huggingface.co/datasets/FluidInference/JSUT-basic5000/blob/main/README_en.txt)
permit personal use/non-commercial research and impose commercial-use and
redistribution restrictions. The English recording comes from a pinned
[LibriSpeech mirror](https://huggingface.co/datasets/Narsil/asr_dummy), attributed to
Panayotov et al.; [LibriSpeech](https://www.openslr.org/12/) is CC BY 4.0.
Downloaded and derived audio remains in ignored `.runtime/`; it is not part of
the MIT source release.

This is a small latency smoke corpus. The mixed and game cases are synthetic,
not natural conversation or representative gameplay. Only the Japanese source
has an independently supplied transcript in the manifest. It establishes neither
Japanese CER, English WER, nor translation quality; those require PR 07's reviewed
examples and broader recordings. Do not use this set alone to clear a quality gate.

## Clock and latency definitions

All backend observation timestamps use `time.perf_counter()` in milliseconds.
The older `time.monotonic()` utterance age remains separate for existing scheduling
decisions and legacy `latency_ms`. Never subtract those two clocks. On Windows
Python 3.12, `monotonic()` can use a 15.625 ms-resolution clock; the performance
counter avoids that quantization for observations.

The extension timestamps with `performance.timeOrigin + performance.now()`, so its
offscreen document and content frames share a browser time reference. Three
authenticated round trips estimate the server-minus-browser offset. The smallest
round-trip sample wins; uncertainty is half the network/dispatch round trip plus
server clock resolution and a conservative 1 ms browser precision allowance.
Browser timestamps are converted with the offset captured for that caption.
Cross-clock ordering accepts that uncertainty; browser receipt/render ordering
remains strict. Reports retain nonnegative midpoint estimates and lower/upper
bounds. A sub-millisecond delivery estimate is not more precise than its bound.

| Field | Definition |
| --- | --- |
| `speech_started_ms` timestamp | Estimated beginning of the first energy-positive frame: backend detection time minus that frame's audio duration. Excludes retained pre-roll. |
| `speech_to_segment_ms` | Estimated speech-frame onset to segment finalization. Includes spoken duration and endpoint waiting. |
| `queue_ms` | Enqueue request to engine entry, including the processing-status handoff. |
| `lock_ms` | Engine entry to acquiring the inference lock. |
| `asr_ms` | Complete native ASR call including thread dispatch and lazy segment iteration. |
| `translation_ms` | Translation request/output handling; 0 for successful direct/English bypass, null when unobserved. |
| `server_total_ms` | Estimated speech-frame onset to result construction. |
| `delivery_ms` | WebSocket send initiation to offscreen receipt, after calibration. |
| `display_ms` | Offscreen receipt through extension relay and fully visible browser frame. |
| `first_readable_ms` | Estimated speech-frame onset through the first foreground animation frame with connected, nonempty caption text and opacity at least 0.99. |
| `final_ms` | Visibility of the final caption; equal to first-readable in the current final-only pipeline. |

The readable marker is a conservative rendering proxy after the existing fade,
not a compositor/pixel presentation guarantee or a human reading-speed measure.
The onset is a frame-level backend estimate: upstream tab capture, AudioWorklet
batching, resampling and inbound socket delay are not directly timed. Quantization
within a 128 ms frame remains even when clock calibration is accurate. These are
onset-to-caption latencies, not speech-end-to-caption latencies. A segmentation
change must compare the same source audio and consider these limitations.

## Outcomes and report interpretation

Each finalized segment gets a session-local `segment_id`, including segments that
are later dropped or yield no speech. Events record segmentation, enqueue, engine
entry, lock acquisition, ASR completion, translation start/end, result construction,
send, browser receipt and rendering. Metrics contain numeric values and fixed
outcome/source labels, without dialogue or pairing tokens. They travel only over
the authenticated connection and extension-internal messages. Render reports must
come from the active trusted tab/session. Duplicate/stale acknowledgements cannot
replace the first observation.

Counters distinguish segmented/emitted captions, pending-queue evictions, stale
segments, empty ASR, failed processing, and dropped capture frames/samples.
`asr_failed` currently counts a processing exception, including a translation
failure; it does not classify provider errors. Very short energy bursts rejected
before finalization are not finalized segments and are not in these counters.
`benchmark_end` explicitly flushes an unfinished tail as `source_eof` and drains
work; normal live operation does not flush on that command when observation is
disabled. Prefer the fixed silent tail for natural endpoint comparisons.

Backend retention is bounded at 128 segments; offscreen keeps at most 512 numeric
events and 128 pending render identities. Event loss is reported and invalidates
the browser replay. An emitted caption that never becomes visible has no display
latency: coverage/unobserved counts and `measurement_complete` expose it. After
draining inference, the runner allows two seconds for render acknowledgements.
Timing percentiles alone must not hide missing captions or failed segments.

P50/P95 use nearest rank and merge updates by session/sample/segment, so emitted
and rendered events do not count the same segment twice. Counts accompany every
metric, absent values are null, and successful bypass translations contribute 0.
ASR distributions include completed empty attempts; queue evictions have no ASR
measurement. GPU telemetry samples `nvidia-smi` about once per second and reports
peak board memory/utilization, including desktop and other processes. It can miss
brief peaks; unavailable telemetry is explicitly marked.

See [the measured PR 01 baseline](latency-baseline.md) for results and limits.
