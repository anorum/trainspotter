# Blockade — SE Portland Rail Crossing Blockage Detection

**Status:** design / pre-implementation
**Audience:** Claude Code (implementation agent) and the repo owner
**Repo intent:** public, open source, portfolio-quality

---

## 1. Problem

Union Pacific's Brooklyn Yard sits less than a mile from the SE 11th/12th Ave grade
crossings in inner Southeast Portland. Modern freight trains are longer than the yard,
so trains routinely park across public streets while the far end is worked in the yard.
Blockages of 10–60 minutes are normal, several times a day.

There is no public feed of freight train positions. UP publishes nothing. The only prior
open attempt (`isatrainblocking11th.com`) depended on a webcam in a private office
window and went offline when that office moved.

**This project detects blockages from public ODOT traffic camera imagery, alerts before
you leave the house, and — because nobody archives this data — builds the first
longitudinal record of these crossings in order to predict clear times.**

### Explicit scope

| In scope | Out of scope |
| --- | --- |
| Long blockages (≥ 5 min) | Sub-minute blockages |
| The six SE crossings listed below | Central Eastside / Albina crossings (phase 2 candidate) |
| ~1 minute detection latency | Real-time / sub-second |
| Notification + historical prediction | Routing / navigation |

Latency tolerance is the single most important non-functional requirement. It is what
makes 30–60s camera polling acceptable, permits 10-minute Flink checkpoints, and keeps
the whole thing runnable on a homelab k3s cluster.

---

## 2. Data sources

### 2.1 ODOT TripCheck cameras (primary)

- Developer portal: `https://apiportal.odot.state.or.us/product/tripcheck-data-api`
  (free API key; the "TripCheck Data API" product includes a camera inventory endpoint).
- ODOT permits private and organizational use of TripCheck images via a TripCheck
  Travel Information Portal (TTIP) account. **Read the current terms before publishing
  and record the date read in this file.**
- Working auth/caching reference implementation: `github.com/zigsphere/odot-cameras`.
- **ODOT does not archive images.** Each image is overwritten by the next. This is why
  the project has value: the history does not exist anywhere else.

Target cameras (names as they appear in the TripCheck camera list — resolve to numeric
IDs via the inventory endpoint during Phase 0):

| Camera name | Role |
| --- | --- |
| Portland - 11th at Milwaukie N | 11th northbound approach |
| Portland - 11th at Milwaukie S | 11th southbound approach |
| Portland - 12th at Clinton | 12th, closest to the rails |
| Portland - 12th at Division | 12th northern approach |
| Portland - 8th at Division | 8th approach |
| Portland - 8th at Division Place | 8th approach |

> **Phase 0 gate:** visually inspect all six feeds before writing any detector. Confirm
> framing, day/night usability, and actual refresh interval. Some may not be usable.
> Record findings in `docs/camera-survey.md` with sample frames.

### 2.2 TriMet GTFS-RT (false-positive suppression)

MAX Orange Line crosses the same streets but clears in seconds. Vehicle positions
publish roughly every 30s. Used to suppress queue spikes caused by light rail rather
than freight. Requires a free TriMet developer API key.

### 2.3 FRA National Highway-Rail Crossing Inventory (dimension table)

Static download from FRA safety data. Provides official crossing IDs, coordinates,
track counts, and warning device types. Load once into Iceberg as a slowly-changing
dimension; join for stable crossing identity in outputs.

### 2.4 FRA Blocked Crossing Incident Reports (weak labels)

Crowdsourced public reports. **FRA explicitly states it does not verify accuracy and
that the data is not a representative sample.** Use only as weak positive labels for
detector evaluation — never as ground truth and never as a training target.

---

## 3. Detection strategy

The cameras point at roadways, not necessarily at the rails. Do not try to detect
trains. **Detect the traffic queue.** This is what the prior art did, and it is the
correct read of the available imagery.

Three-layer approach, each independently useful:

1. **Per-camera queue estimate.** Vehicle detection (small YOLO-class model, ONNX
   runtime, CPU-only) over a hand-drawn ROI polygon per camera covering the approach
   lanes. Output: vehicle count in ROI, plus a cheap motion proxy (mean absolute frame
   difference within ROI, which distinguishes a stopped queue from flowing traffic).
2. **Cross-camera consensus.** Simultaneous stopped queues on multiple approaches to
   the same crossing is a far stronger signal than any single camera. This is the main
   reason to use all six.
