# Blockade

Detects freight train blockages at SE Portland grade crossings from public traffic camera imagery, alerts before you leave the house, and builds the longitudinal record needed to predict when they clear.

Union Pacific's Brooklyn Yard sits less than a mile from the SE 11th/12th Ave grade crossings.
Modern freight trains are longer than the yard, so trains routinely park across public streets while the far end is worked.
Blockages of 10-60 minutes are normal, several times a day.

There is no public feed of freight train positions.
**ODOT does not archive its camera images** - each is overwritten by the next - so the history this project builds does not exist anywhere else.
That is the point of the project.

Architecture: [docs/architecture.md](docs/architecture.md) - what each piece owns and the contracts between them.
Decisions: [docs/adr/](docs/adr/).
Original proposals, kept as history: [docs/history/](docs/history/).

**Status: live.**
Capturing six cameras continuously since 2026-08-08; the full pipeline - detection, sessions, alerts, the web board, and the Postgres history store - runs on the k3s cluster and serves [blockade.home.alexnorum.com](http://blockade.home.alexnorum.com) on the LAN.
The first per-camera trained classifier (12th & Clinton) is in production behind the auto router, and its improved history has been backfilled.

---

## What it actually does

```
ODOT/PBOT camera stills
        |
   [capture]        6 cameras, 30s tick, conditional GET, sha256 dedupe
        |           frames to disk + append-only JSONL manifest
        v
   [detector]       interchangeable: reference | vlm | classifier | auto
        |           one ObservationRecord per crossing per tick
        v
   raw observations   BLOCKED / CLEAR / UNKNOWN + confidence
        |
        +--> [alert branch]      rising edge + asymmetric reset -> one alert per train
        |
        +--> [analytics branch]  sessions with gap timeout -> start, end, duration
        |
        +--> [serving layer]     tails Kafka groupless, reducer builds the board
                                 live status at blockade.home.alexnorum.com
                                 frames from S3, SSE for the browser
```

One detection, one event, three consumers.
The alert branch answers "should I leave now"; the analytics branch answers "how often and for how long", and can be rebuilt from the observations whenever its parameters change.
The serving layer answers "is a train blocking right now" with a live schematic board.

Image bytes never enter the message bus - object storage plus a reference, always.

## Detection

Four detectors, all satisfying the same [`Detector`](libs/blockade-core/src/blockade/detect/base.py) protocol and selected by config:

| `BLOCKADE_DETECTOR` | What it is | Cost |
| --- | --- | --- |
| `reference` (library default) | Differencing against a median image of the empty crossing | free |
| `vlm` | Claude Haiku reads the scene | ~$0.0003/frame |
| `classifier` | Per-camera MobileNetV3-small head, trained offline, run as ONNX | free, CPU |
| `auto` (deployed) | Per-camera router: `classifier` where `references/` has a trained model, `reference` everywhere else | free, CPU |

Which is best is an open question that only more data answers, so swapping is a config change and every row records the `detector_version` that produced it.
Rows from different detectors are never silently mixed.
`auto` exists because the detector knob is global but the truth is per camera - flipping the whole fleet to `classifier` would turn every untrained camera into permanent `UNKNOWN`, so the router ships the trained model only where one exists on disk and falls back to `reference` otherwise.

**`UNKNOWN` is a first-class answer.**
A dark camera, a decode failure, or an unfamiliar lighting condition is a gap in coverage, which the dataset can record honestly.
Collapsing it to "not blocked" would assert something false about hours the camera could not see, and every statistic built on top would inherit that.

### What the frames actually show

The cameras see the **rails**, not just the roadway.
The original design (docs/history/design.md) assumed otherwise and proposed detecting the traffic queue as a proxy; daylight frames show track structure and crossing signals directly, so the detector looks for a train rather than inferring one from stopped cars.
That is a stronger signal: a queue of stopped vehicles might be a train, a red light, or rush hour, whereas a train on the tracks is either visible or it is not.

Four of the six do, anyway.
The other two turned out to watch a neighbouring intersection with their crossing out of frame, so they carry `scores: false` in the roster: still captured, still shown on the board, never judged, because a view with no crossing in it can only contribute traffic noise.
[docs/camera-survey.md](docs/camera-survey.md) records which ones and why.

At night a train reads as a long horizontal mass spanning the frame, hiding the road markings and the far side of the intersection.
That contrast is unmistakable even at 328x240.

## Honest limitations

**The false-positive rate is unmeasured, not high.**
An earlier version of this file claimed roughly fifteen spurious sessions a day.
That number was never measured - it was `sessions found` minus `sessions independently confirmed`, which silently assumes an unconfirmed detection is a wrong one.
It is not: freight movements through Brooklyn Yard are frequent, and one session picked out as a textbook false positive turned out, on checking, to be a real train.
Over 36 hours the detector finds about ten sessions a day on one camera, which matches what the original design describes as normal for these crossings.

Measuring it properly needs ground truth that nobody can produce by watching a camera around the clock, which is the strongest argument for building the alert path early: an alert is a prompt to go and check, so the system generates its own verification.

**Accuracy claims are small-sample.**
Three confirmed blockages, all on one camera, all reported by one observer.
Two of the three validations turned out to contain luck, documented in the commit history rather than quietly dropped.

**Per-camera calibration does not scale by hand.**
Cameras differ enough to need different settings, which is why the reference, the track band, and the thresholds are derived from the camera's own labeled frames and carried with the model rather than configured.

## What this deliberately does not do

- **No sensors on or near railroad property.** Camera imagery is public and remote. Nothing physical goes anywhere near the tracks.
- **No personal data, ever.** Frames are public roadway imagery. Nothing here identifies vehicles or people, and there is no plate reading. The detector compares a frame against what the empty crossing looks like; that is all it does.
- **No custom GPU serving.** Inference is CPU-only.

## Attribution

Camera imagery courtesy of the **Portland Bureau of Transportation** (PBOT), served via the **Oregon Department of Transportation** TripCheck API.
None of them endorse this project.
See their respective terms of use.

---

## Running it

```bash
uv sync
cp .env.example .env                      # then add the ODOT key

uv run blockade-inventory resolve         # writes config/cameras.yaml
uv run blockade-capture once              # pre-flight: poll each camera once
uv run blockade-capture run --local-only  # continuous; metrics on :9102
```

`resolve` exits non-zero and names any camera it cannot find.
Cameras get renamed and decommissioned, and silently capturing five of six would not be obvious for weeks.

`--local-only` runs capture with no object store - useful for development.
In that mode the local frames are the only copy, so the cache TTL is disabled and frames are kept indefinitely; budget roughly 100 MB/day.
Production runs with S3, where every frame lands in `pdx-trainspotter` and `blockade-sync` repairs any gap.

On macOS, [deploy/local/com.blockade.capture.plist](deploy/local/com.blockade.capture.plist) runs capture as a LaunchAgent.
It only runs while you are logged in and the machine is awake, which is why the durable home is the k3s Deployment in [deploy/poller/](deploy/poller/).

### Credentials

Only one is required to start.
Register at [apiportal.odot.state.or.us](https://apiportal.odot.state.or.us/), subscribe to the TripCheck Data product, and take the key from your profile.
It is spent on the camera inventory alone, which refreshes every 24 hours, so it costs about one API call per day.
Image polling goes directly to the per-camera URL and does not consume quota.

Polling is conditional (`If-None-Match` / `If-Modified-Since`), floored at 15s, and sends a `User-Agent` naming the project and a contact address.
Most polls return 304 and transfer nothing, since the cameras refresh more slowly than we poll.
Getting the key revoked ends the project.

### Tests

```bash
uv run pytest
uv run ruff check .
```

Tests run against recorded HTTP fixtures rather than hand-written fakes, and the label set in [data/labels/](data/labels/) records whether each judgement came from visual inspection or from a human observer, because those are not equally authoritative.
Hand-labeled BLOCKED training frames live in [data/blocks/](data/blocks/), one directory per camera, kept in git for the same reason as the labels: slow to produce, and reproducible from nothing else once ODOT overwrites the image.

The site the API serves is an Astro app with its own toolchain, gated by the separate `web` workflow so a Python-only change never pays for an npm install:

```bash
cd services/api/web
npm ci
npm run check    # astro check, against a strict tsconfig; `astro build` does not typecheck
npm test         # vitest, over the board logic the server cannot answer for
npm run build
```

## Licence

MIT.
