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

Sessions are per crossing while training is per camera, and that gap is the sharpest label trap in this document.
`build_manifest` selects its windows by `crossing_id`, then labels BLOCKED every frame of whichever camera you pointed it at that falls inside a core.
Sessions open on a blocked-biased consensus across both of a crossing's cameras, so the partner camera inherits positives its own view never justified: `odot-679` favors Division over the Clinton crossing it is paired with, and `odot-682` cannot resolve the tracks at night.
Core positives are therefore trustworthy only for the camera whose view actually drove the session - `odot-678` at Clinton, `odot-681` at Division.
For the weak partner, spot-check the core positives before training; a model taught that a clear-looking frame is BLOCKED is precisely the false-positive generator this section exists to prevent.
The remedy that works for a camera with no hand-saved frames of its own - which is `odot-679` and `odot-682` today - is to build `session_files` from that camera's own observations: `blockade-detect scan --camera odot-679` then `derive_sessions` over the result, so the windows come only from what that view actually produced.
`scan` does not read the corpus section 2 assembles: it takes its frame list from the local manifests and reads image bytes from the local cache, and both want the poller's own layout rather than a convenient one.
The easy way is to run it on the capture workstation, where the poller already writes both and `--local-only` keeps the sweeper off.

Reproducing that layout by hand takes two steps, and both are easy to get subtly wrong.
Frames resolve as `local_cache_dir / object_key`, and the key already begins with `frames/`, so the corpus has to land at `var/frames/frames/{camera_id}/{Y}/{m}/{d}/{H}/` - one more `frames/` than the section 2 sync uses:

```bash
cam=odot-679
aws s3 sync "s3://pdx-trainspotter/frames/$cam" "var/frames/frames/$cam"
```

This is the one place the runbook puts a corpus inside `var/frames`, and it does so only because `score_frames` reads nowhere else; section 2 tells you to avoid that directory for exactly the reason it matters here.
`var/frames` is the poller's TTL-swept cache, and a synced corpus carries its S3 timestamps as mtimes, so on a box where capture is running against S3 the sweeper can delete these frames within the hour.
Do this on a machine that is not capturing, or on the `--local-only` workstation where the sweeper is off, and treat a corpus that shrinks between runs as swept rather than mispathed.

Manifests are matched by `--camera` on the parent directory name and globbed as `*.jsonl`, while S3 keeps them gzipped and nested as `manifests/{camera}/{Y}/{m}/{d}/{H}.jsonl.gz`.
Syncing that nesting verbatim leaves the parent as the day and matches nothing; flattening it naively collides, because every day contributes its own `13.jsonl`.
The layout the poller writes, and the one `--camera` expects, is flat files directly under the camera directory:

```bash
aws s3 sync "s3://pdx-trainspotter/manifests/$cam" "var/scratch/$cam"
mkdir -p "var/manifests/$cam"
find "var/scratch/$cam" -name '*.jsonl.gz' | while read -r f; do
  hour=$(echo "$f" | sed -E 's|.*/([0-9]{4})/([0-9]{2})/([0-9]{2})/([0-9]{2})\.jsonl\.gz|\1-\2-\3-\4|')
  gunzip -c "$f" > "var/manifests/$cam/$hour.jsonl"
done
```

Frames it cannot read are skipped without complaint, so zero observations means one of these two paths is wrong rather than that nothing happened.
Where hand-saved blocks do exist, section 2 folds them in as explicit BLOCKED rows.

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

Two things sit outside what `build_manifest` assembles and are worth more of your time than anything in it.
Hand-saved BLOCKED frames under `data/blocks/{camera_id}/` are the scarce resource: they feed `blockade-detect band`, which derives the camera's track band from real blockages, they are what you adjudicate gold from, and section 2 folds them into training as their own label source.
Roughly 30-40 of them, spanning night, dawn, and day, is what made `odot-678` near-perfect.
Hard negatives - dawn phantoms, gates down with no train, trains on a far siding, MAX trains - are what kill false positives, and finding them is human work.
Adjudicate them in bulk: group strict-detector flags into clusters of three or more consecutive flags within six minutes and judge each cluster at a glance from a composite contact sheet, because the neighbours carry context a single frame does not.
Beyond hand-saved blocks, adjudicated frames reach training only as gold exclusions or through the sources above, so check that the ones you care about actually landed in the manifest.

