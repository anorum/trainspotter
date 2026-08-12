# Sessionizer deployment

## Cutting over from the Flink pipeline

Deleting `deploy/pipeline/` removes the manifests from git, not the objects from the cluster.
Unless ArgoCD is configured to prune, the `blockade-pipeline` FlinkDeployment keeps running, and then two writers publish to `crossing.sessions.v1` and `crossing.alerts.v1`.
Sessions would survive that - the ids are deterministic and every consumer upserts - but alerts would not: the same train would page twice.

So the Flink job goes away first, and the sessionizer starts after.
These steps are imperative and run once, in this order:

```sh
# 1. Stop the old writer. Deleting the FlinkDeployment takes the JobManager,
#    the TaskManager, and the rest Service with it.
kubectl delete flinkdeployment blockade-pipeline -n blockade
kubectl delete httproute flink-ui -n blockade   # the Flink web UI route

# 2. Confirm nothing is left publishing before the new Deployment syncs.
kubectl get pods -n blockade -l app=flink

# 3. Retire the operator. Nothing else in this cluster runs Flink jobs.
helm uninstall flink-kubernetes-operator -n flink
kubectl delete namespace flink
kubectl delete crd flinkdeployments.flink.apache.org flinksessionjobs.flink.apache.org

# 4. Drop the role ARN the Flink pods read for checkpoint writes, and add the
#    api key in its place (see deploy/poller/README.md for the full Secret).
kubectl delete secret aws-roles -n blockade
kubectl create secret generic aws-roles -n blockade \
  --from-literal=poller=<blockade-poller role arn> \
  --from-literal=detector=<blockade-detector role arn> \
  --from-literal=api=<blockade-api role arn>
```

Then in AWS, retire the IRSA role the operator's `flink` ServiceAccount assumed.
The sessionizer needs no AWS access at all - it reads and writes Kafka only - so nothing replaces it:

```sh
aws iam list-attached-role-policies --role-name blockade-flink   # note the ARNs
aws iam detach-role-policy --role-name blockade-flink --policy-arn <arn>
aws iam delete-role --role-name blockade-flink
```

The `s3://.../flink/checkpoints` and `.../flink/savepoints` prefixes are now dead data and can be deleted whenever convenient; see `aws/README.md`.

## Verifying it sessionizes

```sh
kubectl exec -n blockade deploy/sessionizer -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9102/metrics').read().decode())" \
  | grep blockade_sessionizer
```

`blockade_sessionizer_caught_up` reaches 1 once the boot replay has passed the head of `crossing.observations.v1`;
until then the pod is deliberately quiet about alerts, and `blockade_sessionizer_open_sessions` is still being rebuilt.
