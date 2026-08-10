"""Score detector observations against a labelled reference set.

The headline number people reach for is agreement rate, and it is the least
useful thing here. Two failure modes matter far more, and they are not
symmetric:

- **Overconfidence** - the detector states BLOCKED or CLEAR where the reference
  says the frame is unreadable. This is the one that corrupts the dataset,
  because it manufactures observations out of frames nobody can actually read,
  and no downstream analysis can detect it after the fact.
- **Missed blockage** - the detector says CLEAR where the reference says
  BLOCKED. This is what makes the alert useless, and it is the failure a user
  notices.

A detector that answers UNKNOWN too often is merely less useful. One that
answers confidently and wrongly is worse than none, so they are reported apart.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from blockade.config import REPO_ROOT
from blockade.schemas import CrossingState, ObservationRecord

DEFAULT_LABELS = REPO_ROOT / "data" / "labels" / "labels.jsonl"


@dataclass(frozen=True)
class Label:
    """A reference judgement about one frame."""

    object_key: str
    label: CrossingState
    lighting: str
    reason: str
    labeller: str

    @property
    def is_confident(self) -> bool:
        return self.label is not CrossingState.UNKNOWN


def load_labels(path: Path | None = None) -> dict[str, Label]:
    """Load the reference set, keyed by object key."""
    path = path or DEFAULT_LABELS
    if not path.exists():
        raise FileNotFoundError(f"No label set at {path}.")
    labels: dict[str, Label] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        labels[raw["object_key"]] = Label(
            object_key=raw["object_key"],
            label=CrossingState(raw["label"]),
            lighting=raw.get("lighting", "unknown"),
            reason=raw.get("reason", ""),
            labeller=raw.get("labeller", "unknown"),
        )
    return labels


@dataclass
class Score:
    """Evaluation over one slice of frames."""

    slice_name: str
    matrix: Counter = field(default_factory=Counter)  # (label, predicted) -> n

    @property
    def total(self) -> int:
        return sum(self.matrix.values())

    @property
    def agreements(self) -> int:
        return sum(n for (lab, pred), n in self.matrix.items() if lab == pred)

    @property
    def agreement_rate(self) -> float:
        return self.agreements / self.total if self.total else 0.0

    @property
    def overconfident(self) -> int:
        """Detector committed to a state on a frame the reference calls unreadable.

        The dataset-corrupting failure: these become rows that look like
        measurements but describe frames nobody could read.
        """
        return sum(
            n
            for (lab, pred), n in self.matrix.items()
            if lab is CrossingState.UNKNOWN and pred is not CrossingState.UNKNOWN
        )

    @property
    def missed_blockages(self) -> int:
        """Reference says BLOCKED, detector says CLEAR. The alert-killing failure."""
        return self.matrix[(CrossingState.BLOCKED, CrossingState.CLEAR)]

    @property
    def false_blockages(self) -> int:
        return self.matrix[(CrossingState.CLEAR, CrossingState.BLOCKED)]

    @property
    def abstentions(self) -> int:
        """Detector said UNKNOWN where the reference could tell. Costs coverage,
        does not corrupt anything."""
        return sum(
            n
            for (lab, pred), n in self.matrix.items()
            if lab is not CrossingState.UNKNOWN and pred is CrossingState.UNKNOWN
        )


def evaluate(
    observations: list[ObservationRecord], labels: dict[str, Label]
) -> dict[str, Score]:
    """Score observations against labels, sliced by lighting condition.

    Sliced because a single overall number hides the thing worth knowing: these
    cameras behave like different sensors by day and by night, and a detector
    that is excellent in daylight and dangerous after dark averages out to
    "fine".
    """
    scores: dict[str, Score] = {"overall": Score("overall")}
    for obs in observations:
        label = labels.get(obs.object_key)
        if label is None:
            continue
        cell = (label.label, obs.state)
        scores["overall"].matrix[cell] += 1
        scores.setdefault(label.lighting, Score(label.lighting)).matrix[cell] += 1
    return scores


def format_report(scores: dict[str, Score]) -> str:
    """Human-readable summary. Leads with the failures, not the headline rate."""
    lines: list[str] = []
    order = ["overall"] + sorted(k for k in scores if k != "overall")
    for name in order:
        score = scores[name]
        if not score.total:
            continue
        lines.append(f"\n{name}  (n={score.total})")
        lines.append(f"  agreement          {score.agreement_rate:6.1%}")
        lines.append(f"  overconfident      {score.overconfident:3d}   <- corrupts the dataset")
        lines.append(f"  missed blockages   {score.missed_blockages:3d}   <- breaks the alert")
        lines.append(f"  false blockages    {score.false_blockages:3d}")
        lines.append(f"  abstentions        {score.abstentions:3d}   (honest, costs coverage)")

    lines.append("\nconfusion (reference -> detector)")
    for (lab, pred), n in sorted(scores["overall"].matrix.items(), key=lambda kv: -kv[1]):
        mark = "  ok" if lab == pred else ""
        lines.append(f"  {lab.value:<8} -> {pred.value:<8} {n:3d}{mark}")
    return "\n".join(lines)
