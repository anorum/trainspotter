# Classifier training runbook

How a camera goes from the reference fallback to its own classifier, and how an existing classifier gets better.
This is the workflow that took `odot-678` from seven hard exam errors to zero.
It is a workstation workflow, not a service: nothing here runs in the cluster, and torch never enters an image.
The architecture this feeds is described in [docs/architecture.md](architecture.md).

## 1. Labels, by trust

Labels are not equal, and treating them as equal is how a camera regresses.
Four sources, in descending trust:

**Hand-saved BLOCKED frames** (`data/blocks/{camera_id}/`) are the scarce resource and the thing worth your own time.
Roughly 30-40 of them, spanning night, dawn, and day, is what made `odot-678` near-perfect.
Nothing else in the pipeline substitutes for a human having looked at a real train on this camera in bad light.

**Hard negatives** are the frames that look blocked and are not, and they are what kills false positives.
Collect dawn phantoms, gates down with no train, trains on a far siding, and MAX trains passing through the frame.
A model trained only on easy negatives will confidently call every one of these BLOCKED.

**Cluster-adjudicated positives** come from strict-detector flags, grouped into clusters of three or more consecutive flags within six minutes, then adjudicated in bulk from a composite contact sheet.
Adjudicating a cluster at a glance is both faster and more accurate than deciding frame by frame, because the neighbours carry the context that a single frame does not.

**Auto negatives** are the cheap bulk.
Take VLM-sweep CLEARs at confidence 0.8 or above, daylight only, because Haiku is night-blind on these cameras and its night CLEARs are noise.
Add quiet-period frames at least 30 minutes from any flag, sampled stratified by hour so the model does not learn "3am means clear".

`data/labels/labels.jsonl` is gold.
It is the exam, and it is never trained on.
The moment gold enters training, the exam stops measuring anything.

## 2. Train

One model per camera: MobileNetV3-small, ImageNet backbone frozen, the full classifier head fine-tuned.
A fixed camera is a closed world, so a small head over generic features is enough, and freezing the backbone is what keeps a few hundred frames from overfitting.
Loss is BLOCKED-weighted, because the classes are lopsided and a missed train costs more than a spurious flag on the training set.
Around 12 epochs is the working figure; the default in code is lower, so pass it explicitly.

Training runs through `services/detector/src/detector/train_classifier.py` with the `train` extra installed.
Torch is workstation-only, about 2GB of weight that never belongs in a serving image, which is why there is no runtime CLI for this.
The output is an ONNX file; the runtime knows nothing about torch and only ever loads that.

## 3. The exam gate

Score the camera's gold labels with the new ONNX, and score them again with the detector currently in production.
Ship only if hard errors go down, where hard errors are missed blockages plus false BLOCKED.
UNKNOWN is not a hard error: a confident wrong answer is worse than an honest refusal, and a model that trades UNKNOWNs for wrong answers has gotten worse even when its accuracy number goes up.

Cameras with thin gold need a substitute exam.
Hold out the hand-saved positives as the test set, and add a false-positive screen over quiet frames the model has never seen.
A model that passes on held-out positives but lights up on quiet frames has not earned a rollout.

## 4. Ship

`aws s3 cp` the ONNX to the `references/` prefix as `classifier-{camera_id}.onnx`, then rollout restart the detector.
There is no code change and no image build; the detector pods sync the prefix at boot.

Verify two things before walking away.
The pod logs should report the router version as `auto(classifier/...|reference/...)`, which is the only proof the classifier actually loaded for this camera rather than silently falling through to reference differencing.
Then check that a fresh observation carries the new classifier version in `detector_version`.

## 5. Backfill

A better detector should improve the past too, not just the future.
Re-score and load the improved history following the backfill task in [docs/architecture.md](architecture.md): `blockade-detect scan` over the window, then `blockade-api backfill`, dry run first.
The observations table is layered by `detector_version`, so this adds a newer layer rather than rewriting anything.

## 6. Disagreements

When you and the model disagree, verify before you overrule.
Pull the frame and look at an upscaled crop of the track band, not the thumbnail.
On `odot-678` the classifier was right at 0.97 on a dawn frame a human had misread, and the label was the thing that needed fixing.
