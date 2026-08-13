# AWS setup

One bucket. Its prefixes and key formats are documented in
[docs/architecture.md](../docs/architecture.md) under "S3 layout"; this file covers the account
setup only.

`flink/` (the `checkpoints/` and `savepoints/` prefixes under it) used to hold Flink state.
The sessionizer that replaced the Flink job rebuilds its state by replaying Kafka, so nothing
writes there any more and the prefix can be deleted whenever convenient.

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

**Lifecycle applies to `frames/` only.** Manifests are tiny and are read during every backfill.
Transitioning small objects is counterproductive anyway - Glacier classes bill a 128 KB minimum per
object, which is why `lifecycle.json` filters on `ObjectSizeGreaterThan` and its transitions
effectively never fire at the measured ~21 KB per frame. See
[ADR 0003](../docs/adr/0003-storage-and-deployment-target.md) for the measurements and the cost
reasoning behind that.

**No versioning.** Frames are written once and never modified; versioning would only accumulate
delete markers during cache sweeps.

**Requests dominate the early bill,** not storage: ~518k PUTs/month is ~$2.60, against ~$0.70 of
storage in month one. Optimise the storage class only after that stops being true.