## 2. Assemble and train

Both steps are plain functions, called from a workstation script with the `train` extra installed.
There is no runtime CLI for either: torch is about 2GB of workstation-only weight that never belongs in a serving image.

**Get the corpus onto the workstation.**
`blockade-sync` only ever uploads, so pull the frames down yourself, into a directory named for the camera:

```bash
aws s3 sync s3://pdx-trainspotter/frames/odot-678 var/training/corpus/odot-678
```

Do not sync into `var/frames`: that is `local_cache_dir`, the poller's TTL-swept read cache, and its hourly sweep deletes any JPEG whose mtime is past the TTL.
`aws s3 sync` preserves each object's S3 timestamp as the local mtime, so a historical corpus is eligible for deletion the moment it lands, on any workstation whose capture is talking to S3 rather than running `--local-only`.
The failure is silent - `load_examples` skips manifest rows whose frame it cannot find - so training would just proceed on a quietly shrinking set.

`build_manifest` does not care where the frames live: it mints each object key canonically, from the `camera_id` argument you pass plus the epoch-ms timestamp in the filename, and never reads the frame's directory at all.
That is worth knowing for the trap it creates - point `frames_dir` at a directory holding another camera's frames and every key is minted under the camera you named rather than the one the pixels came from, silently.
`load_examples` is the consumer that does depend on the layout, because it only accepts a frame whose path contains the camera id as a directory component - so a flat dump of JPEGs matches nothing, loads zero examples, and trains on an empty set without raising.

**Sweep for VLM negatives**, if you want them:

```bash
mkdir -p var/training
uv run blockade-detect spotcheck --frames-dir var/training/corpus \
  --camera odot-678 --labels var/training/sweep.jsonl
```

Create the directory first, as above.
`append_labels` opens the label file in append mode without creating parents, and it does that only after the sweep has already put the camera's frames through Haiku - so a missing directory costs the whole API spend and then writes nothing.
`var/` is gitignored, which is also why the scratch files and the exported model below live there: weights never belong on a tracked path.

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
    frames_dir=Path("var/training/corpus/odot-678"),
    session_files=[Path("var/training/sessions-odot-678.jsonl")],
    sweep_file=Path("var/training/sweep.jsonl"),
    gold_labels=Path("data/labels/labels.jsonl"),
)
write_manifest(examples, Path("var/training/manifest-odot-678.jsonl"))
```

A missing `sweep_file` is not an error - `build_manifest` skips the source when the file does not exist - so check the assembled manifest actually contains `vlm-sweep` examples rather than assuming it does.

**Fold in the hand-saved blocks**, where the camera has them.
`build_manifest` does not read `data/blocks/`, but the manifest is plain `Example` JSONL, so appending them is a few lines - and this is how the production `odot-678` model was trained.
This step presumes hand-saved frames already exist for that camera: `data/blocks/` holds `odot-676`, `odot-678`, and `odot-681` today, and for the others the session-file remedy in section 1 is the one that applies.

Choose the exam before you append, because here the gold guarantee is yours to keep rather than the code's.
`build_manifest` enforces gold-out-of-training by object key, and these rows carry `blocks/...` keys that check never sees, so nothing stops you training on the very frames you plan to be graded against.
Hold out every block you have already adjudicated into `data/labels/labels.jsonl`.
Nothing links the two files mechanically - gold keys are `frames/{camera}/{Y}/{m}/{d}/{H}/{epoch_ms}-{hash8}.jpg` while blocks are hand-named PNGs carrying no timestamp - so the match is visual, against the `captured_at` of the gold record you adjudicated from.
On a thin-gold camera, choose the held-out exam positives first and keep them out of the append: `odot-676` has thirteen blocks and no BLOCKED gold at all, so folding in all thirteen would leave section 3 with nothing honest to grade.

```python
import json
from pathlib import Path

