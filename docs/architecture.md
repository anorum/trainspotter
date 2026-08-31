# PDX Train architecture

This is the current-truth description of how PDX Train works: what each piece owns, the contracts between them, and how to operate it.
(The product is PDX Train; `blockade` remains the code namespace - see the [README](../README.md).)
The original proposals live in [docs/history/](history/) as a record of what was planned; where they disagree with this document, this document wins.

## The system in one picture

```mermaid
flowchart LR
    subgraph outside [Outside]
        cams[ODOT/PBOT cameras<br/>6 cameras, 3 crossings]
    end
    subgraph cluster [k3s cluster]
        poller[poller<br/>capture + outbox]
        detector[detector<br/>auto router]
        sessionizer[sessionizer<br/>sessions + alerts]
        api[api<br/>board + history + site]
        pg[(Postgres<br/>history store)]
    end
    s3[(S3<br/>frames, manifests,<br/>references)]
    kafka[(Kafka)]

    cams -->|poll every 30s| poller
    poller -->|frames + manifests| s3
    poller -->|crossing.frames.v1| kafka
    kafka -->|frames| detector
    s3 -->|image bytes +<br/>models| detector
    detector -->|crossing.observations.v1| kafka
    kafka -->|observations| sessionizer
    sessionizer -->|crossing.sessions.v1<br/>crossing.alerts.v1| kafka
    kafka -->|frames + observations<br/>+ sessions, tailed| api
    api --> pg
    s3 -->|frame images| api
    api -->|pdxtrain.alexnorum.com<br/>blockade.home.alexnorum.com| browser[Browser<br/>board / sheet / patterns]
    api -->|/api/v1/status| iphone[iPhone + Watch apps + widgets<br/>apps/ios]
```

One detection, one event stream, everything downstream is a consumer.
Image bytes never enter Kafka: topics carry metadata and a reference into S3, always.

## The two ownership rules

Everything about storage follows from two rules.

**Streaming owns now; batch owns history.**
The live path (poller to detector to sessionizer to the board) produces the record as it happens.
When a detector improves, history is corrected by the batch path instead: `blockade-detect scan` re-scores the kept frames and `blockade-api backfill` loads the result into Postgres.
History is never replayed through the live services - that would corrupt their state - and the backfill refuses any window that reaches within one session gap of now, because that edge still belongs to streaming.

**The live board reads memory; history surfaces read Postgres.**
"Is a train blocking right now" is answered from an in-memory state the API rebuilds by replaying Kafka on every boot - it works even with the database down.
"What happened last Tuesday" is answered only from Postgres; without it `/timeline` and `/sessions` return 503 rather than a half-true answer from a memory buffer.
`/analytics` is the one deliberate exception: it answers 200 with `{"available": false, "crossings": {}}`, because the UI needs to hide the stats surface entirely rather than render an error in place of it.

## Components

### poller (`services/poller`, deploy/poller)

