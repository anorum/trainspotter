# Poller deployment

## Apply

The manifests carry no role ARNs.
Each pod reads `AWS_ROLE_ARN` from the imperative `aws-roles` Secret, so the AWS account id never lands in git:

```sh
kubectl create secret generic aws-roles -n blockade \
  --from-literal=poller=<blockade-poller role arn> \
  --from-literal=detector=<blockade-detector role arn> \
  --from-literal=api=<blockade-api role arn>
```

ArgoCD syncs `deploy/` directly; there is no substitution step.

## Prerequisites, created out of band

Neither belongs in git - one is a registry credential, the other holds API keys.

```sh
# Pull credential for the private GHCR package. read:packages is sufficient;
# CI pushes with its own GITHUB_TOKEN and never needs this.
kubectl create secret docker-registry regcred -n blockade \
  --docker-server=ghcr.io --docker-username=<user> --docker-password=<PAT>

# ODOT subscription keys and the contact User-Agent. The User-Agent lives here
# rather than in the manifest so no address is committed.
kubectl create secret generic odot-credentials -n blockade \
  --from-literal=BLOCKADE_ODOT_API_KEY=... \
  --from-literal=BLOCKADE_USER_AGENT='blockade/0.1 (+<repo>; <contact>)'

# Camera roster. Reloader restarts the Deployment when this changes, so adding a
# camera is a config change rather than a rebuild.
kubectl create configmap blockade-cameras -n blockade \
  --from-file=cameras.yaml=config/cameras.yaml
```

### Roster schema changes are ordered

Adding or removing a *camera* is just a ConfigMap edit.
Changing the roster's *shape* is not, because `Camera` forbids unknown keys and Reloader restarts the pod the moment the ConfigMap changes - so a mismatched pair stops capture outright, and ODOT overwrites images within the minute.

When the roster gains a field (as `lat`/`lon` did), roll the new image first and recreate the ConfigMap second.
A new image reads an old roster fine because the new field defaults; an old image reads a new roster as a ValidationError and CrashLoopBackOffs.
The same rule governs rollback: re-apply the old roster before rolling the image back, or the old image meets a roster it cannot parse.

When a field is removed, the order reverses - recreate the ConfigMap without it first, then roll the image.

## AWS access

No stored credential. The pod runs as the `poller` ServiceAccount and mounts a
projected token with audience `sts.amazonaws.com`; boto3 exchanges it for
one-hour credentials via `AssumeRoleWithWebIdentity`.

On EKS a mutating webhook injects the env vars and volume from the
`eks.amazonaws.com/role-arn` annotation. k3s has no such webhook, so the pod spec
does it by hand - that is what the `aws-token` volume and the three `AWS_*`
variables are.

The trust policy pins `sub` to `system:serviceaccount:blockade:poller` with
`StringEquals`. A wildcard there would let any pod in the cluster assume the role.

## Verifying it captures

```sh
kubectl exec -n blockade deploy/poller -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9102/metrics').read().decode())" \
  | grep blockade_frames_total
```

Expect all six cameras, and `not_modified` counts climbing alongside `ok` - most
polls should return 304, since the cameras refresh roughly every two minutes
while the poll interval is 30s.

## Migration note

Capture previously ran on a Mac under launchd
(`deploy/local/com.blockade.capture.plist`). Both were run together until the
cluster pod was confirmed capturing all six cameras, then launchd was unloaded.
Running both permanently would double the request rate against ODOT, and a
revoked key ends the project.
