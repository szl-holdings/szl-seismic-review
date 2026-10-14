"""Advisory joint-support proxy inside the opt-in input contract.

Software checks only. The proxy is the Mahalanobis distance of the scorer's
transformed feature vector from the 164 resolved training rows. It is an
advisory proxy: not joint-support certification and not scientific
qualification, and it never changes score_status or confirmability_score.
"""

import csv
import hashlib
import itertools
import json
import math
import sys
from collections import Counter
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as service  # noqa: E402


CONTRACT = service.INPUT_CONTRACT_V1
PROXY = CONTRACT["joint_support_proxy"]
CODE = "OUTSIDE_TRAINING_JOINT_SUPPORT_PROXY"
REGION = service.MODEL["region"]
CANONICAL_UNITS = {
    "n_pha_total": "count", "n_sta": "count", "moveout_rms": "s", "az_gap": "deg",
    "sec_az_gap": "deg", "dist_nearest_sta_km": "km", "mag": "magnitude",
}
OPT_IN = {"version": CONTRACT["version"], "units": CANONICAL_UNITS}
# 213 phases from 4 stations at the far corner of every other feature range
# (seismic recon PHY-E2-03). Each value sits inside its univariate range.
IMPLAUSIBLE_JOINT = {"n_pha_total": 213, "n_sta": 4, "moveout_rms": 0.083, "az_gap": 305.2,
                     "sec_az_gap": 9.3, "dist_nearest_sta_km": 76.036, "mag": 5.26}
# sha256 over the 400 default-path /api/score bodies (each row as strings, then
# as numbers, one newline after each body), recorded at base 3109b50 before the
# proxy existed.
DEFAULT_PATH_SHA256 = "75d290192f705e087581f5c367c290a7550987c78ee9c235ad96cc2de009c665"


@pytest.fixture
def client():
    return TestClient(service.app)


