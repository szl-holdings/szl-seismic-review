"""Real local transactions with SAMPLE inputs and isolated role credentials."""

from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as service


@pytest.fixture
def operator(tmp_path, monkeypatch):
    monkeypatch.delenv("SPACE_ID", raising=False)
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "operator.sqlite3")
    tokens = {role: secrets.token_urlsafe(32) for role in ("custodian", "A", "B")}
    monkeypatch.setattr(service, "WRITE_TOKEN", tokens["custodian"])
    monkeypatch.setattr(service, "REVIEWER_TOKENS", {k: tokens[k] for k in ("A", "B")})
    service.init_database()
    return TestClient(service.app, raise_server_exceptions=False), {
        key: {"Authorization": "Bearer " + value} for key, value in tokens.items()}


def sample_csv():
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["event_id", *service.FEATURES, "time"])
    writer.writeheader()
    for index in range(2):
        writer.writerow({"event_id": f"private-SAMPLE-{index}", "time": "SAMPLE",
                         **{key: service.PANEL[index][key] for key in service.FEATURES}})
    return out.getvalue()


IMPORT = "/api/catalogues/import?catalogue=private-SAMPLE-catalogue&region=japan_forearc"
WAVEFORM = {"source_uri": "sample://private-waveform", "sample_rate_hz": 10,
            "stations": [{"samples": [0, 1, 0, -1]} for _ in range(5)]}


def counts():
    with service.database() as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("detections", "reviews", "write_receipts"))


def imported(operator):
    client, auth = operator
    assert client.post(IMPORT, content=sample_csv(), headers=auth["custodian"]).status_code == 200
    return client.get("/api/custody/detections", headers=auth["custodian"]).json()["items"]


def test_role_matrix_and_malformed_auth_never_write(operator):
    client, auth = operator
    for header in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic wrong"},
                   {b"Authorization": b"Bearer \xff"}, {"Authorization": "Bearer " + "a" * 513}):
        assert client.post(IMPORT, content=sample_csv(), headers=header).status_code == 401
        assert client.post("/api/reviews", json={}, headers=header).status_code == 401
        assert client.post("/api/detections/import:missing/waveform", json=WAVEFORM, headers=header).status_code == 401
        assert client.get("/api/custody/detections", headers=header).status_code == 401
        assert client.get("/api/receipts", headers=header).status_code == 401
    assert client.post(IMPORT, content=sample_csv(), headers=auth["A"]).status_code == 401
    assert client.post("/api/reviews", json={}, headers=auth["custodian"]).status_code == 401
    assert client.get("/api/custody/detections", headers=auth["A"]).status_code == 401
    assert client.get("/api/receipts", headers=auth["B"]).status_code == 401
    assert counts() == (0, 0, 0)


def test_custody_mapping_is_private_paginated_and_survives_new_connection(operator):
    client, auth = operator
    items = imported(operator)
    assert {item["event_id"] for item in items} == {"private-SAMPLE-0", "private-SAMPLE-1"}
    assert len({item["id"] for item in items}) == 2
    assert all(item["waveform_ready"] is False for item in items)
    before = counts()
    page = client.get("/api/custody/detections?limit=1&offset=1", headers=auth["custodian"])
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert page.json()["items"] == items[1:] and page.json()["receipt_minted"] is False
    assert client.get("/api/custody/detections?q=private-SAMPLE-1", headers=auth["custodian"]).json()["items"] == [items[1]]
    assert client.get("/api/custody/detections?limit=201", headers=auth["custodian"]).status_code == 422
    assert client.get("/api/custody/detections?q=" + "x" * 101, headers=auth["custodian"]).status_code == 422
    for route in ("/api/detections?origin=imported", "/api/export?origin=imported"):
        public = client.get(route).text
        assert "private-SAMPLE" not in public and "source_sha256" not in public
    for item in items:
        blind = client.get("/api/detections/" + item["id"], headers=auth["B"]).text
        assert "private-SAMPLE" not in blind
    service.init_database()
    assert client.get("/api/custody/detections", headers=auth["custodian"]).json()["items"] == items
    assert counts() == before  # Readback, including schema reopen, mints nothing.


