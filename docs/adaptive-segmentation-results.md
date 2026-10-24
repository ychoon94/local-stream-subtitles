# PR 02 adaptive segmentation results

Adaptive segmentation shortened pooled onset-to-caption measurements, but it did not pass the combined latency and quality gate. The best pooled first-readable P50 was 2.891 s, above the 2.5 s target. Short-cap settings met the P95-under-4-s and final-P50-under-4-s checks in this run, but every adaptive setting exceeded the narrow English WER guard. Adaptive modes remain opt-in; `legacy` remains the default.

## Run scope

The comparison used the frozen four-category corpus described in the [PR 01 baseline](latency-baseline.md), with two rounds of four recordings in each of 19 cells: one legacy control and 18 adaptive settings. The matrix completed all 152 sessions. It produced 570 segments, 567 emitted and rendered captions, and three empty ASR results. There were no queue drops, stale skips, or ASR failures. Quality sidecars mapped all sessions by ordinal, and every VAD cell reported `adaptive_vad` as its effective mode.

The matrix changed segmentation mode, cap, and pause only. Adaptive cells kept
minimum speech at 0.25 s and overlap at 0.16 s. The legacy control ignored those
adaptive tuning fields and used its fixed 0.25 s minimum, 5 s cap, 450 ms silence
and no overlap. Recognition and translation models, decoding, prompts, context,
and final-only caption delivery were unchanged. See [adaptive segmentation
configuration and reproduction](adaptive-segmentation.md) for effective duration
rounding and measurement definitions.

The legacy control remained close to the frozen PR 01 run:

| Run | First-readable observations | P50 (s) | P95 (s) |
| --- | ---: | ---: | ---: |
| Frozen PR 01 | 18 | 5.499 | 5.832 |
| PR 02 legacy control | 18 | 5.509 | 5.849 |

First-readable and final-caption values are identical because the runtime emits final captions only. The archived PR 01 report remains unchanged.

## Latency

The table reports first-readable P50 and P95 across all rendered captions in each cell. Each setting had eight replay sessions; the count in parentheses is the number of first-readable caption observations, which varies with segmentation and empty ASR outcomes. Times are seconds. Requested caps and pauses are shown; the runtime rounds them to its 32 ms decision window as described in the configuration guide.

| Requested cap | Requested pause | RMS P50 / P95 (n) | VAD P50 / P95 (n) |
| ---: | ---: | ---: | ---: |
| 2.5 s | 300 ms | 2.910 / 3.147 (40) | 2.940 / 3.167 (42) |
| 2.5 s | 350 ms | 2.938 / 3.180 (38) | 2.896 / 3.091 (42) |
| 2.5 s | 450 ms | 2.913 / 3.140 (40) | 2.891 / 3.105 (41) |
| 3 s | 300 ms | 3.478 / 3.720 (30) | 3.422 / 3.626 (30) |
| 3 s | 350 ms | 3.432 / 3.613 (30) | 3.415 / 3.645 (30) |
| 3 s | 450 ms | 3.456 / 3.656 (30) | 3.429 / 3.707 (30) |
| 5 s | 300 ms | 5.488 / 5.770 (18) | 3.681 / 5.676 (24) |
| 5 s | 350 ms | 5.478 / 5.683 (18) | 3.695 / 5.652 (24) |
| 5 s | 450 ms | 5.526 / 5.712 (18) | 3.802 / 5.680 (24) |

The fastest pooled result was VAD at a 2.5 s cap and 450 ms pause:
first-readable and final P50 2.891 s, P95 3.105 s. The legacy first-readable
P50 was 5.509 s. The 2.5 s target for first-readable P50 was not met. At 5 s
caps, RMS P50 stayed around 5.48–5.53 s. VAD lowered P50 to
3.68–3.80 s, but its P95 remained around 5.65–5.68 s.

These pooled percentiles do not represent one first caption per recording: they combine 18 observations in the legacy control with as many as 42 in adaptive cells. Segmentation changes the number and composition of captions. The report also retains first-caption-per-session and per-round summaries. Its browser playback-to-first-caption measurement starts at the audio `playing` event, includes leading silence and capture delay, and is a different measure from speech-onset latency. Do not treat differences between the pooled rows as whole-recording end-to-end gains.

For the same VAD 2.5 s / 450 ms setting, the browser playback-origin
first-caption P50 was 4.017 s, compared with 4.966 s for legacy, across eight
sessions per condition. This per-session measure uses the same playback
origin and does not pool every caption, but still includes leading silence and
capture delay. Both comparisons come from this small frozen corpus and do not
establish a general latency or quality gain.

## Recognition quality

