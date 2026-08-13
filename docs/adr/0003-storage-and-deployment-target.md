# 3. Storage and deployment target

**Date:** 2026-08-08
**Status:** Accepted, implemented 2026-08-09

> **Implemented 2026-08-09.** Capture runs on the k3s cluster with IRSA and writes every frame to
> `pdx-trainspotter`; `blockade-sync` backfilled everything captured during the brief local-only
> window, so the corpus is continuous. The measurements below informed the decision and stand.
>
> One consequence had to be handled during the local-only window: the local cache's 7-day TTL
> assumes S3 holds the archive. With no second copy it would have started deleting the corpus, so
> the sweeper now refuses to run when no object store is configured.
>
> The Consequences below still argue from the Iceberg-and-Flink stack that was never built - what
> shipped is Postgres for state and a plain Kafka consumer for sessionizing, and the `demo/` stack
> named under Decision does not exist. The storage choice and its cost reasoning stand on their own;
> read the Iceberg and Flink justifications as the reasoning of the day, not as current shape.

## Context

docs/history/design.md assumed a homelab deployment with MinIO already present. In fact the k3s
cluster exists but MinIO and Postgres do not, and running the system on AWS is acceptable if it is
cheap.

That splits into two decisions that interact: where compute runs, and where objects live.

The workload is small and predictable. Frames are written once and read many times, because every
detector re-tune reprocesses the corpus.

**Measured 2026-08-08, once capture was live** (the original estimate here was ~6× too high):

- Frames are **328×334, ~21 KB** - not the 40–80 KB assumed.
- Cameras refresh roughly **every 60s**, not every 30s. With conditional GETs, ~55% of polls return
  304 and transfer nothing.
- So ~8.6k stored frames/day, **~180 MB/day, ~5.4 GB/month**.

Polling stays at 30s despite the 60s refresh. A 304 is nearly free, and polling at half the refresh
interval bounds how stale `captured_at` can be - which is the quantity that determines detection
latency.

## Decision

**Compute in k3s. Objects in AWS S3. Every frame written to S3 *and* a local PVC cache.**

The S3 client takes a configurable `endpoint_url`, so MinIO and localstack exercise the same code
path in tests and in the `demo/` stack.

## Consequences

### Why S3 rather than MinIO

Iceberg's S3FileIO, Flink's checkpoint store, and the maintenance procedures are all best-supported
against real S3. Standing up MinIO would mean operating a stateful storage service to save a few
dollars a month, and would leave the AWS migration path untested. S3 also makes the corpus
durable independently of the homelab, which matters for a dataset that cannot be recaptured.

### Why compute stays at home

The cluster is already running and idle. Moving compute to AWS adds cost with no capability gain
at this volume.

### The egress trap, and why frames are written twice

Compute at home plus storage in AWS means reading frames back from S3 is billed egress at
$0.09/GB. The detector re-reads the entire corpus every time thresholds change, so this is a
recurring cost that grows with the archive rather than a one-off.

Writing each frame to a local PVC cache as well as S3 removes it: the detector reads locally, and
S3 is touched only for backfill and replay from elsewhere. The cache is keyed identically to S3,
so a reader tries local first and falls back to the bucket with the same key.

### Cost

| Item | Month 1 | Month 12 |
| --- | --- | --- |
| Storage (~5.4 GB/mo accumulating) | ~$0.12 | ~$1.50 |
| PUT requests (~259k/mo) | ~$1.30 | ~$1.30 |
| GET + egress (backfill only) | ~$0 | ~$0 |

Roughly **$1.50–3/month in year one** - cheap enough that storage cost is not a design constraint.

Requests, not bytes, dominate the bill, which is counterintuitive and worth knowing before
optimising storage class. It also means the *conditional GET is the cost control*: it removes a
PUT, not just a download.

Lifecycle rules move objects to Standard-IA at 30 days and Glacier Instant Retrieval at 90 - but
at 21 KB per frame these fall under the 128 KB minimum billable size, so the transitions are
configured with `ObjectSizeGreaterThan` and will effectively **never fire** for individual frames.
That is deliberate: transitioning them would cost more than it saves. Revisit only if frames are
later rolled up into larger archive objects.

### Rejected: MinIO in k3s

Cheaper in dollars, more expensive in operations, and leaves the Iceberg-on-S3 path unexercised.
Kept as a supported configuration via `endpoint_url` rather than as the default.

### Rejected: everything on AWS

Correct if the homelab becomes unreliable, and the design deliberately keeps it a deployment
change rather than a rewrite: S3 is already S3, Postgres becomes RDS, Strimzi becomes MSK, the
Flink Operator moves to EKS. The only home-specific pieces are the local cache PVC and the MQTT
sink to Home Assistant. Not worth paying for today.
