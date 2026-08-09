# Blockade

Detects freight train blockages at SE Portland grade crossings from public ODOT traffic camera
imagery, and builds the longitudinal record needed to predict when they will clear.

Union Pacific's Brooklyn Yard sits less than a mile from the SE 11th/12th Ave grade crossings.
Modern freight trains are longer than the yard, so trains routinely park across public streets
while the far end is worked. Blockages of 10–60 minutes are normal, several times a day.

There is no public feed of freight train positions. **ODOT does not archive its camera images** —
each is overwritten by the next — so the history this project builds does not exist anywhere else.
That is the point of the project.

See [DESIGN.md](design.md) for the full design.

**Status:** Phase 0 (capture). **Collecting since 2026-08-08.** All six cameras resolved and
returning frames; see [docs/camera-survey.md](docs/camera-survey.md).

---

## What this is honestly worth

The dataset is the product. The pipeline's job ends at `blockage_sessions`; everything downstream
is a client of that table, and the project is worth having even if only the tables exist.

**This is more infrastructure than the problem strictly needs.** At this volume — six cameras, one
frame per 30 seconds — a cron job and a Postgres table would work. What the streaming layer
actually buys:

- **Session windows with gap timeouts** over a noisy binary signal are genuinely awkward in SQL
  and natural in Flink.
- **Stream-stream interval joins** for MAX light-rail suppression are a real streaming problem.
- **Replayability.** Kafka retention plus Iceberg history means the detector can be re-tuned and
  the entire history reprocessed through the same code path. This is the strongest argument, and
  the repo demonstrates it rather than claiming it.

Single Kafka broker, RF=1. That is a homelab tradeoff, not a recommendation; in production this
would be three brokers with RF=3.

## What this deliberately does not do

- **No sensors on or near railroad property.** Camera imagery is public and remote. Nothing
  physical goes anywhere near the tracks.
- **No personal data, ever.** Frames are public roadway imagery. Nothing here identifies vehicles
  or people, and there is no plate reading. The detector counts vehicle-shaped blobs in a hand-drawn
  region and measures whether they are moving. That is all it does.
- **No train detection.** The cameras point at roadways, not at the rails. The system detects the
  *traffic queue*, which is what the available imagery actually supports.

## Attribution

Camera imagery courtesy of the **Portland Bureau of Transportation** (PBOT), served via the
**Oregon Department of Transportation** TripCheck API. Transit data courtesy of **TriMet**. None of
them endorse this project. See their respective terms of use.

---

## Architecture

```
ODOT TripCheck ──> poller ──> S3 frames/  +  local cache (detector reads here)
                      │
                      └──> Kafka crossing.frames.v1 (metadata + object key only)
                                    │
                              detector (ONNX, CPU) ──> crossing.detections.v1
                                    │
TriMet GTFS-RT ──> poller ──> transit.vehicles.v1
                                    │
                        Flink FusionJob  ──> crossing.state.v1
                        Flink SessionJob ──> blockage_sessions
                                    │
                   ┌────────────────┼────────────────┐
                   v                v                v
              Iceberg/S3        Postgres         MQTT / Home Assistant
             (analytics)      (serving API)
```

Image bytes never enter Kafka — object storage plus a reference, always.

### Why frames are written twice

Each frame goes to S3 *and* a local PVC cache. Compute runs in k3s at home while storage is in
AWS, so reading frames back from S3 is billed egress — and the detector re-reads the entire corpus
every time thresholds are re-tuned. The cache makes recurring egress ~zero while S3 remains the
durable replay corpus, retrievable from anywhere.

---

## Getting set up

### Credentials

| What | Where | Needed for | Blocking? |
| --- | --- | --- | --- |
| **ODOT TripCheck Data API key** | [apiportal.odot.state.or.us](https://apiportal.odot.state.or.us/) → Sign up → verify email → **Products** → **TripCheck Data** → name a subscription, accept the Terms of Use, **Subscribe**. Keys appear on **Profile**. Instant, free, no approval wait. You receive **two** keys. | Camera inventory only | **Yes** — resolves camera IDs |
| **Terms of Use review** | The "Show" link beside the agree checkbox at subscribe time, plus the [Getting Started Guide](https://www.tripcheck.com/pdfs/TripCheckAPI_Getting_Started_GuideV5.pdf) | Permission to archive imagery | **Yes — gate** |
| **TriMet AppID** | [developer.trimet.org/appid/registration/](https://developer.trimet.org/appid/registration/) — free | GTFS-RT, MAX suppression | No |
| **AWS IAM user** | Scoped to one bucket | Frame + Iceberg storage | Yes |

The subscription key is spent on the **camera inventory only**, which ODOT refreshes every 24 hours
— roughly one API call per day. Image polling goes directly to the per-camera `cctv-url` and does
not consume API quota. Polling is nevertheless conditional (`If-None-Match` / `If-Modified-Since`),
floored at 15s, and sends a `User-Agent` naming the project and a contact address. Getting the key
revoked ends the project.

The cameras refresh about every 60 seconds, so roughly half of all polls return 304 and transfer
nothing. That is the main reason the whole archive costs a couple of dollars a month.

**Read the imagery terms before starting continuous capture.** If archiving is restricted, the
shape of this project changes. Record the date reviewed in [design.md](design.md) §2.1.

### Install

```bash
uv sync                       # Python 3.11 — PyFlink does not support 3.12 yet
cp .env.example .env          # then fill in credentials
```

### Resolve the cameras

```bash
uv run blockade-inventory fetch      # saves the raw inventory as a provenance record
uv run blockade-inventory resolve    # writes config/cameras.yaml
```

`resolve` exits non-zero and names any camera it could not find. Cameras get renamed and
decommissioned; silently capturing five of six would not be obvious for weeks.

### Capture

```bash
uv run blockade-capture once        # poll each camera once, print outcomes, verify the roster
uv run blockade-capture run         # continuous; metrics on :9102
```

`once` is the pre-flight check — run it before starting continuous capture.

### Test

```bash
uv run pytest
uv run ruff check .
```

Tests run against recorded HTTP fixtures, not hand-written fakes. S3 behaviour is exercised against
MinIO rather than a mock, so the same code path is tested that runs in production.

---

## Phases

| Phase | Status | What |
| --- | --- | --- |
| 0 — Capture | **in progress** | Poll cameras, dedupe, JSONL manifest. The manifest is the backfill corpus for everything later. |
| 1 — Detector | not started | ROI vehicle count + motion proxy, ONNX on CPU |
| 2 — Streaming | not started | Kafka + Flink; replay the Phase 0 corpus through the live pipeline |
| 3 — Iceberg | not started | Tables, compaction, and the file-count graph |
| 4 — Serving | not started | Postgres + FastAPI read API; MQTT to Home Assistant |
| 5 — Prediction | not started | Empirical survival curves; needs ~3 months of history |

Phase 0 exit criteria: 14 days of continuous capture with <1% gaps, a completed camera survey with
a usable/marginal/unusable verdict per camera, and at least three hand-timestamped blockages to
seed the Phase 1 labels.

## Licence

MIT.