3. **MAX suppression.** If a GTFS-RT Orange Line vehicle was within the crossing
   geofence in the preceding ~90s, discount the spike.

Do not train a custom model. Start with a pretrained detector and tune thresholds
against hand-labeled frames. Model quality is not the interesting part of this project
and is not where effort should go.

### Calibration

Phase 0 collects raw frames for at least two weeks before the detector is finalized.
Hand-label 200–400 frames across day/night/rain into `data/labels/`. Report
precision/recall at the *blockage session* level, not the frame level — a few dropped
frames mid-blockage don't matter, a hallucinated 20-minute session does.

---

## 4. Architecture

```
 ODOT TripCheck API ──┐
                      │  poller (CronJob or Deployment, 30s tick)
                      ├──> MinIO: frames/{camera_id}/{yyyy}/{mm}/{dd}/{ts}.jpg
                      └──> Kafka: crossing.frames.v1   (metadata + object key ONLY)
                                        │
                                        v
                          detector service (K8s Deployment)
                          consumes frames.v1, loads image from MinIO,
                          runs ROI vehicle count + motion proxy
                                        │
                                        v
                             Kafka: crossing.detections.v1
                                        │
 TriMet GTFS-RT ──> poller ──> Kafka: transit.vehicles.v1
                                        │
                                        v
                    ┌───────────────────────────────────────┐
                    │  Flink Job A — FusionJob              │
                    │  keyBy crossing_id                    │
                    │  - tumbling 1 min aggregation         │
                    │  - cross-camera consensus             │
                    │  - MAX suppression (interval join)    │
                    │  - hysteresis / debounce              │
                    └───────────────────────────────────────┘
                                        │
                             Kafka: crossing.state.v1
                                        │
                    ┌───────────────────────────────────────┐
                    │  Flink Job B — SessionJob             │
                    │  keyBy crossing_id                    │
                    │  - session window, 5 min gap          │
                    │  - emits open + closed sessions       │
                    └───────────────────────────────────────┘
                          │                    │
                          v                    v
                 MQTT ──> Home Assistant    Iceberg (MinIO)
                                            raw_detections
                                            crossing_state
                                            blockage_sessions
```

### Why Kafka and Flink here (be honest in the README)

At this volume a cron job and Postgres would work. The justifications that are actually
real, and should be stated as such:

- **Session windows with gap timeouts** over a noisy binary signal are genuinely
  awkward in SQL and natural in Flink.
- **Stream-stream interval join** for MAX suppression is a real streaming problem.
- **Replayability** — Kafka retention plus Iceberg history means the detector can be
  re-tuned and the entire history reprocessed through the same code path. This is the
  strongest argument and should be demonstrated, not just claimed.

Do not oversell it. A README that says "this is more infrastructure than the problem
strictly needs, and here's what it buys" reads better than one that pretends otherwise.

### Component sizing (homelab k3s)

| Component | Config |
| --- | --- |
| Kafka | Strimzi, KRaft mode, **1 broker**, 1Gi heap, RF=1, 3 partitions/topic, 7d retention |
| Flink | Flink Kubernetes Operator, Application mode, 1 JM @ 1Gi, 1 TM @ 2Gi / 2 slots |
| State backend | RocksDB on local PV, checkpoints to MinIO, **10 min interval** |
| Object store | MinIO (existing) |
| Catalog | Iceberg JDBC catalog on small Postgres |
| Detector | 1 replica, CPU-only ONNX, ~500m CPU |

RF=1 and a single broker are deliberate. Note it in the README as a homelab tradeoff
with the production alternative stated.

---

## 5. Schemas

Use Avro with a schema registry if you want the full story; JSON Schema is acceptable
and simpler. Pick one in Phase 2 and record the decision in an ADR.

```jsonc
// crossing.frames.v1  — key: camera_id
{
  "camera_id": "odot-1234",
  "crossing_id": "SE_11TH_MILWAUKIE",
  "captured_at": "2026-08-08T14:32:07Z",   // EVENT TIME
  "fetched_at":  "2026-08-08T14:32:09Z",
  "object_key":  "frames/odot-1234/2026/08/08/1754663527.jpg",
  "content_hash": "sha256:...",             // dedupe: ODOT may serve a stale image
  "image_bytes": 48213
}
```

```jsonc
// crossing.detections.v1  — key: camera_id
{
  "camera_id": "odot-1234",
  "crossing_id": "SE_11TH_MILWAUKIE",
  "captured_at": "2026-08-08T14:32:07Z",
  "roi_vehicle_count": 14,
  "roi_motion_score": 0.02,
  "queue_occupancy": 0.81,
  "detector_version": "v0.3.1",
  "confidence": 0.88,
  "degraded": false          // night, rain, camera offline, stale hash
}
```

