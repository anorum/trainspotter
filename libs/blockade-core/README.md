# blockade-core

The shared library every Blockade service builds on.
One copy of the record schemas, the detectors, the session and alert logic, and the storage/bus helpers - several services, no drift.

What lives here and why:

- `schemas.py` - the records that cross Kafka topics; the contract between services.
- `detect/` - interchangeable detectors behind one registry (`reference` today; `yolo` and `vlm` behind config).
- `sessions.py` - the batch sessionizer, kept as the oracle the streaming path is diffed against.
- `stream_sessions.py` - the streaming sessionizer the Flink job hosts; proven equivalent to the oracle.
- `alerts.py` - rising-edge alerting: fire once at the front of a train, stay quiet after.
- `storage.py`, `bus.py`, `config.py` - S3/manifests, Kafka producer/consumer, settings.

Logic lives here, plain and unit-tested; services and the Flink job stay thin shells around it.
