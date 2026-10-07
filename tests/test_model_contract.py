"""Model-contract checks for the committed Japan fixture and logistic artifact.

These are software checks. They pin today's agreement between the preprocessing
and coefficients stored in models/japan_assoc_logistic_v1.json, the published
fixture, and the runtime scorer in app.py. They do not qualify the model
scientifically and say nothing about regions, reviewers or catalogues outside
the published Japan panel.
"""

import csv
import math
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as service  # noqa: E402


MODEL = service.MODEL
EXPECTED_FEATURES = (
    "n_pha_total", "n_sta", "moveout_rms", "az_gap", "sec_az_gap", "dist_nearest_sta_km", "mag",
)
COUNT_FEATURES = ("n_pha_total", "n_sta")
# The only transform name the runtime implements (app.py applies math.log1p).
SUPPORTED_TRANSFORMS = {"log1p": math.log1p}
GOLDEN_SCORES = {"J001": 0.94531, "J002": 0.898346, "J100": 0.988871, "J200": 0.302323}
OUT_OF_SUPPORT_PANEL_IDS = {"J092", "J158", "J160"}
IN_SUPPORT = {"score_status": "JAPAN_PANEL_RESEARCH"}
ABSTAIN = {"score_status": "OUT_OF_SUPPORT", "confirmability_score": None}