```jsonc
// crossing.state.v1  — key: crossing_id
{
  "crossing_id": "SE_11TH_MILWAUKIE",
  "window_start": "2026-08-08T14:32:00Z",
  "state": "BLOCKED",        // CLEAR | BLOCKED | UNKNOWN
  "cameras_reporting": 2,
  "cameras_agreeing": 2,
  "max_suppressed": false,
  "confidence": 0.91
}
```

```jsonc
// blockage_sessions (Iceberg + MQTT)
{
  "session_id": "uuid",
  "crossing_id": "SE_11TH_MILWAUKIE",
  "started_at": "2026-08-08T14:31:00Z",
  "ended_at": null,           // null while open
  "duration_seconds": null,
  "peak_queue_occupancy": 0.93,
  "is_open": true,
  "detector_version": "v0.3.1"
}
```

### Event time rules

- Event time is `captured_at`, always. Never `fetched_at`, never processing time.
- Watermark: bounded out-of-orderness, **90 seconds** (covers a missed poll cycle).
- Idle source timeout on all Kafka sources — a quiet camera must not stall watermarks
  for the whole job. This is the single most likely bug.
- `UNKNOWN` is a first-class state. A camera going dark is not a clear crossing.
- **`session_id` must be stable across the life of a session.** It is assigned when the
  session opens and never changes when the session updates or closes. Derive it
  deterministically (e.g. hash of `crossing_id` + `started_at`) rather than generating a
  fresh UUID per emission. Any downstream consumer — API, frontend, future notifier —
  uses it for idempotency, and an unstable ID means duplicate alerts. This must be
  correct in Phase 2; it is expensive to retrofit.

---

## 6. Iceberg layer

Tables (namespace `blockade`):

| Table | Partitioning | Notes |
| --- | --- | --- |
| `raw_detections` | `days(captured_at)` | Append-only, the replay source of truth |
| `crossing_state` | `days(window_start)` | 1-min resolution |
| `blockage_sessions` | `days(started_at)` | The analytical core |
| `crossings` | unpartitioned | FRA inventory dimension, static |

Small-file management is the point of this layer, not an afterthought:

- Checkpoint interval **is** the file-size knob. 10 minutes at this volume.
- Sink parallelism 1. Do not scale it up "for safety."
- CronJobs from day one: `rewrite_data_files` (nightly), `rewrite_manifests` (weekly),
  `expire_snapshots` (7d retention, nightly), `remove_orphan_files` (weekly).
- Track and chart file count and snapshot count over time. Put the before/after
  compaction numbers in the README — that graph is the most credible artifact in the
  whole repo.

Verify current pyiceberg maintenance support before adding a Spark container solely to
run stored procedures.

---

## 7. Outputs

**The dataset is the product.** The pipeline's job ends at `blockage_sessions`.
Everything below is a client of that dataset, and none of it is on the critical path —
the project is worth having even if only the tables exist.

Serving design is deliberately deferred to implementation. The one constraint the
pipeline must satisfy: Iceberg is an analytical format with no point lookups, so
"is it blocked right now" cannot be answered from it. A serving store fed from the same
Flink job is required; the Postgres instance already present for the Iceberg catalog is
the obvious candidate. Decide the details at build time, not now.

**Home Assistant.** MQTT sensors per crossing: `state`, `duration_minutes`,
`predicted_clear_minutes`, `confidence`. Notification fires on session open past a
threshold, not on every state flip. Phrase it as predicted clear time, not just
"blocked" — that's the part that's actually useful.

**Prediction (Phase 5, needs ~3 months of history).** Given hour-of-day, day-of-week,
and elapsed duration so far, estimate P(clear within N minutes). Start with an
empirical conditional survival curve computed from `blockage_sessions` — a hazard/
survival estimate, not a classifier. It's more honest, more interpretable, and works
with far less data than anything learned.

**Public status page (optional).** Static site reading a small JSON blob. Fills the
gap left by `isatrainblocking11th.com`.

---

## 8. Phased plan

### Phase 0 — Capture (start immediately, before any infrastructure)

**Nothing else matters more than starting the clock on data collection.**

