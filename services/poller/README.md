# blockade-poller

The capture service: polls the ODOT/PBOT cameras on their refresh cadence, writes frames to S3 and the manifest, and publishes frame metadata to crossing.frames.v1 through the outbox.
Capture is sacred: a downstream failure must never cost a frame, because the cameras overwrite their images within the minute.