camera = "odot-678"
held_out = {"image copy 2.png", "image copy 3.png"}  # this camera's exam; never trained on

with open(f"var/training/manifest-{camera}.jsonl", "a") as fh:
    for frame in sorted(Path(f"data/blocks/{camera}").iterdir()):
        if frame.suffix.lower() not in {".jpg", ".jpeg", ".png"} or frame.name in held_out:
            continue
        fh.write(json.dumps({
            "object_key": f"blocks/{camera}/{frame.name}",
            "camera_id": camera,
            "label": "BLOCKED",
            "source": "hand-blocks",
            "split": "train",
        }) + "\n")
```

Then pass `data/blocks` as a second entry in `frames_roots` below.
`load_examples` resolves each row by basename and accepts the hit whose path contains the camera id, which `data/blocks/{camera_id}/` satisfies - so the rows above find their pixels without the frames being moved or renamed.
A `dataset.py` helper that folded blocks in automatically, taking the held-out set as an argument so the exclusion cannot be forgotten, would be a worthwhile small addition; today it is glue you write once.

Then train.
One model per camera: MobileNetV3-small, ImageNet backbone frozen, the full classifier head fine-tuned.
A fixed camera is a closed world, so a small head over generic features is enough, and freezing the backbone is what keeps a few hundred frames from overfitting.
Loss is BLOCKED-weighted, because the classes are lopsided and a missed train costs more than a spurious flag on the training set.

```python
from detector.train_classifier import train

metrics = train(
    manifest=Path("var/training/manifest-odot-678.jsonl"),
    frames_roots=[Path("var/training/corpus"), Path("data/blocks")],
    out_onnx=Path("var/training/classifier-odot-678.onnx"),
    epochs=12,
)
```

The default is 6 epochs; around 12 is the working figure, so pass it.
`train` returns tp/fp/tn/fn over the 15% validation split held out of the manifest.
Read that as a smoke signal only - the split is drawn from the same weak labels the model trained on, so it cannot tell you the model got better.
Section 3 is the gate.

## 3. The exam gate

Score the camera's gold labels with the new ONNX, and score them again with the detector currently in production.
Ship only if hard errors go down, where hard errors are missed blockages plus false BLOCKED.
UNKNOWN is not a hard error: a confident wrong answer is worse than an honest refusal, and a model that trades UNKNOWNs for wrong answers has gotten worse even when its accuracy number goes up.

There is no batch scorer for this, and pretending otherwise would be the fastest way to skip the gate.
`blockade-detect explain` scores a single frame and `scan` scores frames from the manifests; neither reads `data/labels/labels.jsonl`.
Scoring gold is a throwaway script: read the label file, run both models over the referenced frames, and tabulate the two error counts.
Build it however you like, but do not ship without the two numbers side by side.

Cameras with thin gold need a substitute exam.
Hold out the hand-saved positives as the test set - the `held_out` set from section 2, which is why that set has to be chosen before the fold-in rather than after - and add a false-positive screen over quiet frames the model has never seen.
A model that passes on held-out positives but lights up on quiet frames has not earned a rollout.

That substitute needs hand-saved positives to exist, and for three cameras they do not.
`odot-677`, `odot-679`, and `odot-682` have no BLOCKED gold and no blocks in `data/blocks/`, so there is nothing to grade a model against and no gate they can pass.
The false-positive screen alone does not close that: it cannot detect a missed train, which is the error class this whole gate exists to hold down.
For those cameras the workflow starts at section 1 rather than here - hand-save a handful of BLOCKED frames spanning night, dawn, and day first.
No model ships without an exam it could have failed.

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
