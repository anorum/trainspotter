# blockade-api

The serving layer: one pod answering "is a train blocking the crossing right now".
Tails the Kafka topics into an in-memory board (the reducer in blockade-core), serves it as JSON and SSE, proxies frames from S3, and serves the Astro-built site from the same container.
When `BLOCKADE_DATABASE_URL` is set, one grouped consumer per topic (`blockade-api-db-obs` and `blockade-api-db-sess`) also materializes observations and sessions into Postgres and the `/api/v1/timeline` and `/api/v1/sessions` endpoints answer from that history; unset, the API serves only the in-memory window and the live board never depends on the database.
