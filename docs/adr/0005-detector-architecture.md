# 5. Detector architecture: local prescreen plus VLM adjudicator

**Date:** 2026-08-08
**Status:** Accepted

## Context

DESIGN.md §3 specifies a pretrained YOLO-class ONNX detector counting vehicles in a hand-drawn ROI,
plus a frame-difference motion proxy, with thresholds tuned against 200–400 hand-labeled frames.
That was the right call when written. Two things have changed.

**The images are smaller than assumed.** Measured after capture began: 328×334, of which ~90px is
burned-in chrome, leaving roughly **328×240 of usable roadway**. A vehicle a block back is a few
dozen pixels. A general-purpose detector will struggle, and after dark — where headlight glare and
stopped vehicles look alike at this resolution — hand-tuned thresholds are the weakest possible
tool.

**A vision language model can answer the question directly**, without training, labeling, or
threshold tuning. DESIGN.md §3 already says "do not train a custom model… model quality is not the
interesting part of this project." A VLM is the stronger form of that same argument.

The objection is cost, so it was measured rather than assumed.

## Cost, measured

Frames are 328×334 ≈ **146 image tokens** (w×h/750); with a short prompt, ~300 input tokens per
classification. Cameras refresh every ~60s, so ~8,640 new frames/day across six cameras.

| Approach | Volume | Model | Cost |
| --- | --- | --- | --- |
| Every new frame | 8,640/day | Haiku 4.5 | ~$97/mo |
| Every new frame | 8,640/day | Sonnet 5 | ~$290/mo |
| **Prescreen + adjudicate** | ~200/day | Haiku 4.5 | **~$2/mo** |
| Label 5,000 frames once (Batch, 50% off) | one-time | Haiku 4.5 | **~$0.75** |

Prompt caching does not apply: Haiku 4.5 needs a ≥4096-token cacheable prefix and this prompt is
~150 tokens.

## Decision

**Hybrid.** Local frame-differencing runs on every frame as an always-on prescreen — free, no
network dependency. A VLM adjudicates only candidate state transitions, plus a heartbeat while a
session is open. Roughly 200 calls/day.

**Label the Phase 0 corpus with a VLM batch job** rather than hand-labeling. ~$1 for ~5,000 frames,
spot-checked by hand. This is useful under any detector choice and doubles as the capability test.

## Consequences

- Classifying every frame is the wrong shape regardless of cost: blockages last 10–60 minutes and
  cameras refresh every 60s, so the interesting information is at transitions, not in every frame.
- Cost lands at roughly the same order as storage (~$2–5/month), so detection cost is not a design
  constraint.
- The prescreen keeps working if the API is unreachable; the system degrades to the motion signal
  rather than going blind.
- **Replay gets more expensive.** Re-running the corpus through ONNX is free; through an API it is
  billed each time. This weakens the replayability argument DESIGN.md calls the project's
  strongest. Mitigation: evaluate prompt changes against a sampled subset, and keep the frame-diff
  signal reproducible offline for the full-corpus replay demonstration.
- **Judgments will flicker** frame to frame. Not fatal: FusionJob's hysteresis and debounce exist
  precisely to smooth a noisy binary signal, and the same smoothing was always needed for ONNX.
- Adds a network dependency the pipeline did not previously have beyond ODOT.
- The ROI editor is still needed — the prescreen must exclude the burned-in chrome bands, or the
  frame-difference score fires on the ticking timestamp rather than on traffic.

## Unproven

Capability at night is **not yet established**. On two consecutive 9:5x PM frames from
`odot-678`, the scene, intersection, and streetlights were readable, but whether vehicles were
queued was not confidently determinable. Every frame captured so far is from one night. The
labeling run is the test: if daylight labels are good and night labels are poor, that determines
where each signal is trusted, and the confidence values in `crossing.state.v1` should reflect it
rather than being invented.

## Rejected: VLM only

Simplest to build — no ROI editor, no labeling, no thresholds. Rejected because classifying every
frame costs ~$97/month for information that only changes at transitions, sampling to cut that cost
reintroduces the latency question, and a hard network dependency for the always-on path is a poor
trade when a free local signal covers it.

## Rejected: ONNX only, as originally specified

Free to run and free to replay, with no external dependency — genuinely the better story for the
replay demonstration. Rejected as the *primary* signal because 328×240 after dark is where
hand-tuned thresholds fail worst, and that is precisely when blockages still need detecting. Kept
as the prescreen, which is the part it does well.
