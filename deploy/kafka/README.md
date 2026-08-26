# Kafka: project side

The broker, its storage, the Strimzi operator, and the Kafka UI are platform
infrastructure and live in the homelab repo (`homelab/kafka/`), managed by
ArgoCD.
This directory holds only what belongs to this project: its topics.

`topics.yaml` declares the `crossing.*` topics as KafkaTopic resources against
the shared `blockade` Kafka cluster in the `kafka` namespace.
The cloud overlay mirrors these topic configs in the `rpk` bootstrap Job in
`deploy/cloud/redpanda/redpanda.yaml`; until one source generates both, a
change here must be copied there by hand.
If this project were retired, deleting these topics would be the only Kafka
cleanup needed.
