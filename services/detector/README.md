# blockade-detector

The scoring service: consumes crossing.frames.v1, fetches frame bytes from S3, classifies each frame with the configured detector, and publishes crossing.observations.v1.
Stateless and replayable: its position lives in the Kafka consumer group and reference models re-sync from S3 at startup.

The `blockade-detect` CLI carries the service and the operator tools:

- `run` - the streaming service: Kafka frames in, observations out.
- `scan` - score manifest frames on disk; the replay/backfill route.
- `explain <image>` - score one frame with the deployed detector build and print the judgement.
- `band <camera> --blocked-dir ...` - derive a camera's track band from hand-labeled blocked frames.
- `spotcheck` - grow the label set with VLM spot-checks (needs `ANTHROPIC_API_KEY`).
