# AWS setup

One bucket, three prefixes:

| Prefix | Contents |
| --- | --- |
| `frames/` | JPEGs, `frames/{camera_id}/{yyyy}/{mm}/{dd}/{HH}/{epoch_ms}.jpg` |
| `manifests/` | Hourly gzipped JSONL, the backfill corpus |
| `warehouse/` | Iceberg tables (Phase 3) |

`checkpoints/` used to hold Flink state. The sessionizer that replaced the Flink job
rebuilds its state by replaying Kafka, so nothing writes that prefix any more.

```bash
BUCKET=blockade-$(aws sts get-caller-identity --query Account --output text)
aws s3api create-bucket --bucket "$BUCKET" --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2

# Public roadway imagery is not secret, but it is also nobody else's to serve.
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration file://lifecycle.json

aws iam create-policy --policy-name blockade-s3 \
  --policy-document "$(sed "s/BUCKET_NAME/$BUCKET/g" iam-policy.json)"
```

## Notes

**Lifecycle applies to `frames/` only.** Manifests are tiny and are read during every backfill;
Iceberg metadata must stay in Standard or the maintenance jobs get slow and expensive. Transitioning
small objects is also counterproductive - Glacier classes bill a 128 KB minimum per object and a
40 KB overhead, so a 50 KB JPEG is not obviously cheaper in Glacier than in Standard-IA. The
transitions below are set at 90 days for that reason, and are worth re-checking against real object
sizes once a few months of frames exist.

**No versioning.** Frames are written once and never modified; versioning would only accumulate
delete markers during cache sweeps.

**Requests dominate the early bill,** not storage: ~518k PUTs/month is ~$2.60, against ~$0.70 of
storage in month one. Optimise the storage class only after that stops being true.