The Japanese CER uses one standalone reference of 17 normalized characters per round, or 34 across two rounds. It applies NFKC, case-folding, and punctuation/space removal, then concatenates all ASR caption text in order without deduplication. The legacy control scored 8.824% CER (3 edits over 34 characters), including an orthographic variation between kanji and hiragana.

The English WER uses the independently annotated 28-word reference in the official OpenSLR LibriSpeech `clean/test` record `1089-134686-0000`, pinned to revision `71cacbfb7e2354c4226d01e70d77d5fca3d04ba1`. The source audio was confirmed byte-identical to the frozen English input. WER applies NFKC and case-folding, tokenizes Unicode letters and numbers with hyphens separating words, and compares the ordered concatenation of ASR text without deduplication. Across two rounds, the control scored 1.786% WER (1 edit over 56 words). Reference attribution and audio identity are recorded in the [quality reference ledger](../benchmarks/quality-references.json).

| Requested cap | Requested pause | Japanese CER RMS / VAD (%) | English WER RMS / VAD (%) |
| ---: | ---: | ---: | ---: |
| 2.5 s | 300 ms | 5.882 / 41.176 | 16.071 / 16.071 |
| 2.5 s | 350 ms | 2.941 / 29.412 | 14.286 / 16.071 |
| 2.5 s | 450 ms | 5.882 / 2.941 | 10.714 / 16.071 |
| 3 s | 300 ms | 8.824 / 0.000 | 12.500 / 12.500 |
| 3 s | 350 ms | 0.000 / 0.000 | 7.143 / 7.143 |
| 3 s | 450 ms | 0.000 / 8.824 | 10.714 / 10.714 |
| 5 s | 300 ms | 8.824 / 0.000 | 7.143 / 19.643 |
| 5 s | 350 ms | 0.000 / 8.824 | 7.143 / 14.286 |
| 5 s | 450 ms | 0.000 / 0.000 | 7.143 / 19.643 |

Every adaptive English WER result was at least 7.143%, above the control-plus-2-percentage-point guard of 3.786%. This one 28-word reference does not establish broad English accuracy, and no new human listening review was performed. The Japanese results are also too small to establish the broad CER guard; the 34-character score is sensitive to a few character differences.

The sidecars show short tails can add out-of-language text after a complete Japanese sentence. With a 2.5 s RMS cap and 300 ms requested pause, a 576 ms tail contained 160 ms of copied context, 320 ms of effective pause, and at most 96 ms of newly classified audio; ASR returned Korean `다.` and the translation `Da.`. With a 2.5 s VAD cap and 350 ms pause, both Japanese repetitions were followed by `감사합니다.` (“Thank you.”) in a 576 ms tail with 160 ms of context, 352 ms of pause, and at most 64 ms of newly classified audio. English outputs also split or repeated fragments such as `out` and `flour`.

## Decision and limits

The latency and quality results do not support changing the default or treating adaptive segmentation as quality-approved. The implementation remains behind its opt-in configuration. No partial captions, rolling ASR, streaming profiles, or model/decoding/prompt/context changes were part of this comparison. Broader accuracy review and boundary/meaning checks are needed before considering further latency work; PR 03 has not started.

VAD used a CPU-only detector. GPU memory is sampled for the whole RTX 3070 board, including desktop and other processes; the highest per-cell sample was 6,152 / 8,192 MiB. This does not identify application-only usage or guarantee behavior alongside a GPU-heavy game.

## Review fixes and follow-up

The PR review fixes strengthen benchmark evidence and reporting without changing
segmentation, recognition, translation, or caption delivery. They make a later
GPU probe failure mark telemetry unavailable while retaining earlier peak
samples; hold a cell when quality evidence cannot be mapped to verified replay
session ordinals; reject missing or out-of-range ordinals on captured segments;
require unique, non-empty string sample IDs in manifests; and report legacy's
fixed limits rather than adaptive tuning values. `legacy` remains the default.

The archived 19-cell summaries were independently recomputed and matched. The
browser/GPU matrix itself was not rerun after these review fixes. The separate
76-case, 285-segment check establishes PCM equivalence for its cases only; it is
not new browser timing or recognition-quality evidence. The provenance section
below distinguishes the original measured runner/backend hashes from the later
final-source PCM-equivalence hash; those hashes do not all describe the same
source version.

