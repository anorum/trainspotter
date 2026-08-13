# Classifier training runbook

How a camera goes from the reference fallback to its own classifier, and how an existing classifier gets better.
This is the workflow that took `odot-678` from seven hard exam errors to zero.
It is a workstation workflow, not a service: nothing here runs in the cluster, and torch never enters an image.
The architecture this feeds is described in [docs/architecture.md](architecture.md).

## 1. Labels, by trust

Labels are not equal, and treating them as equal is how a camera regresses.
`build_manifest` in `services/detector/src/detector/dataset.py` is the only assembler in the repo, and it draws on three sources that are not equally reliable.

**Session cores** are the entire positive class.
Every frame inside a closed blockage session, shrunk by a two-minute margin at both ends so boundary uncertainty never becomes a training label, is labelled BLOCKED.
An unclosed session contributes no positives, because its real end is not known and must not be guessed.

**VLM-sweep CLEARs** are the good negatives.
`blockade-detect spotcheck` walks frames at a stride and records Haiku's judgement; the manifest takes the ones labelled CLEAR at confidence 0.8 or above.
The sweep must write to its own file rather than to gold, for two independent reasons that section 2 spells out.
The code applies no time-of-day filter, so you have to be the filter: Haiku missed a confirmed night train on these cameras, and a high-confidence night CLEAR does not mean what the same number means at noon.
Review the night CLEARs before you trust them.

**Quiet-period frames** are the cheap bulk negatives.
Any frame more than 30 minutes from every known session is labelled CLEAR, and every such frame is taken rather than sampled.
The hour mix is therefore whatever the corpus happens to hold, so eyeball the spread before training: a negative class that is mostly small hours teaches the model that darkness means clear.
Stratifying that draw by hour is the obvious improvement to `build_manifest` if it ever bites, and is deliberately out of scope for this docs change.

`data/labels/labels.jsonl` is gold.
`build_manifest` skips every frame whose key appears in it, so gold is the exam and never the textbook.
The moment gold enters training, the exam stops measuring anything.

Two things sit outside the manifest and are worth more of your time than anything in it.
Hand-saved BLOCKED frames under `data/blocks/{camera_id}/` are the scarce resource: they feed `blockade-detect band`, which derives the camera's track band from real blockages, and they are what you adjudicate gold from.
Roughly 30-40 of them, spanning night, dawn, and day, is what made `odot-678` near-perfect.
Hard negatives - dawn phantoms, gates down with no train, trains on a far siding, MAX trains - are what kill false positives, and finding them is human work.
Adjudicate them in bulk: group strict-detector flags into clusters of three or more consecutive flags within six minutes and judge each cluster at a glance from a composite contact sheet, because the neighbours carry context a single frame does not.
Adjudicated frames reach training only as gold exclusions or through the sources above, so check that the ones you care about actually landed in the manifest.

## 2. Assemble and train

Both steps are plain functions, called from a workstation script with the `train` extra installed.
There is no runtime CLI for either: torch is about 2GB of workstation-only weight that never belongs in a serving image.

**Get the corpus onto the workstation.**
`blockade-sync` only ever uploads, so pull the frames down yourself, and keep the S3 layout when you do:

```bash
aws s3 sync s3://pdx-trainspotter/frames/odot-678 var/frames/frames/odot-678
```

The doubled `frames/` is not a typo: the local cache mirrors S3 keys exactly, because `LocalFrameCache` resolves a path as `root / key` and `var/frames` is that root.
Both consumers below depend on the layout rather than on a path you pass them.
`build_manifest` rebuilds each object key from the frame's own directory and filename, and `load_examples` only accepts a frame whose path contains the camera id as a directory component - so a flat dump of JPEGs matches nothing, loads zero examples, and trains on an empty set without raising.

**Sweep for VLM negatives**, if you want them:

```bash
uv run blockade-detect spotcheck --frames-dir var/frames/frames \
  --camera odot-678 --labels data/work/sweep.jsonl
```

Pass `--labels` every time.
It defaults to `data/labels/labels.jsonl`, and sweeping into that default appends Haiku's own judgements to the file section 3 uses as the exam: the exam would stop measuring the model against humans and start measuring it against another model, silently, one sweep at a time.
Today all 33 gold records are human or human-anchored adjudications, and that is the property worth protecting.
The two files cannot be the same one anyway - `build_manifest` skips every gold key before it consults the sweep, so a sweep written into gold contributes nothing to training and the VLM negatives vanish instead.

