# blockade-core

The shared library every Blockade service builds on.
One copy of the record schemas, the detectors, the session and alert logic, and the storage/bus helpers - several services, no drift.

What lives here and why:

- `schemas.py` - the records that cross Kafka topics; the contract between services.
- `detect/` - interchangeable detectors behind one registry (`reference`, `vlm`, the per-camera ONNX `classifier`, and the `auto` router that picks classifier-per-camera and falls back to `reference`).
- `sessions.py` - the batch sessionizer, kept as the oracle the streaming path is diffed against.
- `stream_sessions.py` - the streaming sessionizer the sessionizer service hosts; proven equivalent to the oracle.
- `alerts.py` - rising-edge alerting: fire once at the front of a train, stay quiet after.
- `storage.py`, `bus.py`, `config.py` - S3/manifests, Kafka producer/consumer/tailer, settings.
- `api/` - serving-layer logic: response models and the live-state reducer (`LiveState`) that the API service hosts.

Logic lives here, plain and unit-tested; the services stay thin shells around it.
