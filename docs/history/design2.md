> **Historical record.** Despite the name, this is the EARLIER of the two design documents - the first hand-off draft.
> What actually got built diverged substantially: the fine-tuned YOLO detector below was never built (per-camera MobileNetV3-small classifiers with a reference-differencing fallback instead), Iceberg tables became Postgres, `raw-detections` / `crossing.detections.v1` became `crossing.observations.v1`, and the Flink sessionizer became a plain Kafka consumer.
> The current truth is [docs/architecture.md](../architecture.md).

Grade Crossing Blockage Detector — Design Doc
Author: Alex Norum Status: Draft for implementation (hand-off to Claude Code) Guiding principle: Simple beats complex. Build only what we need. The detector stays dumb; smarts live in cheap layers around it.


1. Goals
Early detection — near-real-time alert that a freight train is blocking an SE Portland grade crossing, so the morning commute can be re-routed. Latency matters; a false alarm is a minor annoyance.
Historical analytics — a clean, consistent record of when crossings were blocked and for how long. Accuracy matters more than speed; it can be reprocessed after the fact.

Both goals are served by one pipeline and one event stream. We do not build two detectors.


2. Non-goals
No custom GPU serving in the homelab. Inference runs CPU-only on the k3s cluster.
No perfect single-frame classification. We lean on temporal persistence and fixed-camera geometry instead of chasing model accuracy in the dark.
No streaming stack for the detection itself — a single image every 1–2 minutes is trivially small data.


3. High-level architecture
ODOT camera stills (already scraped)

        |

   [Detector]  YOLO on cropped region-of-interest  -> raw "blocked/not-blocked" + confidence

        |

   Kafka topic: raw-detections   (immutable truth of what the detector saw)

        |

        +----> [Alert branch]     rising-edge detection -> commute alert (fast, low latency)

        |

        +----> [Analytics branch] windowing + watermarks + geometric check -> settled verdict

                                                                                    |

                                                                            Iceberg (upsert by timestamp)

One detection → one event → two consumers. Kafka retention lets us replay history through the analytics branch whenever the smoothing logic changes.


4. Detector
Model: Fine-tuned YOLO (YOLOv8 or YOLO11) nano/small from Ultralytics. pip install, pretrained weights download automatically.
Why fine-tune, not open-vocab: For production-grade accuracy on grainy day/night fixed-camera footage, open-vocab (YOLO-World / Grounding DINO) is good for zero-shot "good enough" but won't hit the last few nines. Domain match — same viewpoints, lighting cycles, weather — is what buys accuracy.
Training: Rent a cloud GPU for a few hours, train, download weights. Serve forever on CPU. Training is the one expensive burst; inference is cheap and permanent.
Region of interest: Crop to the rectangle where the tracks cross the road before running the detector. Faster inference, fewer false positives.
Geometric sanity check: A detection only counts if it lands in the known track region. Fixed cameras make this a free, powerful false-positive killer.
Labeling is the real work: A few thousand frames spanning day/night/rain/fog and empty/partial/full blockage. The careful offline analytics pipeline (below) is what generates and verifies these labels.


5. Event schema (raw-detections)
Each frame emits one immutable event:

timestamp
crossing_id
blocked (bool, raw single-frame verdict)
confidence (float)

The raw layer is never edited. Interpretation lives in the verdict layer.


6. Alert branch (fast path)
Reads raw-detections directly.
Rising-edge detection: alert on the transition from not-blocked → blocked, not on the blocked state. Keyed state per crossing stores the last alerted state.
Incoming "blocked" + previous state already blocked → swallow, do nothing.
Incoming "blocked" + previous state not blocked → fire alert, set state to blocked.
Asymmetric reset (slow to clear): require several consecutive "not blocked" frames (or a couple minutes) before flipping state back to open. Prevents a flicker between railcars from re-arming and double-alerting.
Net behavior: one alert at the front of the train, silence while it passes, clean reset once genuinely gone so the next train alerts fresh.


7. Analytics branch (patient path)
Reads the same raw-detections stream.
Windowing + watermarks: buffer a few minutes of frames. The watermark ("I don't expect frames older than this") lets the window wait, giving backward-looking correction.
Two-layer model:
Raw layer — immutable, what the detector saw.
Verdict layer — best current interpretation, revisable. Looks both backward and forward around each frame.
Retroactive correction example: frame 1 = "not blocked" @ 60%, frame 2 = "blocked" @ 95%. The window reinterprets frame 1 as the train's leading edge and emits a corrected verdict for frame 1's timestamp.
Sink: upsert/overwrite the verdict row in Iceberg by timestamp. Corrected verdict supersedes the earlier one.
Also produces the duration signal: blocked for N consecutive minutes = a genuinely stuck crossing, the thing worth reporting.


8. Why one pipeline, not two
The "two things" split was about accuracy tolerance, not two detectors. Folded into one stream: the alert branch emits a quick provisional call immediately; the analytics branch emits a settled, higher-confidence version once more frames arrive. Same data, early guess and settled answer.

The one genuine reason to split would be reliability (alert must fire even if the streaming stack is down). Given this is a homelab hobby project, not worth it — unified pipeline wins.


9. Deployment
Inference on k3s nodes (swagman-1, swagman-2), CPU-only. A still every 1–2 minutes leaves tons of headroom.
Scraper already exists and is capturing images. Phase 0 remains urgent because ODOT does not archive images.
Training happens off-cluster on a rented GPU.


10. Build order
Wire the existing scraper output into the detector + raw-detections Kafka topic.
Alert branch (rising-edge + asymmetric reset). Simplest useful thing; delivers commute value immediately.
Analytics branch (windowing, watermarks, geometric check) → Iceberg.
Use the analytics output + hand-verification to build the labeled training set, then fine-tune the detector and redeploy.
