# blockade-api

The serving layer: one pod answering "is a train blocking the crossing right now".
Tails the Kafka topics into an in-memory board (the reducer in blockade-core), serves it as JSON and SSE, proxies frames from S3, and serves the Astro-built site from the same container.
One grouped consumer per topic (`blockade-api-db-obs` and `blockade-api-db-sess`) also materializes observations and sessions into Postgres, and the `/api/v1/timeline`, `/api/v1/sessions`, and `/api/v1/analytics` endpoints answer only from that history.
Without `BLOCKADE_DATABASE_URL` those history endpoints refuse - 503, or `available: false` from `/api/v1/analytics` so the UI hides the stats surface - rather than serving a half-true history from memory.
The live board never depends on the database either way.

## Backfilling history after a detector improves

Streaming owns now, batch owns history.
When a detector gets better, its new word reaches Postgres through this loop, never through Kafka or the live sessionizer - replaying history through the live sessionizer would corrupt its keyed state.

1. Re-score the window with the new detector, wherever the frame corpus and manifests live in the poller layout (`var/frames/frames/...` and `var/manifests/{camera_id}/{YYYY-MM-DD-HH}.jsonl`; the poller PVC has them, or pull the window down from S3):

   ```
   uv run blockade-detect scan \
     --since 2026-08-09T00:00:00Z --until 2026-08-12T00:00:00Z \
     --output obs.jsonl
   ```

   Cover every camera on the crossing, not just the one whose detector changed - drop `--camera` as above, or scan each and concatenate the JSONL.
   A crossing's sessions are derived from all its witnesses at once, so a scan of one camera of two rebuilds the window from half the evidence, and a scan of a `scores: false` camera (677, 679) rebuilds it from none at all.

2. Reach Postgres (from a workstation, port-forward: `kubectl -n blockade port-forward svc/postgres 5432`).

3. Load it:

   ```
   BLOCKADE_DATABASE_URL=postgresql://blockade:...@localhost:5432/blockade \
     uv run blockade-api backfill obs.jsonl
   ```

   `--dry-run` first prints the plan - per-crossing windows, session counts, detector versions - without touching the database.

The load is one transaction and is safe to re-run.
Observations join the store as a new versioned layer and the timeline resolves latest-ingest-wins per instant, so the old detector's word stays on record but stops being the answer.
Sessions are a projection and get rebuilt: every session starting inside the re-scored window is replaced by what the new derivation found, which is how a phantom session disappears instead of surviving next to its correction.
A partial scan is refused rather than loaded, because the delete would otherwise be silent and unrecoverable from the board.
The plan checks the roster: every scoring camera on a crossing must appear in the observations before any window of that crossing is rewritten, so the 681-only scan of a crossing 682 also watches stops before it deletes what 682 saw.
A crossing the roster does not describe at all - an unknown, retired, or renamed crossing id, or one whose every camera is `scores: false` - is refused outright instead of passing the check with zero required witnesses, and `--allow-empty-window` does not waive that one: it waives coverage for a crossing the roster describes, and here there is no coverage to reason about.
Fix the roster, or the file the scan was pointed at.
The load then refuses a second time, inside the transaction, if a window would be left with no sessions at all - the shape the roster check cannot see, such as a complete re-score that now reads every frame as CLEAR where the old detector found a train.
Both of those give way to `--allow-empty-window`, for a window that predates a camera or whose sessions really were the phantom.
It applies to every window in the file, so load a legitimately-empty crossing on its own rather than disarming the check for the others.
The command refuses windows reaching within one session gap of now - that edge belongs to the streaming sessionizer.
Scan windows should extend a little beyond the period of interest on both sides, so no real session straddles the window boundary.