Owns capture, and capture is sacred: ODOT overwrites each image within the minute, so a missed frame is data nobody can ever recover.
Polls each rostered camera every 30s with conditional GETs, dedupes by content hash, writes every frame to the local cache and S3, and appends one `FrameRecord` line per tick to an hourly JSONL manifest.
The manifest is the append-only source of truth and doubles as the outbox: a sibling task tails it and publishes to `crossing.frames.v1`, advancing a per-camera position file only after broker acks.
A Kafka outage therefore costs publish latency, never a frame; deleting a position file replays the corpus from local manifests.
Runs with `strategy: Recreate` (two pollers would double ODOT's request rate) and no CPU limit (throttling would skew `captured_at`).
Latency is ODOT's, not ours: polling is every 30 seconds, but the cameras publish on their own schedule - measured over Aug 12-18 2026 at 12th & Clinton, a new frame arrives every 3.2 minutes (median; p90 ~6, worst 11.5), and is already ~2 minutes old (median; p90 ~3) when TripCheck first serves it.
What a viewer sees therefore runs a few minutes behind the street, and a state change can take five to nine minutes to fully surface; the session gap and staleness bounds (below) are sized off these numbers.
CLIs: `blockade-capture` (run/once), `blockade-inventory` (fetch/list/resolve - regenerates `config/cameras.yaml` from the ODOT inventory), `blockade-sync` (S3 repair).

### detector (`services/detector` + `libs/blockade-core/src/blockade/detect/`, deploy/detector)

Turns frames into `ObservationRecord`s: one judgement (BLOCKED / CLEAR / UNKNOWN, confidence, reason) per crossing per tick.
UNKNOWN is a first-class honest answer; a detector must never raise and never guess.
Cameras the roster bars from judging carry `scores: false` (today 677 and 679, whose views do not include their crossing, and 682, whose verdicts proved untrustworthy): they emit zero-inference UNKNOWNs stamped `unscored/1`, so the board keeps their pictures while consensus, sessions, alerts, and analytics all ignore them.
That covers new rows; their historical judgements stay authoritative for past instants until a re-score and backfill layers `unscored/1` over them - which has to re-score the crossing's other cameras in the same pass, because that is what rebuilds its sessions.
Every record is stamped with the `detector_version` that produced it, so rows from different detectors are never silently mixed.

Detectors are interchangeable by config (`BLOCKADE_DETECTOR`); production runs `auto`, a per-camera router:

- `classifier` - per-camera MobileNetV3-small ONNX models, used wherever `references/classifier-{camera_id}.onnx` exists in S3.
  Shipping a new camera's model is an S3 upload plus a rollout restart; no code change.
- `reference` - the free fallback for untrained cameras: brightness-binned median references with per-pixel spread thresholds, per-camera calibrated, restricted to a data-derived track band.
- `vlm` - Claude Haiku reads the scene; used by the `spotcheck` label-growing tool, not in the live path.

CLIs: `blockade-detect run` (the streaming service), `scan` (batch re-score for backfills), `explain <image>` (score one frame with the production build), `band <camera>` (derive the track band from hand-labeled blocked frames), `spotcheck` (VLM label sweep).
The accuracy loop - label sources by trust, training, the exam gate, shipping, backfilling - is a workflow, not a service; it is documented in [docs/training.md](training.md).

### sessionizer (`services/sessionizer` + `libs/blockade-core/src/blockade/{stream_sessions,alerts}.py`, deploy/sessionizer)

Turns observations into blockage sessions and rising-edge alerts.
The decisions live in pure, heavily tested classes - `StreamingSessionizer` and `RisingEdgeAlerter` in the core library, coordinated by the service's Kafka-free `Processor` - and the rest of the service is their host.
`StreamingSessionizer` applies the gap rule (a session ends after fifteen quiet minutes - the bound tracks the camera's worst sampling interval, not train behavior) one observation at a time, emitting every run from its first BLOCKED frame - the record records; certification happens where the record is read and is diffed in tests against `sessions.derive_sessions`, the batch oracle that sees whole history - two independent implementations that must agree.
`RisingEdgeAlerter` fires once per blockage at its leading edge (two confirmations to fire, three clears to re-arm) into `crossing.alerts.v1`, which has no consumer yet - the notifier is the next feature.

Recovery is replay: state is a pure function of the observations log, so every boot re-reads the topic and rebuilds open sessions with their original `started_at`.
A committed consumer-group offset serves as an emission boundary only - records below it rebuild state silently (they were published in a previous life; re-publishing would resurrect rows a backfill deleted), records past it emit normally, which is exactly what arrived while the pod was down. A run that any at-or-past-boundary record contributed to is this boot's to close however old its deadline looks - the train that blocked and cleared entirely during the outage.
Offsets commit only at the head after a sweep, making "committed" mean "everything due then was announced".
Session closes fire on wall clock past the gap deadline plus a two-minute drift allowance, and only at the head of the topic, so a backlog drain can never close a session early.

### api (`services/api`, deploy/api, deploy/postgres)

One pod serving both the JSON API and the static site, plus the Postgres materializer.
Every JSON read endpoint sets `Cache-Control` with per-endpoint browser and edge lifetimes - `cache_for` in `src/api/app.py` owns the numbers and the Cloudflare eligibility caveat - except the no-history-store `/analytics` answer, deliberately uncached so an outage is never cached past its end.

- **Board** (`/api/v1/status`, `/api/v1/events` SSE, frames): `LiveState` in `blockade-core/api/state.py` is a pure reducer rebuilt on every boot by groupless Kafka tailers; readiness gates traffic until the replay passes boot-time end offsets.
  Consensus is blocked-biased (any fresh BLOCKED wins; a glare-blind camera's CLEAR cannot veto its partner's train) and anything older than fifteen minutes is stale, so a dead detector can never leave BLOCKED frozen on screen.
  Fifteen is bounded on both sides: above the worst measured camera cadence (692s overnight, ~11.5 minutes, from the Aug 12-18 measurement above) with ~3.5 minutes of margin, and no longer than the gap deadline plus drift margin a session close takes, so the board never claims a blockage the train sheet has already ended.
  The board also says whose fault stale pictures are: a third groupless tail on `crossing.frames.v1` feeds every poll outcome into the reducer, and `/status` carries a `feed` block blaming in evidence order - `capture_stale` (no poll heartbeat at all: ours), `upstream_down` (a full error streak on every camera: ODOT's server), `upstream_stale` (healthy polls but nothing new past the staleness bound: ODOT's cameras frozen) - which the board renders as an amber note (`feedNote` in `web/src/lib/feed.ts`).
  The verdict transitions on event time like every other reducer rule, except that a silent poller produces no record to announce itself, so the SSE heartbeat tick re-judges on wall clock and pushes the verdict instead of a bare keep-alive.
- **Materializer**: two grouped consumers upsert observations and sessions into Postgres, committing offsets only after the transaction - at-least-once, absorbed by deterministic keys.
- **History** (`/api/v1/timeline`, `/sessions`, `/analytics`): plain SQL in `db.py`; analytics buckets are corridor-local (America/Los_Angeles) via SQL `AT TIME ZONE`.
  The record keeps every blocked run from its first frame, carrying its evidence (`observation_count`); certification - two observations spanning five minutes, `blockade.sessions.is_certified` - is a read-side rule.
  `/sessions` applies it server-side per row (`certified`), analytics restates it in SQL (the two are pinned against each other in tests), and nothing deletes a run for being brief anymore.
- **Backfill** (`blockade-api backfill obs.jsonl`): loads a re-scored window; see the data contract below.
- **Frames** (`/api/v1/frames/...`): S3 reads behind a content-addressed disk LRU, with a path-pattern guard.
- **Web** (`web/`): static Astro build baked into the image; three board pages, one Preact island each - the board (Leaflet map with a flasher per featured crossing, SSE, time scrubber), the train sheet, and patterns - plus a static, footer-linked privacy page (`/privacy/`, the URL App Store review requires).
  The UI presents only the crossings in `FEATURED` (web/src/lib/crossings.ts) - currently 12th & Clinton alone; [camera-survey.md](camera-survey.md) records why 8th & Division was unfeatured - while every camera keeps capturing in the background - and every scoring one keeps scoring - so the record accumulates for the rest.
  `/analytics` also ships `local_tz`, the corridor's clock, which every corridor-clock rendering reads - on the patterns page, the day profile's current-hour ring and the record list's dates - the timezone is a wire contract, not a client constant.
  The board's blocked ticker carries a wait-outlook line - the median of the recorded durations the blockage has not yet outlasted, from `/analytics` (`waitOutlook` in `web/src/lib/analytics.ts`) - and the patterns page derives its longest-on-record list client-side from `/sessions`, not from a new aggregate.
  The scrubber is the one place the consensus rule above exists twice: `LiveState` only ever holds the present, so answering "what did the board show at 05:45" happens client-side over `/timeline` rows, in `web/src/lib/scrub.ts`.
  The blockage lanes under the slider are that same rule swept over the whole window (`blockedSpans` in the same file), not the session projection - the lanes must never disagree with what scrubbing to the same instant shows, so they replay the same rule over the same `/timeline` rows rather than waiting on the materialized record.
  That copy is pinned against the reducer's own scenarios in `scrub.test.ts`, so the two cannot drift silently.
  The train sheet tiers rows by that flag: certified sessions as solid signals, uncertified runs as hollow-signal sightings with their evidence in the footer - so a train the lanes show is never missing from the sheet.
  The site ships a web-app manifest and icons (`web/public/manifest.webmanifest`), so a phone can install the board standalone on its home screen.
  `web/public/offline.html` is a self-contained outage page (no stylesheet, script, font, or content image to fail alongside the origin) for Cloudflare to serve when the origin is unreachable.
  `npm run check` typechecks under Astro strict and `npm test` runs those scenarios plus the other `web/src/lib` suites - among them `crossings.test.ts`, which pins the FEATURED presentation contract, and `analytics.test.ts`, which pins the outlook line and the day profile's prose; CI runs both for web changes.

### iPhone and Apple Watch apps (`apps/ios`)

A native read-only client of the public board: a map-and-sheet SwiftUI app (glance plaque, latest camera frame, train sheet), home/lock-screen widgets, a watch app with the train sheet a swipe below the glance plus watch-face complications, and a Siri intent - answering from `/api/v1/status`, `/api/v1/sessions`, and `/api/v1/frames/...`.
Build, signing, and setup for both platforms live in [apps/ios/README.md](../apps/ios/README.md).
Installed copies pin their payloads, and a rename inside a present object fails the whole decode - blanking installed apps the site's lockstep deploy would not:

- `/status`: `generated_at`, `crossings[].{crossing_id,state,stale,since,open_session.started_at,latest_observation.{camera_id,captured_at,object_key}}`, and the `feed` verdict strings.
- `/sessions`: `sessions[].{session_id,started_at,duration_seconds,is_open,certified}`.
- `/frames/{object_key}`: keys from `latest_observation.object_key` resolving to JPEG bytes.

### Postgres (deploy/postgres)

The history store, on the 12TB disk.
Everything in it is derived and replayable from Kafka and S3; losing it is an inconvenience, not data loss.
A nightly `pg_dump` CronJob (`deploy/postgres/backup.yaml`) ships the record to `backups/postgres/` in S3 so that loss costs a restore rather than a re-score; the job is live now that the `backup` role ARN is in the `aws-roles` Secret.
Schema is `CREATE TABLE IF NOT EXISTS` on API start - at this scale a migration framework is ceremony.

## Data contracts

### Kafka topics

| Topic | Key | Producer | Consumers | Semantics |
| --- | --- | --- | --- | --- |
| `crossing.frames.v1` | camera_id | poller outbox | detector, api tailer | at-least-once; payload is the manifest line, byte for byte |
| `crossing.observations.v1` | crossing_id | detector | sessionizer, api tailer | at-least-once; 7-day retention; the dataset's live edge |
| `crossing.sessions.v1` | session_id | sessionizer | api tailer, api materializer | compacted; one row per session survives, the latest emission |
| `crossing.alerts.v1` | crossing_id | sessionizer | none yet | rising edges only; the future notifier's feed |

Keys are an ordering contract: all records for one key land on one partition, which is the only reason a consumer may assume per-camera or per-crossing order.
Producers use acks=all and idempotence; consumers commit offsets only after their output is durable.

### Postgres tables

`observations` is append-only and versioned: primary key `(camera_id, captured_at, detector_version)`, and reads resolve latest-`ingested_at`-wins per `(camera_id, captured_at)`.
That layering is where retroactive honesty lives - a better detector's backfill adds a newer layer, and every read surface (timeline, scrubber, analytics) resolves to it without rewriting history.

`sessions` is a projection of observations: primary key `(session_id, detector_version)`, upserted by the materializer, and read latest-ingest-wins per `session_id`.
A backfill deletes every session starting inside the re-scored window and inserts the fresh derivation in one transaction - a session whose boundaries changed gets a new deterministic id, so upserts alone would leave the old wrong row standing.

`session_id` is `sha256(crossing_id | started_at-in-UTC)`, assigned when a session opens and never regenerated - the idempotency key the whole replay story leans on.

### S3 layout (`pdx-trainspotter`)

- `frames/{camera_id}/{Y}/{m}/{d}/{H}/{epoch_ms}-{hash8}.jpg` - the corpus, content-addressed, kept forever; the reason history can always be re-derived at better accuracy.
- `manifests/{camera_id}/.../{H}.jsonl.gz` - hourly-rolled manifest uploads; with the poller PVC these are the backfill source.
- `backups/postgres/blockade-{timestamp}.dump` - nightly `pg_dump` of the history store (custom format); the fast restore path for Postgres.
- `references/{camera_id}.npz|.json` and `references/classifier-{camera_id}.onnx` - detector models; every detector pod syncs the prefix at boot.

Model weights never live in git; they ship through this prefix.

## Deploy and operations

ArgoCD tracks `main` and applies the service directories under `deploy/` - edit the repo, not the cluster.
Cluster-wide platform (Kafka/Strimzi, Prometheus, ArgoCD itself) lives in the homelab repo; everything specific to PDX Train lives here under `deploy/`, next to what it deploys.
Each service directory carries a base kustomization that lists its files explicitly, and [deploy/cloud/](../deploy/cloud/README.md) overlays those bases for the single cloud box; that overlay is synced by the cloud box's own ArgoCD, never the homelab's, and its README owns the deltas.
Images build per service on merge (QEMU arm64, GHCR, `:latest` + SHA tags); a new image reaches pods via rollout restart.
All code changes go through the no-mistakes gate on a feature branch; nothing lands on main directly.

Imperative, never in git: the `aws-roles` Secret (IRSA role ARNs), `odot-credentials`, `postgres-credentials`, `regcred`, and the `blockade-cameras` ConfigMap.
When the roster schema gains a field, roll images before recreating the ConfigMap; when it loses one, recreate the ConfigMap first - `Camera` is `extra="forbid"` in both directions.

Monitoring: the poller, detector, sessionizer, and api each expose Prometheus metrics on :9102 with a ServiceMonitor, and the poller's `deploy/poller/alerts.yaml` carries the poller rules that matter, including `BlockadePollerMetricsMissing` - the absent() rule that fires when the poller's own series stop arriving and every other poller rule has therefore gone blind.
The backup rules live in `deploy/postgres/backup.yaml`, built on kube-state-metrics: `BlockadeBackupFailed` (a Job failed within the last 24h, so a transient bad night self-resolves) and `BlockadeBackupMissing` (no successful dump in 26 hours while the CronJob is unsuspended).
The api serves its metrics on that separate port rather than :8000 because the public HTTPRoute forwards every path on :8000, so `/metrics` there would be internet-facing.
`deploy/monitoring/dashboard.yaml` is the app's Grafana dashboard, a `grafana_dashboard`-labeled ConfigMap the kube-prometheus-stack sidecar provisions; `tests/test_api_metrics.py` joins its panel queries against the series the api actually exports so a renamed metric can't blank a panel silently.
A deploy-manifest test asserts every ServiceMonitor actually selects a Service, because the one time it didn't, all alerting was silently dead for two days.

### Common tasks

- **Retrain a camera**: follow [docs/training.md](training.md) (label, train, exam against gold labels, `aws s3 cp` the ONNX to `references/`, rollout restart the detector, then backfill the improved history).
- **Backfill after a detector change**: `blockade-detect scan --until <now-20min>` over *every* camera on the crossing, then `blockade-api backfill obs.jsonl --dry-run`, then for real; only the re-scored crossing's history changes.
  Scanning one camera of a two-camera crossing is the trap: the sessions are derived from all its witnesses at once, so a partial scan rebuilds the window from half the evidence, and a scan of a `scores: false` camera rebuilds it from none.
  The plan reads the roster and refuses any window whose crossing has a scoring camera missing from the scan, and the load refuses a second time if a window would end up with no sessions at all; `--allow-empty-window` is the deliberate override for windows that predate a camera or whose sessions really were phantoms.
  The step-by-step runbook, including reaching Postgres, is in [services/api/README.md](../services/api/README.md).
- **Add a camera**: add it to `TARGET_CAMERAS` in `inventory.py` (and to `NON_SCORING_CAMERAS` there if its judgements must not count - otherwise the next `resolve` enfranchises it), run `blockade-inventory resolve`, recreate the ConfigMap; it scores UNKNOWN until it has a reference model (and earns its track band from its first hand-labeled blockages via `blockade-detect band`).
- **Debug one frame**: `uv run blockade-detect explain path/to/frame.jpg`.