A credible follow-up is to test a rolling recognition window separately from
the schedule that commits visible captions. One candidate is a
[Whisper-Streaming LocalAgreement approach](https://arxiv.org/abs/2307.14743):
decode overlapping audio repeatedly and commit text only after a prefix stays
stable across successive decodes. This is an experiment, not a promised latency
improvement or a design selected for this PR. Keep acoustic context around
boundaries, align overlap using audio or token timing, and carry short pending
tails into the next decode. Independently accepting tiny tails can add fragments;
blindly suppressing them can omit speech. For Japanese-to-English captions,
use a clause- and context-aware translation commit schedule as well; a stable ASR
prefix alone does not determine a finalized English rendering.

Evaluate each change on held-out Japanese and English audio with independent
references, including negative controls, short replies, names, and language
switches. Count omissions, duplicates, hallucinations, and semantic translation
errors alongside word/character scores. Use common speech-onset and speech-end
labels, and report first meaningful caption and final caption separately. Track
CPU/GPU work and dropped audio, then remeasure the exact final source on the
same paired inputs. Separately profile endpoint cost. Moving VAD scoring off the
event loop or avoiding rescoring the entire 1.984 s lookback at each 32 ms
decision are experiments only; require endpoint-decision parity on the same
audio and paired quality results before adopting either. Keep the current models
and decoding settings until paired latency and quality results support a change.
The [faster-whisper performance comparison
guidance](https://github.com/SYSTRAN/faster-whisper#comparing-performance-of-other-implementations)
can inform backend profiling, but does not predict results in this application.

## Provenance and validation

The main matrix ran before the correction that preserved silence across forced cuts. Its harness source SHA-256 is `EAEABA101C5DE9B9ED7F5E2C9FFF2FC6BD33D525347B93D1B4805A36698A2F0C`; the measured `backend/audio.py` source SHA-256 is `7b25d240784d47751a2568dad00be5156487e6ae8476a06ffbc125a87bd363cb`. These are source-code hashes, not audio hashes. The [numeric PR 02 report](../benchmarks/results/pr02-rtx3070.json) preserves the completed 19-cell measurements and their source provenance.

A separate CPU-only comparison covered 76 cases and 285 segments. It found identical segment counts, order, and float PCM; the comparison artifact SHA-256 is `E9FA8161DF3AEA7BD6C5A8885F30D638F56994C0EF74A4E9D1B1A21CFC74F4B4`. The final `backend/audio.py` source SHA-256 is `E923181ECD972519D0273B09805056BEEB8E29B0584C8EA567D22CAF47532959`. This establishes PCM equivalence for those cases, not unchanged browser latency or recognition quality. The full frozen matrix was not rerun with the final runner; the separate supplemental run below exercises it through Chrome and native inference.

The corrected runner's verification included the backend and benchmark tests with warnings treated as errors, 19 Node tests, three launcher checks, four Chrome visibility checks, and Vimeo checks. Independent Windows probes observed a 2.203 s watchdog stop and 0.188 s Node child cleanup. Detached-browser cleanup on POSIX remains unverified.

## Supplemental music and meaning controls

The final runner completed a separate Chrome/native-inference run: legacy, RMS
3 s / 350 ms, and VAD 3 s / 350 ms, with two rounds and 30 fresh sessions per
condition. Inputs were the frozen Japanese positive, seven music/action crops
previously confirmed speech-free by user listening, six previously reviewed
dialogue meanings, and two seconds of silence. Each source crop's recorded hash
was verified before adding fixed leading/trailing silence. Public source URLs,
crop times, hashes, labels, and numeric events are retained in the supplemental
section of the numeric report; audio stays local.

| Condition | Finalized segments | Visible captions | Empty ASR attempts | Captions on 16 negative/silence sessions |
| --- | ---: | ---: | ---: | ---: |
| Legacy | 28 | 7 | 21 | 0 |
| RMS 3 s / 350 ms | 48 | 7 | 41 | 0 |
| VAD 3 s / 350 ms | 10 | 8 | 2 | 0 |

All 90 sessions completed with verified mapping and effective buffer modes,
including sessions with zero segments. There were no drops, stale skips,
processing failures, or missing render acknowledgements. Peak sampled GPU use
was 5,720 MiB in this run.

The VAD gate rejected all negative/silence inputs before ASR. It also produced no
segments for the independently labeled safety command, mother call, and
invitation in either round. Legacy and RMS had already failed to recover those
meanings; their empty ASR attempts are not evidence that speech was absent.
The other outputs did not recover the Ginza/Odo comparison or Godzilla name.
Tokyo examples retained partial refusal/fire wording while omitting the name;
the VAD Ginza examples instead produced unrelated English and Japanese fragments.
No condition fully recovered the six reviewed meanings with their required names.

These controls establish zero false captions on the selected negative inputs and
expose persistent speech/meaning failures. They do not establish representative
music, short-utterance, or gameplay quality. The low VAD median on this different
set includes short fragments and omitted lines and cannot clear the frozen-corpus
latency target. Adaptive segmentation alone has not met the combined requirements;
rolling ASR remains a separate decision, and PR 03 has not started.
