"""Integration checks for provenance and the blind-review boundary."""

import csv
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as service  # noqa: E402


def first_reference_row():
    with (ROOT / "data/japan_panel.csv").open(newline="", encoding="utf-8") as handle:
        return next(csv.DictReader(handle))


def test_published_fixture_and_model_scope():
    client = TestClient(service.app)
    health = client.get("/healthz").json()
    assert health["fixture_verified"] is True
    assert health["training_receipt_chain_self_consistent"] is True
    assert health["training_receipt_signature_status"] == "UNSIGNED"
    summary = client.get("/api/summary").json()
    assert summary["published_count"] == 200
    assert summary["confirmed"] + summary["rejected"] + summary["unresolved"] == 200
    row = first_reference_row()
    features = {name: row[name] for name in service.FEATURES}
    other = client.post("/api/score", json={"region": "ridgecrest", "features": features}).json()
    assert other["score_status"] == "UNQUALIFIED_REGION"
    assert other["confirmability_score"] is None
    local = client.post("/api/score", json={"region": "japan_forearc", "features": features}).json()
    assert local["score_status"] == "UNVALIDATED_QUERY"
    assert 0 < local["confirmability_score"] < 1
    assert client.post("/api/score", content=b"x" * 16_385).status_code == 413
    docs = client.get("/api/docs")
    assert docs.status_code == 200
    directives = {
        parts[0]: set(parts[1:])
        for directive in docs.headers["content-security-policy"].split(";")
        if (parts := directive.split())
    }
    assert "https://cdn.jsdelivr.net" in directives["script-src"]
    assert "https://cdn.jsdelivr.net" in directives["style-src"]


def test_runtime_source_binding_requires_file_and_environment_agreement(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "ROOT", tmp_path)
    revision = "a" * 40
    monkeypatch.setenv("SZL_SOURCE_REVISION", revision)
    client = TestClient(service.app)
    assert client.get("/api/build-info").json()["build"]["state"] == "UNKNOWN"
    (tmp_path / "SOURCE_REVISION").write_text(revision + "\n", encoding="ascii")
    binding = client.get("/api/build-info").json()
    assert binding["build"] == {"state": "OBSERVED", "revision": revision}
    assert binding["evidence_class"] == "DECLARED"
    assert binding["receipt_minted"] is False
    monkeypatch.setenv("SZL_SOURCE_REVISION", "b" * 40)
    assert client.get("/api/build-info").json()["build"]["revision"] is None
    (tmp_path / "SOURCE_REVISION").write_bytes(b"\xff")
    assert client.get("/api/build-info").json()["build"]["state"] == "UNKNOWN"


def test_import_requires_auth_and_waveform_review_stays_blind(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "reviews.sqlite3")
    monkeypatch.setattr(service, "WRITE_TOKEN", "local-test-token")
    monkeypatch.setattr(service, "REVIEWER_TOKENS", {"A": "reviewer-a-token", "B": "reviewer-b-token"})
    service.init_database()
    client = TestClient(service.app)
    row = first_reference_row()
    columns = ["event_id", *service.FEATURES, "time", "lat", "lon", "dep"]
    secret_id = "hidden-catalogue-event-123"
    row["event_id"] = secret_id
    csv_text = ",".join(columns) + "\n" + ",".join(row.get(key, "") for key in columns) + "\n"
    url = "/api/catalogues/import?catalogue=test-pipeline&region=japan_forearc"
    assert client.post(url, content=csv_text).status_code == 401
    auth = {"Authorization": "Bearer local-test-token"}
    reviewer_a = {"Authorization": "Bearer reviewer-a-token"}
    reviewer_b = {"Authorization": "Bearer reviewer-b-token"}
    imported = client.post(url, content=csv_text, headers=auth)
    assert imported.status_code == 200
    assert imported.json()["receipt"]["signature_status"] == "UNSIGNED"
    assert client.get("/api/detections?catalogue=test-pipeline").json()["total"] == 0
    assert client.get("/api/detections?origin=published").json()["total"] == 200
    assert client.get("/api/detections?origin=imported").json()["total"] == 1
    item = next(row for row in client.get("/api/detections?limit=200").json()["items"]
                if row["origin"] == "OPERATOR_IMPORT")
    identifier = item["id"]
    assert secret_id not in identifier
    assert item["event_id"] is None
    assert item["catalogue"] == "Blind import"
    assert client.get(f"/api/detections/{identifier}").status_code == 401
    blind = client.get(f"/api/detections/{identifier}", headers=reviewer_a).json()
    assert "features" not in blind and "metadata" not in blind
    review = {"detection_id": identifier, "verdict": "unresolved", "note": "local synthetic gate test"}
    assert client.post("/api/reviews", json=review, headers=auth).status_code == 401
    assert client.post("/api/reviews", json={**review, "reviewer_id": "B"}, headers=reviewer_a).status_code == 422
    assert client.post("/api/reviews", json=review, headers=reviewer_a).status_code == 409
    waveform = {
        "source_uri": "local-test://secret-source-event-123",
        "sample_rate_hz": 100,
        "stations": [{"samples": [0, 1, 0, -1], "p_offset_s": 0.01, "s_offset_s": 0.03} for _ in range(5)],
    }
    assert client.post(f"/api/detections/{identifier}/waveform", json=waveform, headers=auth).status_code == 200
    blind = client.get(f"/api/detections/{identifier}", headers=reviewer_a).json()
    assert "source_uri" not in blind["waveform"]
    assert "features" not in blind and "metadata" not in blind
    saved = client.post("/api/reviews", json=review, headers=reviewer_a)
    assert saved.status_code == 200 and saved.json()["reviewer_id"] == "A"
    assert client.post("/api/reviews", json=review, headers=reviewer_a).status_code == 409
    still_blind = client.get(f"/api/detections/{identifier}", headers=reviewer_b).json()
    assert still_blind["blind"] is True and "features" not in still_blind
    operator_blind = client.get(f"/api/detections/{identifier}", headers=auth).json()
    assert operator_blind["blind"] is True
    revealed = client.get(f"/api/detections/{identifier}", headers=reviewer_a).json()
    assert revealed["blind"] is False
    assert revealed["event_id"] == secret_id
    assert revealed["waveform"]["source_uri"] == waveform["source_uri"]
    assert revealed["score_status"] == "UNVALIDATED_IMPORT"
    assert client.get("/api/receipts").status_code == 401
    receipts = client.get("/api/receipts", headers=auth).json()
    assert receipts["count"] == 3
    assert receipts["chain_self_consistent"] is True
    assert receipts["signature_status"] == "UNSIGNED"
