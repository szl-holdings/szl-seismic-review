"""Train a region-bound confirmation model from the pinned Japan panel.

The target is published human confirmability, not earthquake existence.
This script uses no author model code or precomputed author scores.
"""

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
FEATURES = (
    "n_pha_total",
    "n_sta",
    "moveout_rms",
    "az_gap",
    "sec_az_gap",
    "dist_nearest_sta_km",
    "mag",
)
LOG_FEATURES = {"n_pha_total", "n_sta"}
SEED = 20260924


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vector(row: dict, features=FEATURES) -> list[float]:
    output = []
    for name in features:
        value = float(row[name])
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if name in LOG_FEATURES:
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
            value = math.log1p(value)
        output.append(value)
    return output


def consensus(votes: list[str]) -> str:
    if len(votes) != 4:
        raise ValueError("each reference case needs four published verdicts")
    counts = Counter(votes)
    maximum = max(counts.values())
    leaders = [name for name, count in counts.items() if count == maximum]
    if len(leaders) != 1 or leaders[0] == "uncertain":
        return "unresolved"
    return "confirmed" if leaders[0] == "real" else "rejected"


def ece5(y: np.ndarray, probabilities: np.ndarray) -> float:
    boundaries = np.linspace(0, 1, 6)
    total = 0.0
    for low, high in zip(boundaries[:-1], boundaries[1:]):
        mask = (probabilities >= low) & (probabilities < high if high < 1 else probabilities <= high)
        if mask.any():
            total += mask.mean() * abs(y[mask].mean() - probabilities[mask].mean())
    return float(total)


def evaluate(X: np.ndarray, y: np.ndarray) -> dict:
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    oof = np.full(len(y), np.nan)
    for train, test in folds.split(X, y):
        pipeline = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=1.0, max_iter=2000, random_state=SEED),
        )
        pipeline.fit(X[train], y[train])
        oof[test] = pipeline.predict_proba(X[test])[:, 1]
    if np.isnan(oof).any():
        raise AssertionError("every resolved case needs an out-of-fold score")
    rng = np.random.default_rng(SEED)
    bootstraps = []
    for _ in range(2000):
        sample = rng.integers(0, len(y), len(y))
        if len(np.unique(y[sample])) == 2:
            bootstraps.append(roc_auc_score(y[sample], oof[sample]))
    return {
        "roc_auc_oof": float(roc_auc_score(y, oof)),
        "roc_auc_oof_bootstrap_95pct": [float(x) for x in np.percentile(bootstraps, [2.5, 97.5])],
        "brier_oof": float(brier_score_loss(y, oof)),
        "ece5_oof_unweighted": ece5(y, oof),
        "cv": "5-fold stratified random CV; preprocessing fitted in each training fold",
        "holdout_limit": "Same Japan panel and author-reviewers; not a new region or independent-panel test",
    }


def train() -> dict:
    source = json.loads((ROOT / "data/source_manifest.json").read_text(encoding="utf-8"))
    for name, expected in source["derived_sha256"].items():
        if sha256(ROOT / "data" / name) != expected:
            raise ValueError(f"derived data checksum mismatch: {name}")
    with (ROOT / "data/japan_panel.csv").open(newline="", encoding="utf-8") as handle:
        panel = list(csv.DictReader(handle))
    with (ROOT / "data/japan_verdicts.csv").open(newline="", encoding="utf-8") as handle:
        verdicts = list(csv.DictReader(handle))
    votes = defaultdict(list)
    for row in verdicts:
        votes[row["panel_id"]].append(row["verdict"])
    labels = [consensus(votes[row["panel_id"]]) for row in panel]
    resolved = [index for index, label in enumerate(labels) if label != "unresolved"]
    X = np.asarray([vector(panel[index]) for index in resolved], dtype=float)
    y = np.asarray([labels[index] == "confirmed" for index in resolved], dtype=int)
    if len(panel) != 200 or len(verdicts) != 800 or len(np.unique(y)) != 2:
        raise ValueError("unexpected Japan reference sample")
    measured = evaluate(X, y)
    baseline_indices = [FEATURES.index(name) for name in ("n_pha_total", "n_sta", "moveout_rms")]
    baseline = evaluate(X[:, baseline_indices], y)
    pipeline = make_pipeline(
        StandardScaler(),
        LogisticRegression(C=1.0, max_iter=2000, random_state=SEED),
    )
    pipeline.fit(X, y)
    scaler, classifier = pipeline.steps[0][1], pipeline.steps[1][1]
    artifact = {
        "id": "szl-japan-confirmability-v1",
        "model_kind": "L2 logistic regression on seven association features",
        "source_doi": source["source"],
        "source_version": source["source_version"],
        "source_sha256": source["derived_sha256"],
        "region": "japan_forearc",
        "target": "four author-reviewer plurality: confirmed vs rejected; uncertain or tied excluded",
        "claim": "Research-only human confirmability; not earthquake existence, hazard or forecast probability",
        "features": list(FEATURES),
        "transforms": {name: "log1p" for name in LOG_FEATURES},
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "coefficient": classifier.coef_[0].tolist(),
        "intercept": float(classifier.intercept_[0]),
        "training_min": X.min(axis=0).tolist(),
        "training_max": X.max(axis=0).tolist(),
        "n_panel": len(panel),
        "n_train": len(resolved),
        "class_counts": dict(Counter(labels)),
        "evaluation": measured,
        "three_feature_baseline": baseline,
        "training_seed": SEED,
        "algorithm_version": "scikit-learn 1.9.0",
        "waveform_review": "unavailable in pinned archive",
    }
    output = ROOT / "models/japan_assoc_logistic_v1.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return artifact


if __name__ == "__main__":
    result = train()
    print(json.dumps({key: result[key] for key in ("id", "n_train", "class_counts", "evaluation", "three_feature_baseline")}, indent=2))
