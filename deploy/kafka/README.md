# Kafka: project side

The broker, its storage, the Strimzi operator, and the Kafka UI are platform
infrastructure and live in the homelab repo (`homelab/kafka/`), managed by
ArgoCD.
This directory holds only what belongs to this project: its topics.

`topics.yaml` declares the `crossing.*` topics as KafkaTopic resources against
the shared `blockade` Kafka cluster in the `kafka` namespace.
If this project were retired, deleting these topics would be the only Kafka
cleanup needed.
