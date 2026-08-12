# blockade-sessionizer

Turns `crossing.observations.v1` into `crossing.sessions.v1` (compacted, one row per session) and `crossing.alerts.v1` (rising edges).
The session and alert decisions live in `blockade-core` (`stream_sessions.py`, `alerts.py`), unit-tested and diffed against the batch oracle; this service is the host.

It replaced the Flink job that used to host the same two classes.
At three crossings and one observation every couple of minutes, Flink's price - an operator, a JVM, checkpoints, 2.5Gi of memory - bought a durable dict of a few numbers and one timer per crossing.

State is a pure function of the observations log, so recovery is replay: every boot re-reads the topic from the beginning (groupless, the same `TopicTailer` pattern the API uses) and rebuilds open sessions with their original `started_at`.
Session emissions are idempotent - deterministic `session_id`, compacted topic, upserted in Postgres - so replay converges instead of duplicating.

Replaying the log is not the same as republishing it, though, and the difference matters because batch owns history: loading a re-scored window deletes the sessions it supersedes, and a boot that re-announced every session it re-derived would put those back.
So a committed group offset rides alongside the tail - it decides nothing about what to read, only about what has already been published.
Records below it rebuild state in silence; records at or past it emit as usual, which is exactly what arrived while the pod was down.

A session closes on silence, so its close belongs to no record and an offset alone cannot place it.
The commit point supplies the rest: offsets move only on the empty poll at the head, after that poll's sweep and its emissions are acked, never mid-drain.
A committed offset therefore means "at some wall clock past every event below it, this service stood at the head and fired every deadline then due" - so on a boot's first sweep a deadline that fell before the newest replayed observation was announced back then and is closed silently, while a deadline past it is the session that was open when the pod died.
A life that dies mid-drain commits nothing and its successor simply redoes it, duplicates and all, rather than losing a close and leaving a row open forever.
The residual is a deadline that came due between the newest replayed observation and the commit, which can be announced twice; a backfill cannot have claimed that window, since it refuses to come within a session gap of now.

Timing discipline, replacing Flink's watermark:

- A session closes only when wall clock has passed its gap deadline plus the out-of-orderness allowance, and the consumer is at the head of the topic - so a backlog drain can never close a session while older frames are still in flight.
- Alerts are suppressed during boot replay unless the rising edge is fresh. That sits on top of the published-offset boundary and catches what it cannot: after a long outage every buried edge is unpublished, and only the recent one is still worth a page.
