"""Train a per-camera blocked/clear classifier and export it to ONNX.

MobileNetV3-small pretrained on ImageNet, backbone frozen, a two-class head
fine-tuned on this camera's frames. Deliberately the simplest learned model
that can win: a fixed camera is a closed world, and a small head over generic
features is enough to separate "train across the background" from "empty
crossing" in any lighting - the thing pixel differencing demonstrably cannot.

Torch lives only here, behind the [train] extra, and only on a workstation.
The runtime ships the exported ONNX and knows nothing about torch.

Plain functions, invoked from a workstation script with the [train] extra
installed; there is no runtime CLI, because training does not belong on the
serving box.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from blockade.detect.classifier import IMAGE_SIZE, LABELS, MEAN, STD

log = logging.getLogger(__name__)


def load_examples(manifest: Path, frames_roots: list[Path], split: str) -> list[tuple[Path, int]]:
    # One walk per root up front, not one rglob per manifest line: on a corpus
    # of thousands of frames the per-line glob dominated training wall-time.
    by_name: dict[str, list[Path]] = {}
    for root in frames_roots:
        for path in root.rglob("*"):
            if path.is_file():
                by_name.setdefault(path.name, []).append(path)

    out = []
    for line in manifest.read_text().splitlines():
        rec = json.loads(line)
        if rec["split"] != split:
            continue
        name = Path(rec["object_key"]).name
        camera_id = rec["camera_id"]
        hit = next((h for h in by_name.get(name, ()) if camera_id in h.parts), None)
        if hit is not None:
            out.append((hit, LABELS.index(rec["label"])))
    return out


def train(
    manifest: Path,
    frames_roots: list[Path],
    out_onnx: Path,
    epochs: int = 6,
    batch_size: int = 32,
    seed: int = 11,
) -> dict:
    """Returns validation metrics. Torch imports deferred to keep the module
    importable without the [train] extra."""
    import torch
    from PIL import Image
    from torch import nn
    from torch.utils.data import DataLoader, Dataset
    from torchvision import models, transforms

    torch.manual_seed(seed)

    # The other half of the preprocessing contract shared with inference,
    # like LABELS: skew here degrades production silently while validation
    # (computed with the same transform) keeps looking healthy.
    normalize = transforms.Normalize(mean=MEAN.tolist(), std=STD.tolist())
    train_tf = transforms.Compose(
        [
            transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
            # Photometric only: the camera never moves, so flips and crops
            # would teach geometry that can never occur at inference.
            transforms.ColorJitter(brightness=0.3, contrast=0.3),
            transforms.ToTensor(),
            normalize,
        ]
    )
    eval_tf = transforms.Compose(
        [transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)), transforms.ToTensor(), normalize]
    )

    class Frames(Dataset):
        def __init__(self, items, tf):
            self.items, self.tf = items, tf

        def __len__(self):
            return len(self.items)

        def __getitem__(self, i):
            path, label = self.items[i]
            with Image.open(path) as img:
                return self.tf(img.convert("RGB")), label

    train_items = load_examples(manifest, frames_roots, "train")
    val_items = load_examples(manifest, frames_roots, "val")
    log.info("train=%d val=%d", len(train_items), len(val_items))

    model = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
    for p in model.parameters():
        p.requires_grad = False
    # The whole classifier head trains, not just the final linear: a single
    # linear probe under-fit the night scenes (6 of 20 validation positives
    # missed). The backbone stays frozen - a few hundred camera-specific
    # frames cannot re-teach ImageNet features, only misremember them.
    model.classifier[3] = nn.Linear(model.classifier[3].in_features, len(LABELS))
    for p in model.classifier.parameters():
        p.requires_grad = True

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model.to(device)
    optimizer = torch.optim.AdamW(model.classifier.parameters(), lr=5e-4)
    # A missed train becomes a missed alert and a hole in the record; a false
    # BLOCKED becomes one wrong minute on the board. Weight the loss the way
    # the product weighs the errors.
    weights = torch.tensor([1.0, 2.0]).to(device)  # [CLEAR, BLOCKED]
    loss_fn = nn.CrossEntropyLoss(weight=weights)

    loader = DataLoader(Frames(train_items, train_tf), batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(Frames(val_items, eval_tf), batch_size=batch_size)

    for epoch in range(epochs):
        model.train()
        for x, y in loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            loss = loss_fn(model(x), y)
            loss.backward()
            optimizer.step()
        model.eval()
        correct = total = 0
        with torch.no_grad():
            for x, y in val_loader:
                pred = model(x.to(device)).argmax(1).cpu()
                correct += (pred == y).sum().item()
                total += len(y)
        log.info("epoch %d val_acc %.3f", epoch + 1, correct / max(total, 1))

    # Confusion on val, the number that matters.
    tp = fp = tn = fn = 0
    model.eval()
    with torch.no_grad():
        for x, y in val_loader:
            pred = model(x.to(device)).argmax(1).cpu()
            for p, t in zip(pred.tolist(), y.tolist(), strict=True):
                if t == 1 and p == 1:
                    tp += 1
                elif t == 0 and p == 1:
                    fp += 1
                elif t == 0 and p == 0:
                    tn += 1
                else:
                    fn += 1

    out_onnx.parent.mkdir(parents=True, exist_ok=True)
    model.cpu()
    torch.onnx.export(
        model,
        torch.zeros(1, 3, IMAGE_SIZE, IMAGE_SIZE),
        str(out_onnx),
        input_names=["image"],
        output_names=["logits"],
        dynamo=False,
    )
    metrics = {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "val_n": tp + fp + tn + fn}
    log.info("exported %s  metrics %s", out_onnx, metrics)
    return metrics
