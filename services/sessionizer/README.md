# blockade-sessionizer

Turns `crossing.observations.v1` into `crossing.sessions.v1` (compacted, one row per session) and `crossing.alerts.v1` (rising edges).
The session and alert decisions live in `blockade-core` (`stream_sessions.py`, `alerts.py`), unit-tested and diffed against the batch oracle; this service is the host.

It replaced the Flink job that used to host the same two classes.
At three crossings and one observation every couple of minutes, Flink's price - an operator, a JVM, checkpoints, 2.5Gi of memory - bought a durable dict of a few numbers and one timer per crossing.

State is a pure function of the observations log, so recovery is replay: every boot re-reads the topic from the beginning (groupless, the same `TopicTailer` pattern the API uses) and rebuilds open sessions with their original `started_at`.
Session emissions are idempotent - deterministic `session_id`, compacted topic, upserted in Postgres - so replay converges instead of duplicating.

Timing discipline, replacing Flink's watermark:

- A session closes only when wall clock has passed its gap deadline plus the out-of-orderness allowance, and the consumer is at the head of the topic - so a backlog drain can never close a session while older frames are still in flight.
- Alerts are suppressed during boot replay unless the rising edge is fresh, so a restart never re-announces history but still announces a train that arrived during the outage.