**Assemble the manifest.**
`session_files` is JSONL of `BlockageSession` records - a dump of the compacted `crossing.sessions.v1` topic, or `sessions.derive_sessions` run over `blockade-detect scan` output; there is no command that writes it for you.

```python
from pathlib import Path

from detector.dataset import build_manifest, write_manifest

examples = build_manifest(
    camera_id="odot-678",
    crossing_id="SE_12TH_CLINTON",
    frames_dir=Path("var/frames/frames/odot-678"),
    session_files=[Path("data/work/sessions-odot-678.jsonl")],
    sweep_file=Path("data/work/sweep.jsonl"),
    gold_labels=Path("data/labels/labels.jsonl"),
)
write_manifest(examples, Path("data/work/manifest-odot-678.jsonl"))
```

A missing `sweep_file` is not an error - `build_manifest` skips the source when the file does not exist - so check the assembled manifest actually contains `vlm-sweep` examples rather than assuming it does.

Then train.
One model per camera: MobileNetV3-small, ImageNet backbone frozen, the full classifier head fine-tuned.
A fixed camera is a closed world, so a small head over generic features is enough, and freezing the backbone is what keeps a few hundred frames from overfitting.
Loss is BLOCKED-weighted, because the classes are lopsided and a missed train costs more than a spurious flag on the training set.

```python
from detector.train_classifier import train

metrics = train(
    manifest=Path("data/work/manifest-odot-678.jsonl"),
    frames_roots=[Path("var/frames")],
    out_onnx=Path("data/work/classifier-odot-678.onnx"),
    epochs=12,
)
```

The default is 6 epochs; around 12 is the working figure, so pass it.
`train` returns tp/fp/tn/fn over the 15% validation split held out of the manifest.
Read that as a smoke signal only - the split is drawn from the same weak labels the model trained on, so it cannot tell you the model got better. Section 3 is the gate.

## 3. The exam gate

Score the camera's gold labels with the new ONNX, and score them again with the detector currently in production.
Ship only if hard errors go down, where hard errors are missed blockages plus false BLOCKED.
UNKNOWN is not a hard error: a confident wrong answer is worse than an honest refusal, and a model that trades UNKNOWNs for wrong answers has gotten worse even when its accuracy number goes up.

There is no batch scorer for this, and pretending otherwise would be the fastest way to skip the gate.
`blockade-detect explain` scores a single frame and `scan` scores frames from the manifests; neither reads `data/labels/labels.jsonl`.
Scoring gold is a throwaway script: read the label file, run both models over the referenced frames, and tabulate the two error counts.
Build it however you like, but do not ship without the two numbers side by side.

Cameras with thin gold need a substitute exam.
Hold out the hand-saved positives as the test set, and add a false-positive screen over quiet frames the model has never seen.
A model that passes on held-out positives but lights up on quiet frames has not earned a rollout.

## 4. Ship

`aws s3 cp` the ONNX to the `references/` prefix as `classifier-{camera_id}.onnx`, then rollout restart the detector.
There is no code change and no image build; the detector pods sync the prefix at boot.

Verify with an observation, not with the boot log.
The pod logs the router version as `auto(classifier/mobilenetv3s-v1-c0.7|reference/...)`, which proves only that the router is active somewhere in the fleet: that string names no camera, does not change when a retrained model ships, and appears whenever any camera has a model at all.

The real proof is per camera and per model.
`detector_version` on a fresh observation for the retrained camera reads `classifier/mobilenetv3s-v1-c0.7-h<hash>`, where the hash is the first twelve hex of the ONNX file's sha256 - so a changed hash is the upload landing and being loaded.
If the classifier fails to load, that camera answers UNKNOWN with reason "no classifier trained for this camera"; it does not quietly fall back to reference differencing, because routing was decided by filename at boot.
An untrained camera keeps its bare `reference/...` version.

## 5. Backfill

A better detector should improve the past too, not just the future.
Re-score and load the improved history following the backfill task in [docs/architecture.md](architecture.md): `blockade-detect scan` over the window, then `blockade-api backfill`, dry run first.
The observations table is layered by `detector_version`, so this adds a newer layer rather than rewriting anything.

## 6. Disagreements

When you and the model disagree, verify before you overrule.
Pull the frame and look at an upscaled crop of the track band, not the thumbnail.
On `odot-678` the classifier was right at 0.97 on a dawn frame a human had misread, and the label was the thing that needed fixing.
