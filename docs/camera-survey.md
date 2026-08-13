# Camera survey

**Status: closed 2026-08-12.** Capture started 2026-08-08 21:24 PDT; all six cameras resolved and
have been returning frames continuously since. The survey's job is done: usability now shows up
empirically as per-camera detector performance (odot-682 cannot resolve the tracks at night, which
the blocked-biased consensus absorbs; odot-679's view favors Division over the Clinton crossing),
and the per-camera model workflow in [docs/architecture.md](architecture.md) replaced the idea of
a hand-maintained usability field. The night-one findings below stand as the record.

## Findings that already change the design

**1. The images are much smaller than assumed: 328×334, ~20 KB.**
Roughly 90 px of that is burned-in chrome (a title/timestamp header and a "Camera courtesy of
PBOT" footer), leaving about **328×240 of usable roadway**. At that size a vehicle a block back is
a few dozen pixels. This is a real constraint on Phase 1: a general-purpose detector will struggle,
and the motion proxy is likely to carry more of the signal than docs/history/design.md assumed.
Every ROI polygon must exclude the chrome bands, or the frame-difference score will fire on the
ticking timestamp rather than on traffic.

**2. These are PBOT cameras, not ODOT ones**, served through TripCheck. Attribution in the README
should credit PBOT as well as ODOT.

**3. The cameras see the tracks directly - which overturns docs/history/design.md §3.**

Confirmed in daylight (2026-08-09 09:33 PDT) on both `odot-676` (11th at Milwaukie N) and
`odot-678` (12th at Clinton): the rails are plainly visible crossing the roadway, along with
crossing-signal masts. `odot-676` shows several tracks of the rail corridor curving through frame.

docs/history/design.md §3 says: *"The cameras point at roadways, not necessarily at the rails. Do
not try to detect trains. Detect the traffic queue."* In daylight that premise does not hold. The
queue heuristic was a workaround for a limitation these cameras do not have.

This matters more than it first appears. Detecting the queue is an **inference** - a line of
stopped cars might be a train, a red light, a delivery truck, or rush hour. Detecting a train on
the tracks is an **observation**. The first needs thresholds tuned per camera and produces
arguable results; the second is either visible or it is not.

docs/history/design.md §11 asked "whether any camera has direct sightline to the rails; if one
does, add a direct train-detection signal as a second opinion." The answer is that the direct
signal should be the *primary* one during daylight, with the queue as the fallback - the reverse of
the original design. Night remains open (see below).

**4. The burned-in timestamp agrees with the `Last-Modified` header.**
The header reads "Aug 08 2026 9:21 PM" and `Last-Modified` gave 04:21 UTC - the same instant. Event
time from `Last-Modified` is therefore trustworthy, and the burned-in text is a cross-check
available if a camera's header ever starts lying.

**5. A seventh useful camera exists.** Device 1250, "Portland - Division at 12th", sits 53 m from
the target set and gives another angle on the 12th crossing. Not enabled; worth adding if the
survey finds any of the paired cameras unusable.


The Phase 0 gate. Visually inspect all six feeds before writing any detector, and record what is
actually there rather than what the camera name implies. Some cameras will not be usable, and
finding that out now is much cheaper than discovering it while tuning thresholds.

Fill this in after 24-48 hours of capture.

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

### Portland - 11th at Milwaukie N - `odot-676`, `SE_11TH_MILWAUKIE`
45.50329, -122.65457 · labelled "212 - SE Milwaukie @ Gideon St"
Night: streetlights and headlights dominate; vehicle bodies are hard to separate from background.
**Rail structure visible in frame** - candidate for a direct train signal. Day/dusk/rain pending.
Verdict: **pending**.

### Portland - 11th at Milwaukie S - `odot-677`, `SE_11TH_MILWAUKIE`
45.50314, -122.65414 · labelled "213 - SE Milwaukie @ Gideon St"
Also overlooks the rail corridor. Darker frame than 676; a large unlit region occupies the left
third. Verdict: **pending**.

### Portland - 12th at Clinton - `odot-678`, `SE_12TH_CLINTON`
45.50360, -122.65381 · labelled "214 - SE 12th @ Clinton"
Clean view straight down the roadway with the intersection centred - the most promising framing for
queue detection of the four seen so far. Verdict: **pending**.

### Portland - 12th at Division - `odot-679`, `SE_12TH_CLINTON`
45.50494, -122.65360 · largest payload of the six (~29 KB), suggesting more scene detail.
Verdict: **pending**.

### Portland - 8th at Division - `odot-681`, `SE_8TH_DIVISION`
45.50573, -122.65745 · ~300 m from the crossings, the furthest of the set. Verdict: **pending**.

### Portland - 8th at Division Place - `odot-682`, `SE_8TH_DIVISION`
45.50476, -122.65796 · Verdict: **pending**.

### Portland - Division at 12th - `odot-1250` (not enabled)
45.50493, -122.65372 · A second angle on the 12th crossing, 53 m from `odot-679`. Held in reserve.

## Summary

Cross-camera consensus needs at least two usable cameras per crossing. A crossing left with one
usable camera still produces a signal, but with materially lower confidence, which is why the
confidence on every `crossing.observations.v1` record is per camera and the board's consensus is
blocked-biased rather than a vote.

The per-crossing usability tally this section was going to hold was never filled in, and is not
worth reconstructing: usability turned out to be per camera and per lighting condition rather than
a single verdict, and it now shows up empirically in detector performance instead.

## Hand-timestamped blockages

Phase 0 also needs at least three blockages observed and timestamped by hand. These are the seed
labels for Phase 1 threshold tuning, and the only ground truth this project will ever have - the
FRA blocked-crossing reports are explicitly unverified and not a representative sample, so they
are used as weak evaluation labels only, never as a training target.

Those observations live in `data/labels/sessions.jsonl`, which carries the full record: observed
start and end in both local and UTC, the precision of each boundary, the observer, and what the
detector said about the same frames. That file is the record; the table drafted here never was.
