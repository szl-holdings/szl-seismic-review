"""Public discovery/export must not reveal imported review evidence."""

import csv
import hashlib
import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "catalogue.sqlite3")
    monkeypatch.setattr(service, "WRITE_TOKEN", "test-custodian")
    monkeypatch.setattr(service, "REVIEWER_TOKENS", {"reviewer": "test-reviewer"})
    service.init_database()
    return TestClient(service.app)


def import_case(client, suffix):
    row = service.PANEL[0]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["event_id", *service.FEATURES])
    writer.writeheader()
    writer.writerow({"event_id": f"private-event-{suffix}", **{key: row[key] for key in service.FEATURES}})
    response = client.post("/api/catalogues/import?catalogue=private-pipeline&region=japan_forearc",
                           content=output.getvalue(), headers={"Authorization": "Bearer test-custodian"})
    assert response.status_code == 200


def test_search_verdict_and_bounded_export_use_the_same_public_view(client):
    result = client.get("/api/detections", params={"q": "  j001  "}).json()
    assert result["total"] == 1
    assert result["items"][0]["id"] == "published:J001"
    result = client.get("/api/detections", params={"consensus": "confirmed", "limit": 2}).json()
    assert result["total"] == 139
    assert all(item["published_consensus"] == "confirmed" for item in result["items"])
    exported = client.get("/api/export", params={"consensus": "confirmed", "limit": 2})
    data = exported.json()
    assert data["items"] == result["items"]
    assert data["count"] == 2 and data["total"] == 139
    assert data["truncated"] is True and data["next_offset"] == 2
    assert exported.headers["content-disposition"].startswith("attachment;")
    assert exported.headers["cache-control"] == "no-store"
    last = client.get("/api/export", params={"consensus": "confirmed", "offset": 138}).json()
    assert last["count"] == 1 and last["next_offset"] is None and last["truncated"] is False
    assert client.get("/api/detections", params={"q": "x" * 101}).status_code == 422
    assert client.get("/api/export", params={"limit": 10001}).status_code == 422
    assert client.get("/api/detections", params={"consensus": "arbitrary"}).status_code == 422


def test_import_search_and_export_stay_blind_after_private_reveal(client):
    import_case(client, "a")
    case = client.get("/api/detections", params={"origin": "imported"}).json()["items"][0]
    identifier = case["id"]
    waveform = {"source_uri": "sample://private-waveform", "sample_rate_hz": 100,
                "stations": [{"samples": [0, 1]} for _ in range(5)]}
    assert client.post(f"/api/detections/{identifier}/waveform", json=waveform,
                       headers={"Authorization": "Bearer test-custodian"}).status_code == 200
    assert client.post("/api/reviews", json={"detection_id": identifier, "verdict": "confirmed"},
                       headers={"Authorization": "Bearer test-reviewer"}).status_code == 200
    with service.database() as db:
        before = db.execute("SELECT COUNT(*) FROM write_receipts").fetchone()[0]
    for endpoint in ("/api/detections", "/api/export"):
        for hidden in ("private-event-a", "private-pipeline", "private-waveform"):
            assert client.get(endpoint, params={"q": hidden}).json()["total"] == 0
        visible = client.get(endpoint, params={"q": identifier}).json()
        assert visible["total"] == 1
        assert visible["items"][0]["event_id"] is None
        assert visible["items"][0]["confirmability_score"] is None
        assert "private-" not in str(visible)
        assert client.get(endpoint, params={"origin": "imported", "consensus": "confirmed"}).json()["total"] == 0
        assert client.get(endpoint, params={"catalogue": "private-pipeline"}).json()["total"] == 0
    with service.database() as db:
        assert db.execute("SELECT COUNT(*) FROM write_receipts").fetchone()[0] == before


def test_pagination_crosses_import_and_published_boundary_without_duplicates(client):
    import_case(client, "a")
    import_case(client, "b")
    first = client.get("/api/detections", params={"limit": 3}).json()
    assert first["total"] == 202
    assert [item["origin"] for item in first["items"]] == ["OPERATOR_IMPORT", "OPERATOR_IMPORT", "PUBLISHED_PANEL"]
    second = client.get("/api/detections", params={"limit": 3, "offset": 3}).json()
    assert second["items"][0]["id"] == "published:J002"
    assert not ({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
    assert client.get("/api/detections", params={"offset": 202}).json()["items"] == []


def test_evidence_hashes_identify_loaded_artifacts_without_promoting_science(client, tmp_path, monkeypatch):
    expected = hashlib.sha256(service.MODEL_BYTES).hexdigest()
    replacement = tmp_path / "changed-model.json"
    replacement.write_text("{}")
    monkeypatch.setattr(service, "MODEL_PATH", replacement)
    monkeypatch.setattr(service, "ROOT", tmp_path)
    response = client.get("/api/evidence")
    data = response.json()
    assert data["model"]["sha256"] == expected
    assert data["model"]["evidence_class"] == "REPORTED"
    assert data["model"]["training_cases"] == 164
    assert data["model"]["independent_replay"] == "UNAVAILABLE"
    assert data["source"]["files_sha256"] == service.MANIFEST["derived_sha256"]
    assert data["training_receipt"]["signature_status"] == "UNSIGNED"
    assert data["runtime"]["build"]["state"] == "UNKNOWN"
    assert data["receipt_minted"] is False
    assert response.headers["cache-control"] == "no-store"
