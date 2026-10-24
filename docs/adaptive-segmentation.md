# Adaptive speech segmentation (PR 02)

Adaptive endpointing is an opt-in experiment. Existing installations continue to
use the original RMS buffer, five-second cap, 450 ms pause and three-frame
pre-roll. Recognition and translation models, decoding, prompts, context, native
inference cancellation, pairing and final-only captions are unchanged.

## Configuration and fallback

Merge these fields into the existing local `.runtime/settings.json`, preserving
its other fields, then restart the service. This is an experiment configuration,
not a quality-approved recommendation:

```json
{
  "segmentation_mode": "adaptive_rms",
  "speech_min_seconds": 0.25,
  "speech_max_seconds": 3.0,
  "speech_silence_seconds": 0.35,
  "speech_overlap_seconds": 0.16
}
```

| Setting | Default | Accepted values | Meaning in adaptive modes |
| --- | --- | --- | --- |
| `segmentation_mode` | `legacy` | `legacy`, `adaptive_rms`, `adaptive_vad` | Original energy gate, sample-count energy gate, or speech-aware gate |
| `speech_min_seconds` | 0.25 | 0.032–2 | Minimum newly classified speech; copied context does not count |
| `speech_max_seconds` | 5 | 1–10 | Hard cap including retained context; rounded down to a 32 ms decision |
| `speech_silence_seconds` | 0.45 | 0.1–2 | Consecutive detector-negative audio before closing; rounded up to a decision |
| `speech_overlap_seconds` | 0.16 | 0–0.5 | Context copied after a forced cap; rounded down to a decision |

The maximum must exceed minimum speech + requested overlap + 300 ms. Invalid or
nonfinite values fail before capture. Adaptive tuning fields have no effect in
`legacy` mode. To roll back, set `segmentation_mode` to `legacy` (or remove it)
and restart; no model, extension preference or pairing migration is needed. The
benchmark reports legacy's actual fixed limits as 0.25 s minimum, 5 s maximum,
450 ms silence and no overlap, even if the adaptive tuning fields differ.

`adaptive_vad` uses the CPU Silero model already packaged with faster-whisper.
The [packaged callable](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/vad.py)
scores a bounded lookback of at most 1.984 seconds, with session-local audio and
0.5 activation / 0.35 release thresholds. Recognition still receives original
PCM, including context and endpoint silence; its existing native VAD remains
enabled. This adds an endpoint detector, not a recognition model change.

If loading or calling the endpoint detector fails, buffered audio is retained,
a fixed warning is logged, and that session continues as `adaptive_rms` with the
configured durations. The benchmark records the effective mode separately and
holds a requested VAD experiment if it observes fallback. Protocol timing events
remain numeric and do not expose detector metadata or dialogue.

## Boundary behavior

Adaptive decisions use 512 samples / 32 ms, independently of incoming packet
sizes. Requested 2.5, 3 and 5 second caps become 2.496, 2.976 and 4.992 seconds.
Requested 300, 350 and 450 ms pauses become 320, 352 and 480 ms. Packet delivery
and CPU scheduling can delay finalization beyond these audio durations.

Idle pre-roll retains at most 288 ms. A forced cap copies the configured overlap
into the next segment, counting it toward the next cap. A natural pause retains
only bounded pre-roll for a future start. Copied audio alone cannot generate a
caption. Newly detected short continuations after a forced cap are kept even if
they are shorter than the minimum; this avoids dropping the last part of an
otherwise accepted utterance. EOF retains a partial decision without adding
padding to recognition audio.

The input frame limit remains one second, and the pending inference queue remains
one item. An idle worker can take a segment before another endpoint from the same
packet applies the normal queue eviction policy. A busy worker still evicts old
pending work to stay live. Audio storage is bounded by the cap, one incoming
packet, pre-roll/overlap, and the detector lookback.

Sample continuity does not prove word continuity. Shorter contexts, delayed VAD
activation, low-energy speech, and repeated overlap can change recognition or
duplicate words. No text deduplication or decoding adjustment hides those effects.

## Reproduce the comparison

Prepare the frozen corpus without changing `benchmarks/baseline.json` or PR 01's
archived report. Stop any existing capture and service on port 8765; the matrix
refuses to replace any listener. Start local Ollama normally, enable
`latency_observability` in local settings, and install the normal dependencies.

```powershell
.venv\Scripts\python.exe -m scripts.prepare_baseline
.venv\Scripts\python.exe -m scripts.adaptive_benchmark --browser --manifest benchmarks/baseline.json --repeats 2 --timeout 30 --output test-results/pr02-matrix/checkpoint.json
```