def panel_rows() -> list[dict]:
    with (ROOT / "data/japan_panel.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == MODEL["n_panel"] == 200
    return rows


def panel_labels(rows: list[dict]) -> dict[str, str]:
    return {
        row["panel_id"]: service.consensus([vote["verdict"] for vote in service.VOTES[row["panel_id"]]])
        for row in rows
    }


def transformed(row: dict) -> list[float]:
    vector = []
    for name in service.FEATURES:
        raw = float(row[name])
        transform = MODEL["transforms"].get(name)
        vector.append(SUPPORTED_TRANSFORMS[transform](raw) if transform else raw)
    return vector


def in_training_box(row: dict) -> bool:
    return all(minimum <= value <= maximum for value, minimum, maximum in zip(
        transformed(row), MODEL["training_min"], MODEL["training_max"]))


def test_stored_preprocessing_matches_fixture():
    rows = panel_rows()
    labels = panel_labels(rows)
    assert dict(Counter(labels.values())) == MODEL["class_counts"] == {
        "confirmed": 139, "rejected": 25, "unresolved": 36}
    resolved = np.array([transformed(row) for row in rows if labels[row["panel_id"]] != "unresolved"])
    assert resolved.shape == (MODEL["n_train"], len(service.FEATURES)) == (164, 7)
    # StandardScaler semantics: population standard deviation (ddof=0).
    np.testing.assert_allclose(resolved.mean(axis=0), MODEL["mean"], rtol=1e-12, atol=0)
    np.testing.assert_allclose(resolved.std(axis=0, ddof=0), MODEL["scale"], rtol=1e-12, atol=0)
    assert resolved.min(axis=0).tolist() == MODEL["training_min"]
    assert resolved.max(axis=0).tolist() == MODEL["training_max"]


def test_runtime_scores_match_stored_coefficients():
    rows = panel_rows()
    labels = panel_labels(rows)
    matrix = np.array([transformed(row) for row in rows])
    inside = np.all((matrix >= np.array(MODEL["training_min"]))
                    & (matrix <= np.array(MODEL["training_max"])), axis=1)
    standardized = (matrix - np.array(MODEL["mean"])) / np.array(MODEL["scale"])
    expected = 1.0 / (1.0 + np.exp(-(standardized @ np.array(MODEL["coefficient"]) + MODEL["intercept"])))
    statuses = Counter()
    scores = {}
    for row, row_inside, probability in zip(rows, inside, expected):
        result = service.score_features(MODEL["region"], row)
        statuses[result["score_status"]] += 1
        if row_inside:
            assert result == {**IN_SUPPORT, "confirmability_score": round(float(probability), 6)}, row["panel_id"]
            assert abs(result["confirmability_score"] - float(probability)) <= 5e-7
            scores[row["panel_id"]] = result["confirmability_score"]
        else:
            assert result == ABSTAIN, row["panel_id"]
    assert statuses == {"JAPAN_PANEL_RESEARCH": 197, "OUT_OF_SUPPORT": 3}
    abstained = {row["panel_id"] for row, row_inside in zip(rows, inside) if not row_inside}
    assert abstained == OUT_OF_SUPPORT_PANEL_IDS
    # The box is fitted on resolved rows, so only unresolved rows can fall outside it.
    assert {labels[panel_id] for panel_id in abstained} == {"unresolved"}
    assert {panel_id: scores[panel_id] for panel_id in GOLDEN_SCORES} == GOLDEN_SCORES


def test_model_declares_only_supported_transforms():
    # app.py applies log1p to any feature named in MODEL["transforms"] without
    # reading the declared transform name, so pin the artifact to that one name.
    assert service.FEATURES == tuple(MODEL["features"]) == EXPECTED_FEATURES
    assert set(MODEL["transforms"].values()) == set(SUPPORTED_TRANSFORMS) == {"log1p"}
    assert set(MODEL["transforms"]) <= set(service.FEATURES)
    assert set(MODEL["transforms"]) == set(COUNT_FEATURES)
    for key in ("mean", "scale", "coefficient", "training_min", "training_max"):
        assert len(MODEL[key]) == len(service.FEATURES), key
        assert all(isinstance(value, float) and math.isfinite(value) for value in MODEL[key]), key
    assert all(scale > 0 for scale in MODEL["scale"])
    assert all(low < high for low, high in zip(MODEL["training_min"], MODEL["training_max"]))
    assert math.isfinite(MODEL["intercept"])


def test_unit_mislabels_leaving_training_box_abstain():
    """Positive guarantees only: these mislabels leave the per-feature box.

    The abstention comes from the training box and is reported as
    OUT_OF_SUPPORT, never as a unit error; the service has no unit check.
    Other mislabels (for example miles for kilometres or a magnitude-scale
    offset) can stay inside the box and are deliberately not pinned here.
    """
    index = {name: position for position, name in enumerate(service.FEATURES)}
    low, high = MODEL["training_min"], MODEL["training_max"]
    # Box-level reasons each mislabel abstains for any in-box true value.
    assert low[index["moveout_rms"]] * 1000 > high[index["moveout_rms"]]
    assert low[index["dist_nearest_sta_km"]] * 1000 > high[index["dist_nearest_sta_km"]]
    assert 2 * math.pi < low[index["az_gap"]] and 2 * math.pi < low[index["sec_az_gap"]]
    mislabels = {
        "moveout_rms_in_ms": lambda row: {"moveout_rms": float(row["moveout_rms"]) * 1000},
        "distance_in_metres": lambda row: {"dist_nearest_sta_km": float(row["dist_nearest_sta_km"]) * 1000},
        "az_gap_in_radians": lambda row: {"az_gap": math.radians(float(row["az_gap"]))},
        "sec_az_gap_in_radians": lambda row: {"sec_az_gap": math.radians(float(row["sec_az_gap"]))},
        "both_gaps_in_radians": lambda row: {"az_gap": math.radians(float(row["az_gap"])),
                                             "sec_az_gap": math.radians(float(row["sec_az_gap"]))},
    }
    checked = Counter()
    for row in panel_rows():
        if not in_training_box(row):
            continue
        assert service.score_features(MODEL["region"], row)["score_status"] == "JAPAN_PANEL_RESEARCH"
        for label, change in mislabels.items():
            assert service.score_features(MODEL["region"], {**row, **change(row)}) == ABSTAIN, (label, row["panel_id"])
            checked[label] += 1
    assert checked == {label: 197 for label in mislabels}


def test_fixture_feature_semantics():
    """DATA_OBSERVED: relations that hold in all 200 published rows.

    These describe the committed fixture, not physical law, and the runtime
    enforces only n_sta <= n_pha_total. The sec_az_gap relations match a
    'second-largest gap' reading, which is UNVERIFIED against the source
    pipeline's code.
    """
    violations = []
    for row in panel_rows():
        panel_id = row["panel_id"]
        for name in COUNT_FEATURES:
            if not re.fullmatch(r"[0-9]+", row[name]):
                violations.append((panel_id, f"{name} is not a plain integer count"))
        n_pha_total, n_sta = int(row["n_pha_total"]), int(row["n_sta"])
        az_gap, sec_az_gap = float(row["az_gap"]), float(row["sec_az_gap"])
        if not n_sta <= n_pha_total <= 2 * n_sta:
            violations.append((panel_id, "n_sta <= n_pha_total <= 2 * n_sta"))
        if not 0 <= sec_az_gap <= az_gap <= 360:
            violations.append((panel_id, "0 <= sec_az_gap <= az_gap <= 360"))
        if not az_gap + sec_az_gap <= 360:
            violations.append((panel_id, "az_gap + sec_az_gap <= 360"))
    assert violations == []
