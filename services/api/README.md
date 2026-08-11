# blockade-api

The serving layer: one pod answering "is a train blocking the crossing right now".
Tails the Kafka topics into an in-memory board (the reducer in blockade-core), serves it as JSON and SSE, proxies frames from S3, and serves the Astro-built site from the same container.
Postgres-backed history and analytics arrive in later phases.
