# The cloud overlay

Kustomize overlays that re-express the homelab manifests for the single
Hetzner box (k3s, no Strimzi, no Prometheus stack, no Traefik gateway).
The homelab manifests stay the source of truth for each service; this
directory holds only the deltas, so a fix to a deployment lands in both
worlds from one edit.

What the deltas are, and why:

- **AWS**: the box is outside the homelab's OIDC trust, so IRSA becomes a
  static-key IAM user (`blockade-cloud`, same per-prefix S3 scopes as the
  three roles combined). Overlays strip the role-ARN env and projected
  token and read `aws-credentials` instead.
- **Kafka**: single-node Redpanda in the `blockade` namespace replaces the
  Strimzi cluster; `BLOCKADE_KAFKA_BOOTSTRAP` is patched accordingly. The
  four topics are created by an idempotent `rpk` Job with the same
  retention/compaction the Strimzi CRDs declare - keep them in sync with
  deploy/kafka/topics.yaml by hand until one source generates both.
- **Storage**: `local-hdd-storage` (a disk in a specific house) becomes
  k3s's default `local-path`.
- **Ingress**: no gateway; the api Service is a NodePort on 30080 and
  cloudflared (installed on the host, not in-cluster) forwards
  pdxtrain.com -> localhost:30080. No inbound ports are ever open.
- **Excluded**: monitoring.yaml files (ServiceMonitor/PrometheusRule CRDs
  are absent on the box - external uptime monitoring covers v1) and the
  backup CronJob (runs on the homelab until cutover re-points it).

Imperative secrets to seed on the box before first sync (values live in
the password manager, never in git or chat):

    kubectl create ns blockade
    kubectl -n blockade create secret generic regcred \
      --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=...
    kubectl -n blockade create secret generic odot-credentials --from-literal=...
    kubectl -n blockade create secret generic aws-credentials \
      --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=...
    kubectl -n blockade create secret generic postgres-credentials \
      --from-literal=password=... --from-literal=url=postgresql://blockade:...@postgres:5432/blockade

ArgoCD also needs a read credential for this repo (Settings -> Repositories,
or a repo-creds secret) before the Applications in argocd/ can sync.