def test_hosted_store_gate_does_not_trust_tokens_or_a_path(operator, monkeypatch):
    client, auth = operator
    monkeypatch.setenv("SPACE_ID", "SZLHOLDINGS/szl-seismic-review")
    meta = client.get("/api/meta").json()
    assert meta["storage_state"] == "READ_ONLY"
    assert meta["import_enabled"] is False and meta["review_write_enabled"] is False
    assert meta["write_activation"]["evidence_class"] == "BLOCKED"
    assert client.post(IMPORT, content=sample_csv(), headers=auth["custodian"]).status_code == 503
    assert client.post("/api/reviews", json={}, headers=auth["A"]).status_code == 503
    assert client.post("/api/detections/import:missing/waveform", json=WAVEFORM, headers=auth["custodian"]).status_code == 503
    assert client.get("/api/custody/detections", headers=auth["custodian"]).status_code == 503
    assert counts() == (0, 0, 0)


def test_foreign_keys_apply_to_every_connection(operator):
    with service.database() as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)",
                       ("missing", "A", "confirmed", "SAMPLE", "0" * 64, service.utc_now()))
    assert counts() == (0, 0, 0)


@pytest.mark.parametrize("action", ["import", "waveform", "review"])
def test_receipt_failure_rolls_back_entity_and_receipt(operator, monkeypatch, action):
    client, auth = operator
    if action != "import":
        item = imported(operator)[0]
        if action == "review":
            assert client.post(f"/api/detections/{item['id']}/waveform", json=WAVEFORM,
                               headers=auth["custodian"]).status_code == 200
    before = counts()
    append = service.append_write_receipt

    def fail_after_insert(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("SIMULATED storage failure after receipt insert")

    monkeypatch.setattr(service, "append_write_receipt", fail_after_insert)
    if action == "import":
        result = client.post(IMPORT, content=sample_csv(), headers=auth["custodian"])
    elif action == "waveform":
        result = client.post(f"/api/detections/{item['id']}/waveform", json=WAVEFORM, headers=auth["custodian"])
        assert client.get("/api/custody/detections", headers=auth["custodian"]).json()["items"][0]["waveform_ready"] is False
    else:
        result = client.post("/api/reviews", json={"detection_id": item["id"], "verdict": "unresolved"}, headers=auth["A"])
    assert result.status_code == 500 and counts() == before


def test_duplicate_transactions_preserve_exact_receipts(operator):
    client, auth = operator
    items = imported(operator)
    assert client.post(IMPORT, content=sample_csv(), headers=auth["custodian"]).status_code == 409
    item = items[0]
    url = f"/api/detections/{item['id']}/waveform"
    assert client.post(url, json=WAVEFORM, headers=auth["A"]).status_code == 401
    assert client.post(url, json=WAVEFORM, headers=auth["custodian"]).status_code == 200
    assert client.post(url, json=WAVEFORM, headers=auth["custodian"]).status_code == 409
    review = {"detection_id": item["id"], "verdict": "unresolved", "note": "SAMPLE"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post("/api/reviews", json=review, headers=auth["A"]).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert counts() == (2, 1, 3)
    assert client.get(f"/api/detections/{item['id']}", headers=auth["A"]).json()["blind"] is False
    assert client.get(f"/api/detections/{item['id']}", headers=auth["B"]).json()["blind"] is True
    chain = client.get("/api/receipts", headers=auth["custodian"]).json()
    assert chain["count"] == 3 and chain["chain_self_consistent"] is True


@pytest.mark.parametrize("case", ["short", "unicode", "duplicate_identity", "duplicate_token", "shared_custodian", "invalid_identity"])
def test_invalid_credential_configuration_fails_without_disclosure(tmp_path, case):
    custodian, reviewer = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    mapping = {"A": reviewer}
    if case == "short": mapping["A"] = "short"
    if case == "unicode": mapping["A"] = "\u00e9" * 40
    if case == "duplicate_token": mapping["B"] = reviewer
    if case == "shared_custodian": mapping["A"] = custodian
    if case == "invalid_identity": mapping = {"A B": reviewer}
    encoded = json.dumps(mapping)
    if case == "duplicate_identity": encoded = '{"A":' + json.dumps(reviewer) + ',"A":' + json.dumps(secrets.token_urlsafe(32)) + '}'
    env = {**os.environ, "SZL_REVIEW_WRITE_TOKEN": custodian,
           "SZL_REVIEWER_TOKENS_JSON": encoded, "SZL_REVIEW_DB_PATH": str(tmp_path / "unused.sqlite3")}
    result = subprocess.run([sys.executable, "-B", "-c", "import app"], cwd=service.ROOT, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert custodian not in result.stdout + result.stderr and reviewer not in result.stdout + result.stderr
    assert not (tmp_path / "unused.sqlite3").exists()
