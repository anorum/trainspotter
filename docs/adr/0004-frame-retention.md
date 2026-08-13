# 4. Frame retention

**Date:** 2026-08-08
**Status:** Accepted

## Context

Storing every camera frame invites an obvious objection: if the goal is to know whether a train is
blocking the crossing, why keep the pictures rather than just the answer?

The answer is only obvious once the detector exists, and it does not. Two things depend on the raw
frames:

1. **Calibration.** The detector is a vehicle-count and motion threshold, and thresholds are
   meaningless until measured against real queues at 3pm, at midnight, and in rain.
   docs/history/design.md calls for 200–400 hand-labeled frames spanning those conditions. Only
   kept frames can be labeled.

2. **Re-tuning by replay.** The first thresholds will be wrong. Improving them means asking "would
   this version have caught last Tuesday's blockage?", which is a re-run over stored frames.
   Without a corpus, every threshold change restarts a multi-week collection.

The second is the strongest argument in the design, and it expires the moment frames are dropped:
ODOT overwrites each image within the minute, so an unkept frame is gone permanently and at any
price.

## Decision

**Keep every frame indefinitely.** No sampling, no expiry of the archive.

**Collect 24–48 hours before starting Phase 1**, rather than the two weeks docs/history/design.md
suggests. That
two-week target serves *prediction*, which needs months of sessions regardless; *detection* only
needs enough variety to set thresholds, and a day spanning daylight, dusk, night, and a few real
blockages provides it. Thresholds keep improving as the corpus grows.

## Consequences

- ~180 MB/day, ~5.4 GB/month, **~65 GB/year**. Cheap enough that optimising it would cost more
  engineering time than storage.
- The local cache TTL applies *only* when an object store holds a durable copy; it is a read-cache
  eviction policy, never an archive retention policy. The sweeper refuses to run when no object
  store is configured.
- Phase 1 can begin roughly 2026-08-10.

## Rejected: keep only detections

Smallest footprint, and fatal to the project's main technical claim. The detector could never be
re-tuned against history, so every change would need a fresh collection period and the replay
demonstration in Phase 2 would have nothing to replay.

## Rejected: keep session frames plus a thin sample

Preserves re-tuning on the cases that matter at a fraction of the storage, and is the right answer
if the archive ever becomes expensive. At 65 GB/year it is premature: it adds a sampling policy,
a retention job, and a class of bug where the interesting frames are the ones discarded. Revisit if
the camera set grows substantially or the archive moves somewhere billed by the gigabyte.
