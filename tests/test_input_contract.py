"""Opt-in input contract v1 for /api/score.

Software checks only. Conformance covers declared units, value forms and
domains. It is not scientific qualification and does not mean an input lies
inside the training support.
"""

import copy
import csv
import json
import math
import sys
from collections import Counter
from decimal import localcontext
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as service  # noqa: E402


CONTRACT = service.INPUT_CONTRACT_V1
REGION = service.MODEL["region"]
CANONICAL_UNITS = {
    "n_pha_total": "count", "n_sta": "count", "moveout_rms": "s", "az_gap": "deg",
    "sec_az_gap": "deg", "dist_nearest_sta_km": "km", "mag": "magnitude",
}
OUT_OF_SUPPORT_PANEL_IDS = {"J092", "J158", "J160"}


@pytest.fixture
def client():
    return TestClient(service.app)


def panel_rows() -> list[dict]:
    with (ROOT / "data/japan_panel.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 200
    return rows


def features_of(row: dict, as_numbers: bool = False) -> dict:
    return {name: float(row[name]) if as_numbers else row[name] for name in service.FEATURES}


def opt_in(units=None) -> dict:
    return {"version": CONTRACT["version"], "units": dict(CANONICAL_UNITS) if units is None else units}


def legacy_body(payload: dict) -> bytes:
    """The /api/score body as rendered before the contract existed."""
    scored = service.score_features(payload.get("region", ""), payload["features"])
    if scored["score_status"] == "JAPAN_PANEL_RESEARCH":
        scored["score_status"] = "UNVALIDATED_QUERY"
    return JSONResponse({"model_id": service.MODEL["id"], "claim": service.MODEL["claim"], **scored}).body


def violation_codes(report: dict) -> list[tuple[str, str]]:
    return [(item["feature"], item["code"]) for item in report["violations"]]


def advisory_codes(report: dict) -> list[str]:
    return [item["code"] for item in report["advisory"]]


def test_contract_declares_units_for_every_feature():
    assert CONTRACT["version"] == "szl.seismic-input/v1"
    assert CONTRACT["model_id"] == service.MODEL["id"] == "szl-japan-confirmability-v1"
    assert tuple(CONTRACT["features"]) == service.FEATURES
    assert {name: spec["unit"] for name, spec in CONTRACT["features"].items()} == CANONICAL_UNITS
    assert {name for name, spec in CONTRACT["features"].items() if spec["kind"] == "integer"} == {
        "n_pha_total", "n_sta"}
    assert CONTRACT["unit_policy"] == "REFUSE_MISMATCH_NEVER_CONVERT"
    assert CONTRACT["features"]["mag"]["scale"] == "UNDECLARED_IN_SOURCE_FIXTURE"
    assert CONTRACT["features"]["sec_az_gap"]["definition_status"] == "UNVERIFIED"
    invariants = {item["code"]: item for item in CONTRACT["observed_invariants"]}
    assert set(invariants) == {"PHASES_EXCEED_TWO_PER_STATION", "SECONDARY_GAP_EXCEEDS_PRIMARY",
                               "GAPS_SUM_EXCEEDS_360"}
    assert all(item["effect"] == "ADVISORY" and item["evidence_class"] == "DATA_OBSERVED"
               for item in invariants.values())
    assert invariants["SECONDARY_GAP_EXCEEDS_PRIMARY"]["definition_status"] == "UNVERIFIED"
    assert invariants["GAPS_SUM_EXCEEDS_360"]["definition_status"] == "UNVERIFIED"
    assert "not scientific qualification" in CONTRACT["claim"]
    json.dumps(CONTRACT, allow_nan=False)


def test_score_without_contract_key_is_byte_identical(client):
    rows = panel_rows()
    first = features_of(rows[0])
    row_payloads = [{"region": REGION, "features": features_of(row, as_numbers)}
                    for row in rows for as_numbers in (False, True)]
    edge_payloads = [
        {"region": "ridgecrest", "features": first},
        {"features": first},
        {"region": REGION, "features": {**first, "mag": None}},
        {"region": REGION, "features": {**first, "az_gap": 400}},
        # Refused only in contract mode; the default path is deliberately unchanged.
        {"region": REGION, "features": {**first, "n_sta": 5.5}},
        {"region": REGION, "features": {**first, "mag": True}},
        {"region": REGION, "features": {**first, "n_pha_total": "2_7"}},
    ]
    statuses = Counter()
    for position, payload in enumerate(row_payloads + edge_payloads):
        response = client.post("/api/score", json=payload)
        assert response.status_code == 200
        assert response.content == legacy_body(payload)
        assert "input_contract" not in response.json()
        if position < len(row_payloads):
            statuses[response.json()["score_status"]] += 1
    assert statuses == {"UNVALIDATED_QUERY": 394, "OUT_OF_SUPPORT": 6}


def test_all_fixture_rows_conform_and_score_unchanged(client):
    statuses = Counter()
    abstained = set()
    for row in panel_rows():
        for features in (features_of(row), features_of(row, as_numbers=True)):
            report = service.check_input_contract(features, CANONICAL_UNITS)
            assert report == {"version": CONTRACT["version"], "status": "CONFORMS", "violations": [],
                              "advisory": [], "claim": CONTRACT["claim"]}, row["panel_id"]
            payload = {"region": REGION, "features": features}
            response = client.post("/api/score", json={**payload, "input_contract": opt_in()})
            assert response.status_code == 200
            body = response.json()
            assert body == {**json.loads(legacy_body(payload)), "input_contract": report}
            statuses[body["score_status"]] += 1
            if body["score_status"] == "OUT_OF_SUPPORT":
                abstained.add(row["panel_id"])
    # Conforming to the contract does not mean an input is inside the training support.
    assert statuses == {"UNVALIDATED_QUERY": 394, "OUT_OF_SUPPORT": 6}
    assert abstained == OUT_OF_SUPPORT_PANEL_IDS


def test_declared_unit_mismatches_are_refused_never_converted(client):
    row = panel_rows()[0]
    features = features_of(row)
    mismatches = [
        ("moveout_rms", "ms", {"moveout_rms": float(row["moveout_rms"]) * 1000}),
        ("moveout_rms", "ms", {}),  # seconds value labelled ms: refused, not converted
        ("moveout_rms", "seconds", {}),  # only the contract token is accepted
        ("dist_nearest_sta_km", "mi", {"dist_nearest_sta_km": float(row["dist_nearest_sta_km"]) / 1.609344}),
        ("dist_nearest_sta_km", "m", {"dist_nearest_sta_km": float(row["dist_nearest_sta_km"]) * 1000}),
        ("az_gap", "rad", {"az_gap": math.radians(float(row["az_gap"]))}),
        ("sec_az_gap", "rad", {"sec_az_gap": math.radians(float(row["sec_az_gap"]))}),
        ("mag", "Mw", {}),  # the source fixture does not declare its magnitude scale
        ("n_sta", 1, {}),
    ]
    for feature, unit, change in mismatches:
        payload = {"region": REGION, "features": {**features, **change},
                   "input_contract": opt_in({**CANONICAL_UNITS, feature: unit})}
        body = client.post("/api/score", json=payload).json()
        assert set(body) == {"model_id", "claim", "score_status", "confirmability_score", "input_contract"}
        assert body["score_status"] == "INPUT_CONTRACT_VIOLATION"
        assert body["confirmability_score"] is None
        assert body["input_contract"]["status"] == "VIOLATION"
        assert violation_codes(body["input_contract"]) == [(feature, "UNIT_MISMATCH")], (feature, unit)
        assert body["input_contract"]["violations"][0]["expected"] == CANONICAL_UNITS[feature]
    without_moveout = {name: unit for name, unit in CANONICAL_UNITS.items() if name != "moveout_rms"}
    undeclared = [
        (without_moveout, [("moveout_rms", "UNIT_UNDECLARED")]),
        ({**CANONICAL_UNITS, "mag": None}, [("mag", "UNIT_UNDECLARED")]),
        ("s", [(name, "UNIT_UNDECLARED") for name in service.FEATURES]),
    ]
    for units, expected in undeclared:
        body = client.post("/api/score", json={"region": REGION, "features": features,
                                               "input_contract": opt_in(units)}).json()
        assert body["score_status"] == "INPUT_CONTRACT_VIOLATION"
        assert violation_codes(body["input_contract"]) == expected
    no_units = client.post("/api/score", json={"region": REGION, "features": features,
                                               "input_contract": {"version": CONTRACT["version"]}}).json()
    assert violation_codes(no_units["input_contract"]) == [(name, "UNIT_UNDECLARED") for name in service.FEATURES]
    extra = service.check_input_contract(features, {**CANONICAL_UNITS, "depth": "km"})
    assert extra["status"] == "CONFORMS"


def test_value_forms_refused_only_in_contract_mode(client):
    row = panel_rows()[0]
    features = features_of(row)
    cases = [
        ("n_sta", 5.5, "NON_INTEGER_COUNT"),
        ("n_pha_total", "7.25", "NON_INTEGER_COUNT"),
        ("mag", True, "NON_NUMERIC"),
        ("n_pha_total", False, "NON_NUMERIC"),
        ("n_pha_total", "1_9", "NON_NUMERIC"),
        ("moveout_rms", "5e-1", "NON_NUMERIC"),
        ("moveout_rms", " 0.5", "NON_NUMERIC"),
        ("moveout_rms", "+0.5", "NON_NUMERIC"),
        ("mag", "nan", "NON_NUMERIC"),
        ("mag", "Infinity", "NON_NUMERIC"),
        ("n_sta", "١٥", "NON_NUMERIC"),  # non-ASCII digits that float() would accept
        ("mag", None, "NON_NUMERIC"),
        ("mag", [1.0], "NON_NUMERIC"),
        ("moveout_rms", -0.1, "DOMAIN"),
        ("az_gap", 360.5, "DOMAIN"),
        ("sec_az_gap", "-1", "DOMAIN"),
        ("n_sta", -3, "DOMAIN"),
        ("dist_nearest_sta_km", 10 ** 400, "DOMAIN"),
        ("mag", "1" * 400, "DOMAIN"),
    ]
    for feature, value, code in cases:
        changed = {**features, feature: value}
        report = service.check_input_contract(changed, CANONICAL_UNITS)
        assert violation_codes(report) == [(feature, code)], (feature, value)
        body = client.post("/api/score", json={"region": REGION, "features": changed,
                                               "input_contract": opt_in()}).json()
        assert body["score_status"] == "INPUT_CONTRACT_VIOLATION" and body["confirmability_score"] is None
        default = client.post("/api/score", json={"region": REGION, "features": changed})
        assert default.content == legacy_body({"region": REGION, "features": changed})
    missing = {name: value for name, value in features.items() if name != "mag"}
    assert violation_codes(service.check_input_contract(missing, CANONICAL_UNITS)) == [("mag", "NON_NUMERIC")]
    not_a_number = {"region": REGION, "features": {**features, "mag": math.nan}, "input_contract": opt_in()}
    body = client.post("/api/score", content=json.dumps(not_a_number).encode()).json()
    assert violation_codes(body["input_contract"]) == [("mag", "DOMAIN")]
    integral = service.check_input_contract({**features, "n_sta": 20.0, "n_pha_total": "27.0"}, CANONICAL_UNITS)
    assert integral["status"] == "CONFORMS"


@pytest.mark.parametrize("feature,value,code", [
    ("n_pha_total", "27.0000000000000001", "NON_INTEGER_COUNT"),
    ("n_sta", "20.0000000000000001", "NON_INTEGER_COUNT"),
    ("n_sta", "0.0000000000000001", "NON_INTEGER_COUNT"),
    ("az_gap", "360.0000000000000001", "DOMAIN"),
    ("sec_az_gap", "360.0000000000000001", "DOMAIN"),
    ("moveout_rms", "-0." + "0" * 400 + "1", "DOMAIN"),
    ("dist_nearest_sta_km", "-0." + "0" * 400 + "1", "DOMAIN"),
    ("n_sta", "-0." + "0" * 400 + "1", "DOMAIN"),
    ("n_sta", 10 ** 4000, "DOMAIN"),
    ("n_sta", True, "NON_NUMERIC"),
    ("mag", "+0.5", "NON_NUMERIC"),
    ("mag", "1e0", "NON_NUMERIC"),
    ("mag", "1_0", "NON_NUMERIC"),
    ("mag", "０.５", "NON_NUMERIC"),
    ("mag", "0" * 4097, "NON_NUMERIC"),
], ids=["fractional-phase", "fractional-station", "tiny-fractional-station", "primary-over-360",
        "secondary-over-360", "negative-rms-underflow", "negative-distance-underflow",
        "negative-station-underflow", "huge-integer", "boolean", "plus", "exponent",
        "underscore", "non-ascii", "overlong-decimal"])
def test_exact_decimal_violations_before_scoring(client, feature, value, code):
    features = {**features_of(panel_rows()[0]), feature: value}
    report = service.check_input_contract(features, CANONICAL_UNITS)
    assert violation_codes(report) == [(feature, code)]
    response = client.post("/api/score", json={"region": REGION, "features": features,
                                               "input_contract": opt_in()})
    assert response.status_code == 200
    assert response.json()["score_status"] == "INPUT_CONTRACT_VIOLATION"
    assert response.json()["confirmability_score"] is None
    assert violation_codes(response.json()["input_contract"]) == [(feature, code)]


@pytest.mark.parametrize("feature,value", [
    ("n_pha_total", "27.0"), ("n_sta", "20.0000000000000000"),
    ("n_sta", "-0.0000000000000000"), ("moveout_rms", "-0.0"),
    ("moveout_rms", "0." + "0" * 400 + "1"),
    ("az_gap", "0.0000000000000001"),
    ("az_gap", "359.9999999999999999"), ("az_gap", "360.0000000000000000"),
])
def test_exact_decimal_boundaries_preserve_legacy_scores(client, feature, value):
    features = {**features_of(panel_rows()[0]), feature: value}
    payload = {"region": REGION, "features": features}
    report = service.check_input_contract(features, CANONICAL_UNITS)
    assert report["status"] == "CONFORMS"
    response = client.post("/api/score", json={**payload, "input_contract": opt_in()})
    assert response.status_code == 200
    assert response.json() == {**json.loads(legacy_body(payload)), "input_contract": report}


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_json_numbers_are_domain_violations(client, value):
    payload = {"region": REGION, "features": {**features_of(panel_rows()[0]), "mag": value},
               "input_contract": opt_in()}
    response = client.post("/api/score", content=json.dumps(payload).encode())
    assert response.status_code == 200
    assert violation_codes(response.json()["input_contract"]) == [("mag", "DOMAIN")]


def test_exact_advisories_do_not_round_or_depend_on_decimal_context():
    features = {**features_of(panel_rows()[0]), "n_pha_total": 2 ** 54 + 1,
                "n_sta": 2 ** 53, "az_gap": "180.0000000000000000",
                "sec_az_gap": "180.0000000000000001"}
    expected = ["PHASES_EXCEED_TWO_PER_STATION", "SECONDARY_GAP_EXCEEDS_PRIMARY",
                "GAPS_SUM_EXCEEDS_360"]
    for precision in (1, 2, 28, 100):
        with localcontext() as context:
            context.prec = precision
            report = service.check_input_contract(features, CANONICAL_UNITS)
        assert report["status"] == "CONFORMS"
        assert advisory_codes(report) == expected
    boundary = {**features, "n_pha_total": 2 ** 54, "sec_az_gap": "180.0000000000000000"}
    assert advisory_codes(service.check_input_contract(boundary, CANONICAL_UNITS)) == []


def test_decoded_json_float_precision_is_not_recoverable(client):
    # json.loads already rounded this numeric token. Plain decimal strings are
    # required when the sender needs their original decimal precision checked.
    features = {**features_of(panel_rows()[0]), "n_pha_total": json.loads("27.0000000000000001")}
    assert features["n_pha_total"] == 27.0
    report = service.check_input_contract(features, CANONICAL_UNITS)
    assert report["status"] == "CONFORMS"
    payload = {"region": REGION, "features": features}
    response = client.post("/api/score", json={**payload, "input_contract": opt_in()})
    assert response.json() == {**json.loads(legacy_body(payload)), "input_contract": report}


def test_advisory_relations_never_change_status(client):
    """DATA_OBSERVED advisories only. Joint support stays an open owner decision."""
    joint = {"n_pha_total": 213, "n_sta": 4, "moveout_rms": 0.083, "az_gap": 305.2,
             "sec_az_gap": 9.3, "dist_nearest_sta_km": 76.036, "mag": 5.26}
    flipped = {**features_of(panel_rows()[0]), "az_gap": "50.8", "sec_az_gap": "214.0"}
    wide = {**features_of(panel_rows()[0]), "az_gap": 300, "sec_az_gap": 70}
    every = {**joint, "n_pha_total": 30, "n_sta": 10, "az_gap": 200, "sec_az_gap": 210}
    expectations = [
        (joint, ["PHASES_EXCEED_TWO_PER_STATION"]),
        (flipped, ["SECONDARY_GAP_EXCEEDS_PRIMARY"]),
        (wide, ["GAPS_SUM_EXCEEDS_360"]),
        (every, ["PHASES_EXCEED_TWO_PER_STATION", "SECONDARY_GAP_EXCEEDS_PRIMARY", "GAPS_SUM_EXCEEDS_360"]),
    ]
    invariants = {item["code"]: item for item in CONTRACT["observed_invariants"]}
    for features, codes in expectations:
        report = service.check_input_contract(features, CANONICAL_UNITS)
        assert report["status"] == "CONFORMS" and report["violations"] == []
        assert advisory_codes(report) == codes
        assert report["advisory"] == [invariants[code] for code in codes]
        payload = {"region": REGION, "features": features}
        body = client.post("/api/score", json={**payload, "input_contract": opt_in()}).json()
        assert body == {**json.loads(legacy_body(payload)), "input_contract": report}
    mislabelled = service.check_input_contract(joint, {**CANONICAL_UNITS, "mag": "Mw"})
    assert mislabelled["status"] == "VIOLATION"
    assert advisory_codes(mislabelled) == ["PHASES_EXCEED_TWO_PER_STATION"]


def test_contract_key_requires_supported_version(client):
    features = features_of(panel_rows()[0])
    for contract in (None, CONTRACT["version"], [], {}, {"units": CANONICAL_UNITS},
                     {"version": "szl.seismic-input/v2", "units": CANONICAL_UNITS}):
        response = client.post("/api/score", json={"region": REGION, "features": features,
                                                   "input_contract": contract})
        assert response.status_code == 422, contract


def test_check_input_contract_is_pure():
    features = features_of(panel_rows()[0])
    units = dict(CANONICAL_UNITS)
    before = copy.deepcopy((features, units, CONTRACT))
    assert service.check_input_contract(features, units) == service.check_input_contract(features, units)
    assert (features, units, CONTRACT) == before
    report = service.check_input_contract({**features, "az_gap": 300, "sec_az_gap": 70}, units)
    report["advisory"][0]["effect"] = "CHANGED"
    assert CONTRACT == before[2]
    assert violation_codes(service.check_input_contract(None, units)) == [
        (name, "NON_NUMERIC") for name in service.FEATURES]
