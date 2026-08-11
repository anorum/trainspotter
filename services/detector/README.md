# blockade-detector

The scoring service: consumes crossing.frames.v1, fetches frame bytes from S3, classifies each frame with the configured detector, and publishes crossing.observations.v1.
Stateless and replayable: its position lives in the Kafka consumer group and reference models re-sync from S3 at startup.
