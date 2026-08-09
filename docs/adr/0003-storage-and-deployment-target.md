# 3. Storage and deployment target

**Date:** 2026-08-08
**Status:** Accepted

## Context

DESIGN.md assumed a homelab deployment with MinIO already present. In fact the k3s cluster exists
but MinIO and Postgres do not, and running the system on AWS is acceptable if it is cheap.

That splits into two decisions that interact: where compute runs, and where objects live.

The workload is small and predictable. Six cameras at one poll per 30s is ~17.3k frames/day at
40–80 KB each: **0.7–1.2 GB/day, ~30 GB/month**. Frames are written once and read many times,
because every detector re-tune reprocesses the corpus.

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
| Storage (Standard, then lifecycled) | ~$0.70 | ~$8 |
| PUT requests (~518k/mo) | ~$2.60 | ~$2.60 |
| GET + egress (backfill only) | ~$0 | ~$0 |

Roughly **$5–12/month in year one**. Requests dominate early, which is counterintuitive and worth
knowing before optimising storage class. Lifecycle rules move objects to Standard-IA at 30 days
and Glacier Instant Retrieval at 90.

### Rejected: MinIO in k3s

Cheaper in dollars, more expensive in operations, and leaves the Iceberg-on-S3 path unexercised.
Kept as a supported configuration via `endpoint_url` rather than as the default.

### Rejected: everything on AWS

Correct if the homelab becomes unreliable, and the design deliberately keeps it a deployment
change rather than a rewrite: S3 is already S3, Postgres becomes RDS, Strimzi becomes MSK, the
Flink Operator moves to EKS. The only home-specific pieces are the local cache PVC and the MQTT
sink to Home Assistant. Not worth paying for today.