- [ ] Register for TripCheck API key + TTIP account; record terms review date
- [ ] Pull camera inventory, resolve the six camera IDs, save inventory to repo
- [ ] Visually survey all six feeds; write `docs/camera-survey.md` with sample frames
- [ ] Measure actual refresh interval per camera over 24h
- [ ] Single Python script: poll every 30s → write JPEG to disk + append JSONL manifest
- [ ] Dedupe by content hash (ODOT may re-serve identical frames)
- [ ] Run it on a Pi. Systemd unit. Do not containerize yet.
- [ ] Register TriMet API key and start capturing GTFS-RT to JSONL in parallel

Exit criteria: two weeks of continuous frames and a camera survey doc. **The JSONL
manifest is the backfill source for the entire pipeline later** — this is not throwaway
work, it's the replay corpus.

### Phase 1 — Detector

- [ ] ROI polygon editor (tiny HTML page, click-to-draw, saves JSON per camera)
- [ ] Vehicle count + motion proxy over ROI, ONNX CPU
- [ ] Hand-label 200–400 frames spanning day/night/rain
- [ ] Threshold tuning; report session-level precision/recall
- [ ] Package as a service that reads from Kafka and writes to Kafka (but can also run
      in file mode against Phase 0 JSONL — keep both paths)

### Phase 2 — Streaming

- [ ] Strimzi KRaft single broker; topics with schemas
- [ ] Port the Phase 0 poller into a K8s Deployment writing frames to MinIO + Kafka
- [ ] Flink Kubernetes Operator; FusionJob then SessionJob
- [ ] **Backfill:** replay the entire Phase 0 corpus through the pipeline with
      event-time watermarks. Document it. This is the demo.

### Phase 3 — Iceberg

- [ ] JDBC catalog on Postgres; tables above
- [ ] Flink Iceberg sinks; verify commit-on-checkpoint behavior
- [ ] All four maintenance CronJobs
- [ ] File/snapshot count dashboard

### Phase 4 — Alerting

- [ ] MQTT sink → Home Assistant discovery
- [ ] Threshold + debounce logic; notification templates

### Phase 5 — Prediction

- [ ] Survival curves from `blockage_sessions`
- [ ] Feed back into the MQTT payload
- [ ] dbt models over Iceberg for the analytical layer

---

## 9. Repo layout

```
blockade/
├── README.md                  # honest about tradeoffs; include compaction graph
├── DESIGN.md                  # this file
├── docs/
│   ├── camera-survey.md
│   └── adr/                   # 0001-catalog-choice.md, 0002-schema-format.md, ...
├── capture/                   # Phase 0 poller — keep it, it's the backfill tool
├── detector/
│   ├── rois/                  # per-camera ROI polygons
│   └── labels/
├── flink/
│   ├── fusion-job/
│   └── session-job/
├── deploy/
│   ├── strimzi/
│   ├── flink-operator/
│   ├── iceberg-maintenance/   # CronJobs
│   └── minio/
├── analytics/                 # dbt project
└── demo/                      # docker-compose + synthetic frame generator
```

`demo/` matters. A synthetic frame generator plus `docker compose up` is what makes a
public repo usable by someone who doesn't have an ODOT key. Budget real time for it.

---

## 10. Constraints and non-negotiables

1. **No sensors on or near railroad property.** Camera imagery is public and remote.
   Nothing physical goes anywhere near the tracks — that's federal trespassing.
2. **Respect ODOT rate limits.** Cache aggressively (see the zigsphere reference).
   Getting the key revoked ends the project.
3. **Never put image bytes in Kafka.** Object storage plus a reference. Non-negotiable.
4. **No personal data anywhere.** Frames are public roadway imagery; do not build or
   store anything that identifies vehicles or people. No plate reading, ever. State
   this explicitly in the README.
5. **Attribute ODOT and TriMet** per their terms in the README.

---

## 11. Open decisions

- Avro + schema registry vs. plain JSON Schema (lean JSON for a solo homelab project)
- Iceberg catalog: JDBC-on-Postgres (simplest) vs. REST catalog such as Lakekeeper
  (more portable, better story) — ADR this
- Whether any camera has direct sightline to the rails; if one does, add a direct
  train-detection signal as a second opinion
- Whether to extend to the Central Eastside crossings after Phase 4

---

## 12. First instruction for Claude Code

Do **not** start with Kubernetes manifests. Start with Phase 0: get the API key
registered, resolve the six camera IDs from the inventory endpoint, survey the feeds
visually, and get the polling script running on a Pi tonight. Every day without capture
is a day missing from the prediction corpus, and the streaming layer can backfill from
JSONL whenever it's ready.