The runner starts its own loopback service, loads and warms one engine, then runs
the legacy control followed by all 18 mode × cap × pause combinations. It changes
only segmentation settings between idle sessions. Use `--live` for PCM-only
replay, with display metrics remaining unobserved. `--modes adaptive_vad`,
`--maximums 3`, `--silences 0.35` and `--skip-legacy` select a smaller experiment.
Minimum speech and overlap come from the local settings and are recorded.

Every cell has a cooperative deadline; the public CLI also owns a worker process
and enforces an outer wall-clock deadline. Native inference retains its normal
cancellation protection while the worker shuts down. If it stalls beyond the
outer budget, the Windows watchdog terminates only its owned worker/browser
process tree and records `WatchdogDeadlineExceeded`. Browser cancellation also
cleans up the owned Node tree before propagating the failure. POSIX uses an owned
worker process group and gives Node/Playwright bounded time after SIGTERM;
detached-browser cleanup on POSIX has not been verified. At most 64 replay sessions per cell are
accepted, including sessions with no detected speech.

Atomic checkpoints stop at `HOLD` for dropped or
stale audio, failed processing, incomplete rendering, VAD fallback, unavailable
GPU telemetry, unverified quality-to-session mapping, or sampled whole-board
memory reaching 8,192 MiB. Quality mapping requires matching processed-segment
counts and valid session ordinals for every captured segment; missing or
out-of-range ordinals leave the mapping unavailable. The manifest loader also
rejects duplicate, empty, or non-string sample IDs before replay, so ID-keyed
reports keep each recording distinct. An exception
produces a failed checkpoint and cleans up the owned server. Empty ASR attempts
are retained in the report; they can be normal for music and do not automatically
invalidate an experiment. These stop rules are experimental safeguards, not a
quality gate. Speech rejected before a segment is finalized does not appear as a
queue drop or empty ASR attempt. A zero-output session can represent either clean
silence or a missed spoken line; only independent input labels distinguish them.

For one bounded checkpoint wait:

```powershell
.venv\Scripts\python.exe -m scripts.adaptive_benchmark wait test-results/pr02-matrix/checkpoint.json --after-cell 0 --timeout 60
```

The wait returns the next completed cell, hold, failure, completion, or timeout.
Review that result before advancing `--after-cell`. Each cell saves numeric
latency events plus a separate local quality sidecar containing recognized and
translated text. Session ordinals account for sessions with no output; mapping is
marked verified only when processed-segment counts agree. These sidecars are
benchmark artifacts and should be reviewed before sharing. Pairing settings and
audio stay local and ignored.

To reproduce the narrow sentence error counts, save the English dataset
annotation identified in [the reference ledger](../benchmarks/quality-references.json)
as an ignored local text file, then run:

```powershell
.venv\Scripts\python.exe -m scripts.summarize_segmentation test-results/pr02-matrix/checkpoint.json --english-reference .runtime/reference-research/1089-134686-0000.reference.txt --output test-results/pr02-quality-summary.json
```

The summary exports counts and reference hashes. It counts duplicated text and
missing output as errors; it does not remove overlap repetitions before scoring.
Without an English reference, English WER remains unobserved.

## Measurement limits

The existing [PR 01 clock definitions and rendering limits](latency-observability.md)
still apply. Scheduling ages use `monotonic`; observations use `perf_counter`.
The two clocks are never subtracted. Adaptive onset is the estimated start of the
first positive 32 ms detector window, excluding pre-roll and copied overlap.
Legacy onset remains the first positive incoming RMS frame. VAD can detect speech
later than RMS, so these onset-based metrics alone can favor a delayed detector.

Browser reports also measure playback start through the first fully visible
caption, using the audio `playing` event and renderer acknowledgement in the
browser's shared time origin. This includes leading silence and capture delay;
it is not speech-onset latency. It provides a common source origin across modes.
Per-segment percentiles also change their population when segmentation changes;
compare first captions per recording, transcripts, counts and empty outcomes.

The frozen corpus contains only two source recordings; mixed/game conditions are
synthetic. Japanese has a supplied sentence reference. PR 02 adds a dataset-supplied
English annotation whose audio matches the frozen source byte for byte, without
changing the frozen manifest. It has not received a new human listening review.
These two narrow sentence comparisons cannot clear the requested broad Japanese
CER / English WER ≤ baseline + 2 percentage points or meaning/name preservation
gate. Real music controls and reviewed meaning examples add failure evidence,
not a representative accuracy set.

Measured results and the decision about further latency work are recorded in
[the PR 02 comparison](adaptive-segmentation-results.md).