def panel_rows() -> list[dict]:
    with (ROOT / "data/japan_panel.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 200
    return rows


def label(row: dict) -> str:
    return service.consensus([vote["verdict"] for vote in service.VOTES[row["panel_id"]]])


def features_of(row: dict) -> dict:
    return {name: row[name] for name in service.FEATURES}


def legacy_body(payload: dict) -> bytes:
    scored = service.score_features(payload.get("region", ""), payload["features"])
    if scored["score_status"] == "JAPAN_PANEL_RESEARCH":
        scored["score_status"] = "UNVALIDATED_QUERY"
    return JSONResponse({"model_id": service.MODEL["id"], "claim": service.MODEL["claim"], **scored}).body


def proxy_of(features: dict) -> dict:
    return service.check_input_contract(features, CANONICAL_UNITS)["joint_support_proxy"]


def codes(report: dict) -> list[str]:
    return [item["code"] for item in report["advisory"]]


def test_proxy_is_declared_advisory_with_a_training_derived_threshold():
    assert PROXY["code"] == CODE
    assert PROXY["effect"] == "ADVISORY" and PROXY["evidence_class"] == "DATA_OBSERVED"
    assert PROXY["reference_rows"] == service.MODEL["n_train"] == 164
    assert PROXY["threshold"] == service.JOINT_THRESHOLD == 6.05289
    assert "largest distance among the same 164 resolved training rows" in PROXY["threshold_basis"]
    assert "not joint-support certification" in PROXY["claim"]
    assert "not scientific qualification" in PROXY["claim"]
    assert "never changes score_status or confirmability_score" in PROXY["claim"]
    assert service.JOINT_SUPPORT_ADVISORY["effect"] == "ADVISORY"
    json.dumps(CONTRACT, allow_nan=False)


def test_reference_rows_are_the_stored_training_rows():
    # The startup reference reproduces the stored scaler, so it is the training set.
    assert len(service.JOINT_REFERENCE) == 164
    count = len(service.JOINT_REFERENCE)
    for index, (mean, stored_mean, stored_scale) in enumerate(
            zip(service.JOINT_MEAN, service.MODEL["mean"], service.MODEL["scale"])):
        variance = sum((row[index] - mean) ** 2 for row in service.JOINT_REFERENCE) / count
        assert math.isclose(float(mean), stored_mean, rel_tol=1e-12, abs_tol=1e-12)
        assert math.isclose(math.sqrt(variance), stored_scale, rel_tol=1e-12)
    resolved = [row for row in panel_rows() if label(row) != "unresolved"]
    assert [service.transformed_vector(row) for row in resolved] == [
        [float(value) for value in row] for row in service.JOINT_REFERENCE]


def test_implausible_joint_point_is_flagged_and_still_scored(client):
    # The recon point used the fixture's own sec_az_gap minimum, written
    # 9.300000000000011 in the CSV. With it, every value is inside the
    # per-feature training box and the unchanged scorer returns a score.
    scored_point = {**IMPLAUSIBLE_JOINT, "sec_az_gap": "9.300000000000011"}
    report = service.check_input_contract(scored_point, CANONICAL_UNITS)
    assert report["status"] == "CONFORMS" and report["violations"] == []
    assert codes(report) == ["PHASES_EXCEED_TWO_PER_STATION", CODE]
    assert report["advisory"][-1] == service.JOINT_SUPPORT_ADVISORY
    assert report["joint_support_proxy"] == {"distance_status": "ABOVE_TRAINING_MAXIMUM",
                                             "mahalanobis_distance": 34.128463, "threshold": 6.05289}
    payload = {"region": REGION, "features": scored_point}
    body = client.post("/api/score", json={**payload, "input_contract": OPT_IN}).json()
    assert body["score_status"] == "UNVALIDATED_QUERY"
    assert body["confirmability_score"] == 0.940816
    assert body == {**json.loads(legacy_body(payload)), "input_contract": report}
    default = client.post("/api/score", json=payload)
    assert default.content == legacy_body(payload)
    assert "input_contract" not in default.json()
    assert default.json()["confirmability_score"] == 0.940816

    # Taken literally, sec_az_gap 9.3 sits 1.1e-14 below that stored minimum,
    # so the unchanged box abstains. The proxy flags it all the same and
    # neither causes nor prevents the abstention.
    literal = service.check_input_contract(IMPLAUSIBLE_JOINT, CANONICAL_UNITS)
    assert codes(literal) == ["PHASES_EXCEED_TWO_PER_STATION", CODE]
    assert literal["joint_support_proxy"]["distance_status"] == "ABOVE_TRAINING_MAXIMUM"
    payload = {"region": REGION, "features": IMPLAUSIBLE_JOINT}
    body = client.post("/api/score", json={**payload, "input_contract": OPT_IN}).json()
    assert body == {**json.loads(legacy_body(payload)), "input_contract": literal}
    assert body["score_status"] == "OUT_OF_SUPPORT"


def test_fixture_advisory_counts_are_pinned():
    rows = panel_rows()
    statuses = Counter()
    advisories = Counter()
    distances = {}
    for row in rows:
        report = service.check_input_contract(features_of(row), CANONICAL_UNITS)
        statuses[report["joint_support_proxy"]["distance_status"]] += 1
        advisories.update(codes(report))
        distances[row["panel_id"]] = report["joint_support_proxy"]["mahalanobis_distance"]
    assert statuses == {"AT_OR_BELOW_TRAINING_MAXIMUM": 200}
    assert advisories == Counter()
    # Exactly one training row attains the threshold; it is not flagged.
    training = [row["panel_id"] for row in rows if label(row) != "unresolved"]
    assert len(training) == 164
    attaining = [panel_id for panel_id in training
                 if service.squared_mahalanobis(service.transformed_vector(features_of(
                     service.PUBLISHED[f"published:{panel_id}"]))) == service.JOINT_THRESHOLD_SQUARED]
    assert attaining == ["J040"] and distances["J040"] == 6.05289
    unresolved = {row["panel_id"]: distances[row["panel_id"]] for row in rows if label(row) == "unresolved"}
    assert len(unresolved) == 36 and max(unresolved, key=unresolved.get) == "J160"
    assert unresolved["J160"] == 5.209001
    # The proxy is not the box: the three rows that abstain OUT_OF_SUPPORT are not flagged.
    assert {panel_id: distances[panel_id] for panel_id in ("J092", "J158", "J160")} == {
        "J092": 3.489637, "J158": 3.861775, "J160": 5.209001}


def test_training_box_corners_counts_are_pinned(client):
    # Corners built from the training rows' own per-feature extremes. Every
    # corner gets the unchanged legacy status and score with the contract on.
    training = [row for row in panel_rows() if label(row) != "unresolved"]
    extremes = [(min((row[name] for row in training), key=float), max((row[name] for row in training), key=float))
                for name in service.FEATURES]
    outcomes = Counter()
    for corner in itertools.product(*extremes):
        features = dict(zip(service.FEATURES, corner))
        payload = {"region": REGION, "features": features}
        report = service.check_input_contract(features, CANONICAL_UNITS)
        assert report["status"] == "CONFORMS"
        body = client.post("/api/score", json={**payload, "input_contract": OPT_IN}).json()
        assert body == {**json.loads(legacy_body(payload)), "input_contract": report}
        outcomes[(body["score_status"], report["joint_support_proxy"]["distance_status"])] += 1
    assert outcomes == {("UNVALIDATED_QUERY", "ABOVE_TRAINING_MAXIMUM"): 91,
                        ("UNVALIDATED_QUERY", "AT_OR_BELOW_TRAINING_MAXIMUM"): 5,
                        ("INVALID_FEATURES", "ABOVE_TRAINING_MAXIMUM"): 32}


def test_default_path_bodies_are_byte_identical_to_base(client):
    digest = hashlib.sha256()
    statuses = Counter()
    for row in panel_rows():
        for features in (features_of(row), {name: float(value) for name, value in features_of(row).items()}):
            response = client.post("/api/score", json={"region": REGION, "features": features})
            assert response.status_code == 200
            assert "input_contract" not in response.json()
            digest.update(response.content + b"\n")
            statuses[response.json()["score_status"]] += 1
    assert statuses == {"UNVALIDATED_QUERY": 394, "OUT_OF_SUPPORT": 6}
    assert digest.hexdigest() == DEFAULT_PATH_SHA256


@pytest.mark.parametrize("change,distance", [
    ({"dist_nearest_sta_km": 10 ** 300}, 1.0543381914009763e+299),
    ({"mag": "-1" + "0" * 300}, 1.7463260251811607e+300),
    ({"mag": 1.7976931348623157e308}, None),
], ids=["distance-1e300", "magnitude-decimal-1e300", "distance-beyond-binary64"])
def test_huge_conforming_values_are_flagged_exactly(change, distance):
    report = service.check_input_contract({**features_of(panel_rows()[0]), **change}, CANONICAL_UNITS)
    assert report["status"] == "CONFORMS"
    assert codes(report) == [CODE]
    assert report["joint_support_proxy"] == {"distance_status": "ABOVE_TRAINING_MAXIMUM",
                                             "mahalanobis_distance": distance, "threshold": 6.05289}
    json.dumps(report, allow_nan=False)


def test_proxy_is_unavailable_until_every_feature_parses():
    features = {**features_of(panel_rows()[0]), "mag": "nan"}
    report = service.check_input_contract(features, CANONICAL_UNITS)
    assert report["status"] == "VIOLATION"
    assert report["joint_support_proxy"] == {"distance_status": "UNAVAILABLE", "mahalanobis_distance": None,
                                             "threshold": 6.05289}
    assert CODE not in codes(report)
