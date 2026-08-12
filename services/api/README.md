# blockade-api

The serving layer: one pod answering "is a train blocking the crossing right now".
Tails the Kafka topics into an in-memory board (the reducer in blockade-core), serves it as JSON and SSE, proxies frames from S3, and serves the Astro-built site from the same container.
When `BLOCKADE_DATABASE_URL` is set, one grouped consumer per topic (`blockade-api-db-obs` and `blockade-api-db-sess`) also materializes observations and sessions into Postgres and the `/api/v1/timeline`, `/api/v1/sessions`, and `/api/v1/analytics` endpoints answer from that history; unset, the API serves only the in-memory window, `/api/v1/analytics` reports `available: false`, and the live board never depends on the database.

## Backfilling history after a detector improves

Streaming owns now, batch owns history.
When a detector gets better, its new word reaches Postgres through this loop, never through Kafka or the live Flink job - replaying history through the live job would corrupt its keyed state.

1. Re-score the window with the new detector, wherever the frame corpus and manifests live in the poller layout (`var/frames/frames/...` and `var/manifests/{camera_id}/{YYYY-MM-DD-HH}.jsonl`; the poller PVC has them, or pull the window down from S3):

   ```
   uv run blockade-detector scan --camera odot-678 \
     --since 2026-08-09T00:00:00Z --until 2026-08-12T00:00:00Z \
     --output obs-678.jsonl
   ```

2. Reach Postgres (from a workstation, port-forward: `kubectl -n blockade port-forward svc/postgres 5432`).

3. Load it:

   ```
   BLOCKADE_DATABASE_URL=postgresql://blockade:...@localhost:5432/blockade \
     uv run blockade-api backfill obs-678.jsonl
   ```

   `--dry-run` first prints the plan - per-crossing windows, session counts, detector versions - without touching the database.

The load is one transaction and is safe to re-run.
Observations join the store as a new versioned layer and the timeline resolves latest-ingest-wins per instant, so the old detector's word stays on record but stops being the answer.
Sessions are a projection and get rebuilt: every session starting inside the re-scored window is replaced by what the new derivation found, which is how a phantom session disappears instead of surviving next to its correction.
The command refuses windows reaching within one session gap of now - that edge belongs to the streaming sessionizer.
Scan windows should extend a little beyond the period of interest on both sides, so no real session straddles the window boundary.
