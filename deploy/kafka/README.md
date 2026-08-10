# Kafka

Single broker, KRaft mode, sized for two 8GB Raspberry Pis.

## Storage: the HDD, deliberately

Both PVs live on `/mnt/hd` (12TB, swagman-2) via `local-hdd-storage`, not the
default `local-path`.

`local-path` provisions under `/var/lib/rancher/k3s/storage`, which is the Pi's
boot media. Two problems with putting this project there:

- **Write endurance.** The poller writes continuously and Kafka rewrites log
  segments constantly. Flash boot media is the wrong place for either.
- **Reclaim policy.** `local-path` is `Delete`, so removing a PVC destroys the
  data. Wrong for the poller's manifest, which is the outbox recording what has
  been published.

`local-hdd-storage` has **no provisioner**, so PVs are declared by hand and the
directory must already exist on the node.

## Prepare the directory

```sh
kubectl apply -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata: {name: hdd-prepare, namespace: blockade}
spec:
  ttlSecondsAfterFinished: 120
  template:
    spec:
      restartPolicy: Never
      nodeName: swagman-2
      containers:
        - name: prepare
          image: busybox:1.36
          command: ["sh","-c","mkdir -p /mnt/hd/blockade/kafka-0 && chmod 0775 /mnt/hd/blockade/kafka-0"]
          volumeMounts: [{name: hdd, mountPath: /mnt/hd}]
          securityContext: {runAsUser: 0}
      volumes:
        - {name: hdd, hostPath: {path: /mnt/hd, type: Directory}}
YAML
```

## Install

```sh
kubectl create namespace kafka
kubectl apply -f 'https://strimzi.io/install/latest?namespace=kafka' -n kafka
kubectl apply -f deploy/kafka/hdd-volume.yaml
kubectl apply -f deploy/kafka/kafka.yaml
kubectl apply -f deploy/kafka/topics.yaml
```

## Node pinning

The HDD is physically in swagman-2, so anything bound to it is pinned there.
Acceptable for single-replica services, and it is a reason frames also go to S3:
no consumer depends on that node being up to read history.

## One broker

Nothing is replicated. This is a homelab tradeoff, not a recommendation -
production would be three brokers with RF=3. It is acceptable only because Kafka
is not the system of record: every record here is derivable again from the frames
in S3, and the poller's manifest outbox can republish anything lost.
