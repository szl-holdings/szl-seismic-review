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
    assert client.get("/healthz").json()["fixture_verified"] is True
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


def test_import_requires_auth_and_waveform_review_stays_blind(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "reviews.sqlite3")
    monkeypatch.setattr(service, "WRITE_TOKEN", "local-test-token")
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
    assert client.post(url, content=csv_text, headers=auth).status_code == 200
    item = client.get("/api/detections?catalogue=test-pipeline", headers=auth).json()["items"][0]
    identifier = item["id"]
    assert secret_id not in identifier
    assert item["event_id"] is None
    assert item["catalogue"] == "Blind import"
    assert client.get(f"/api/detections/{identifier}").status_code == 401
    blind = client.get(f"/api/detections/{identifier}", headers=auth).json()
    assert "features" not in blind and "metadata" not in blind
    assert client.post("/api/reviews", json={"detection_id": identifier, "reviewer_id": "A", "verdict": "unresolved"}, headers=auth).status_code == 409
    waveform = {
        "source_uri": "local-test://secret-source-event-123",
        "sample_rate_hz": 100,
        "stations": [{"samples": [0, 1, 0, -1], "p_offset_s": 0.01, "s_offset_s": 0.03} for _ in range(5)],
    }
    assert client.post(f"/api/detections/{identifier}/waveform", json=waveform, headers=auth).status_code == 200
    blind = client.get(f"/api/detections/{identifier}", headers=auth).json()
    assert "source_uri" not in blind["waveform"]
    assert "features" not in blind and "metadata" not in blind
    review = {"detection_id": identifier, "reviewer_id": "A", "verdict": "unresolved", "note": "local synthetic gate test"}
    assert client.post("/api/reviews", json=review, headers=auth).status_code == 200
    assert client.post("/api/reviews", json=review, headers=auth).status_code == 409
    still_blind = client.get(f"/api/detections/{identifier}?reviewer_id=B", headers=auth).json()
    assert still_blind["blind"] is True and "features" not in still_blind
    revealed = client.get(f"/api/detections/{identifier}?reviewer_id=A", headers=auth).json()
    assert revealed["blind"] is False
    assert revealed["event_id"] == secret_id
    assert revealed["waveform"]["source_uri"] == waveform["source_uri"]
    assert revealed["score_status"] == "UNVALIDATED_IMPORT"
