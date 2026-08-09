# Camera survey

**Status: not started — blocked on the ODOT API key.**

The Phase 0 gate. Visually inspect all six feeds before writing any detector, and record what is
actually there rather than what the camera name implies. Some cameras will not be usable, and
finding that out now is much cheaper than discovering it while tuning thresholds.

Fill this in after 24–48 hours of capture. Update `usability` in `config/cameras.yaml` to match —
Phase 1 reads that field to decide which cameras to trust.

## How to run the survey

```bash
# Measured refresh interval per camera, from the manifest rather than by assumption
uv run python -c "
import json, pathlib, collections
from datetime import datetime
for cam in sorted(pathlib.Path('var/manifests').iterdir()):
    stamps = sorted({json.loads(l)['content_hash']: json.loads(l)['captured_at']
                     for f in cam.glob('*.jsonl') for l in f.read_text().splitlines()
                     if json.loads(l)['content_hash']}.values())
    gaps = [(datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
            for a, b in zip(stamps, stamps[1:])]
    if gaps:
        gaps.sort()
        print(f'{cam.name:24} n={len(gaps):5} median={gaps[len(gaps)//2]:6.0f}s '
              f'p90={gaps[int(len(gaps)*0.9)]:6.0f}s')
"
```

Deduped hashes are used deliberately: polling every 30s says nothing about how often the *camera*
refreshes, and that interval sets the real detection latency.

## Per-camera findings

For each camera, attach sample frames at **noon, dusk, night, and in rain**, then record:

| Field | What to record |
| --- | --- |
| Framing | Does the view actually cover the approach lanes? How much of the frame is usable road? |
| Sightline to rails | Any direct view of the tracks? If yes, that camera can carry a second, independent train signal |
| Day usability | Can queued vehicles be distinguished from moving ones? |
| Night usability | Headlights vs. vehicle bodies; is a count meaningful after dark? |
| Rain/fog | Does the image degrade to unusable, and how often |
| Measured refresh | Median and p90 seconds between distinct images |
| Obstructions | Poles, signage, overexposure, lens dirt, frozen frames |
| Verdict | `usable` / `marginal` / `unusable` |

### Portland - 11th at Milwaukie N — `SE_11TH_MILWAUKIE`
_Not yet surveyed._

### Portland - 11th at Milwaukie S — `SE_11TH_MILWAUKIE`
_Not yet surveyed._

### Portland - 12th at Clinton — `SE_12TH_CLINTON`
_Not yet surveyed._ Expected to be the camera closest to the rails.

### Portland - 12th at Division — `SE_12TH_CLINTON`
_Not yet surveyed._

### Portland - 8th at Division — `SE_8TH_DIVISION`
_Not yet surveyed._

### Portland - 8th at Division Place — `SE_8TH_DIVISION`
_Not yet surveyed._

## Summary

| Crossing | Cameras usable | Consensus possible? |
| --- | --- | --- |
| SE_11TH_MILWAUKIE | ? / 2 | ? |
| SE_12TH_CLINTON | ? / 2 | ? |
| SE_8TH_DIVISION | ? / 2 | ? |

Cross-camera consensus needs at least two usable cameras per crossing. A crossing left with one
usable camera still produces a signal, but with materially lower confidence — record that here so
the confidence values in `crossing.state.v1` can be justified rather than invented.

## Hand-timestamped blockages

Phase 0 also needs at least three blockages observed and timestamped by hand. These are the seed
labels for Phase 1 threshold tuning, and the only ground truth this project will ever have — the
FRA blocked-crossing reports are explicitly unverified and not a representative sample, so they
are used as weak evaluation labels only, never as a training target.

| # | Crossing | Observed start (UTC) | Observed end (UTC) | How observed | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
