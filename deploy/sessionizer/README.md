# Sessionizer deployment

A plain Deployment: no S3, no IRSA, readiness gated on the boot replay
reaching the head of the observations topic.
The one-shot Flink cutover this service replaced is recorded in
[docs/history/flink-cutover.md](../../docs/history/flink-cutover.md).

## Verifying it sessionizes

```sh
kubectl exec -n blockade deploy/sessionizer -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9102/metrics').read().decode())" \
  | grep blockade_sessionizer
```

`blockade_sessionizer_caught_up` reaches 1 once the boot replay has passed the head of `crossing.observations.v1`;
until then the pod is deliberately quiet about alerts, and `blockade_sessionizer_open_sessions` is still being rebuilt.
