# Blockade

Detects freight train blockages at SE Portland grade crossings from public traffic camera imagery, alerts before you leave the house, and builds the longitudinal record needed to predict when they clear.

Union Pacific's Brooklyn Yard sits less than a mile from the SE 11th/12th Ave grade crossings.
Modern freight trains are longer than the yard, so trains routinely park across public streets while the far end is worked.
Blockages of 10-60 minutes are normal, several times a day.

There is no public feed of freight train positions.
**ODOT does not archive its camera images** - each is overwritten by the next - so the history this project builds does not exist anywhere else.
That is the point of the project.

Design notes: [design.md](design.md), [design2.md](design2.md).
Decisions: [docs/adr/](docs/adr/).

**Status: capturing since 2026-08-08.**
Six cameras, ~5,700 frames, zero capture errors.
Three blockages observed and recorded as ground truth; the detector found all three.

---

## What it actually does

```
ODOT/PBOT camera stills
        |
   [capture]        6 cameras, 30s tick, conditional GET, sha256 dedupe
        |           frames to disk + append-only JSONL manifest
        v
   [detector]       interchangeable: reference | yolo | vlm
        |           one ObservationRecord per crossing per tick
        v
   raw observations   BLOCKED / CLEAR / UNKNOWN + confidence
        |
        +--> [alert branch]      rising edge + asymmetric reset -> one alert per train
        |
        +--> [analytics branch]  sessions with gap timeout -> start, end, duration
```

One detection, one event, two consumers.
The alert branch answers "should I leave now"; the analytics branch answers "how often and for how long", and can be rebuilt from the observations whenever its parameters change.

Image bytes never enter the message bus - object storage plus a reference, always.

## Detection

Three detectors, all satisfying the same [`Detector`](src/blockade/detect/base.py) protocol and selected by config:

| `BLOCKADE_DETECTOR` | What it is | Cost |
| --- | --- | --- |
| `reference` (default) | Differencing against a median image of the empty crossing | free |
| `yolo` | YOLO-World open-vocabulary detection, no training | free, CPU |
| `vlm` | Claude Haiku reads the scene | ~$0.0003/frame |

Which is best is an open question that only more data answers, so swapping is a config change and every row records the `detector_version` that produced it.
Rows from different detectors are never silently mixed.

**`UNKNOWN` is a first-class answer.**
A dark camera, a decode failure, or an unfamiliar lighting condition is a gap in coverage, which the dataset can record honestly.
Collapsing it to "not blocked" would assert something false about hours the camera could not see, and every statistic built on top would inherit that.

### What the frames actually show

The cameras see the **rails**, not just the roadway.
design.md assumed otherwise and proposed detecting the traffic queue as a proxy; daylight frames show track structure and crossing signals directly, so the detector looks for a train rather than inferring one from stopped cars.
That is a stronger signal: a queue of stopped vehicles might be a train, a red light, or rush hour, whereas a train on the tracks is either visible or it is not.

At night a train reads as a long horizontal mass spanning the frame, hiding the road markings and the far side of the intersection.
That contrast is unmistakable even at 328x240.

## Honest limitations

**The false-positive rate is unmeasured, not high.**
An earlier version of this file claimed roughly fifteen spurious sessions a day.
That number was never measured - it was `sessions found` minus `sessions independently confirmed`, which silently assumes an unconfirmed detection is a wrong one.
It is not: freight movements through Brooklyn Yard are frequent, and one session picked out as a textbook false positive turned out, on checking, to be a real train.
Over 36 hours the detector finds about ten sessions a day on one camera, which is what design.md describes as normal for these crossings.

Measuring it properly needs ground truth that nobody can produce by watching a camera around the clock, which is the strongest argument for building the alert path early: an alert is a prompt to go and check, so the system generates its own verification.

**Accuracy claims are small-sample.**
Three confirmed blockages, all on one camera, all reported by one observer.
Two of the three validations turned out to contain luck, documented in the commit history rather than quietly dropped.

**Per-camera calibration does not scale by hand.**
Cameras differ enough to need different settings, which is why references and the track band are derived from data rather than configured.

## What this deliberately does not do

- **No sensors on or near railroad property.** Camera imagery is public and remote. Nothing physical goes anywhere near the tracks.
- **No personal data, ever.** Frames are public roadway imagery. Nothing here identifies vehicles or people, and there is no plate reading. The detector compares a frame against what the empty crossing looks like; that is all it does.
- **No custom GPU serving.** Inference is CPU-only.

## Attribution

Camera imagery courtesy of the **Portland Bureau of Transportation** (PBOT), served via the **Oregon Department of Transportation** TripCheck API.
Transit data courtesy of **TriMet**.
None of them endorse this project.
See their respective terms of use.

---

## Running it

```bash
uv sync                                   # Python 3.11 - PyFlink does not support 3.12
cp .env.example .env                      # then add the ODOT key

uv run blockade-inventory resolve         # writes config/cameras.yaml
uv run blockade-capture once              # pre-flight: poll each camera once
uv run blockade-capture run --local-only  # continuous; metrics on :9102
```

`resolve` exits non-zero and names any camera it cannot find.
Cameras get renamed and decommissioned, and silently capturing five of six would not be obvious for weeks.

**`--local-only` is the current mode**, since object storage is deferred until this is productionised.
In that mode the local frames are the only copy, so the 7-day cache TTL is disabled and frames are kept indefinitely - sweeping them would permanently destroy imagery ODOT overwrote long ago.
Budget roughly 100 MB/day.
`blockade-sync` uploads everything captured in the meantime once a bucket exists, so nothing is lost by deferring.

On macOS, [deploy/local/com.blockade.capture.plist](deploy/local/com.blockade.capture.plist) runs capture as a LaunchAgent.
It only runs while you are logged in and the machine is awake, which is why the durable home is the k3s Deployment in [deploy/capture/](deploy/capture/).

### Credentials

Only one is required to start.
Register at [apiportal.odot.state.or.us](https://apiportal.odot.state.or.us/), subscribe to the TripCheck Data product, and take the key from your profile.
It is spent on the camera inventory alone, which refreshes every 24 hours, so it costs about one API call per day.
Image polling goes directly to the per-camera URL and does not consume quota.

Polling is conditional (`If-None-Match` / `If-Modified-Since`), floored at 15s, and sends a `User-Agent` naming the project and a contact address.
About half of all polls return 304 and transfer nothing.
Getting the key revoked ends the project.

### Tests

```bash
uv run pytest
uv run ruff check .
```

Tests run against recorded HTTP fixtures rather than hand-written fakes, and the label set in [data/labels/](data/labels/) records whether each judgement came from visual inspection or from a human observer, because those are not equally authoritative.

## Licence

MIT.
