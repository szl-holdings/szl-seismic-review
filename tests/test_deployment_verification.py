"""Offline fault injection for runtime verification. Provider replies are SIMULATED."""

import importlib.util
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError

import pytest


spec = importlib.util.spec_from_file_location("deployment_verification", Path(__file__).parents[1] / "scripts/verify_deployment.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
SHA = "a" * 40


class FakeReader:
    def __init__(self):
        model_bytes = (probe.ROOT / "models/japan_assoc_logistic_v1.json").read_bytes()
        model = json.loads(model_bytes)
        receipt_bytes = (probe.ROOT / "models/receipts/train-0001.json").read_bytes()
        receipt = json.loads(receipt_bytes)
        manifest = json.loads((probe.ROOT / "data/source_manifest.json").read_bytes())
        build = {"build": {"revision": SHA}, "evidence_class": "DECLARED", "receipt_minted": False,
                 "source_repository": probe.REPOSITORY}
        self.responses = {
            probe.HUB: {"id": probe.SPACE, "host": probe.HOST, "sha": "b" * 40,
                        "runtime": {"sha": "b" * 40, "stage": "RUNNING"}},
            "/api/build-info": build,
            "/api/evidence": {"schema": "szl.seismic.evidence/v1", "receipt_minted": False,
                              "source": {"files_sha256": manifest["derived_sha256"]},
                              "model": {"sha256": probe.digest(model_bytes), "evaluation": model["evaluation"],
                                        "evidence_class": model["evidence_class"], "independent_replay": "UNAVAILABLE"},
                              "training_receipt": {"sha256": probe.digest(receipt_bytes),
                                                   "chain_head_sha256": receipt["receipt_sha256"],
                                                   "signature_status": receipt["signature_status"]},
                              "limits": {"new_region_validation": "UNAVAILABLE"}, "runtime": build},
            "/api/summary": {"published_count": 200, "imported_count": 0, **model["class_counts"]},
            "/api/detections?q=j001": {"total": 1, "items": [{"id": "published:J001"}]},
            "/api/export?consensus=confirmed&limit=2": {
                "total": 139, "count": 2, "truncated": True, "next_offset": 2, "receipt_minted": False,
                "items": [{"published_consensus": "confirmed"}, {"published_consensus": "confirmed"}]},
            "/api/meta": {"import_enabled": False, "review_write_enabled": False, "storage_state": "READ_ONLY"},
        }
        self.static = {
            probe.HOST + path: probe.source_static_bytes(probe.ROOT, file)
            for path, file in probe.STATIC.items()
        }
        self.hub_reads = 0
        self.move_on_last_read = False
        self.denied_reads = []

    def denied(self, url):
        self.denied_reads.append(url)

    def read(self, url):
        return self.static[url], {}

    def json(self, url):
        if url == probe.HUB:
            self.hub_reads += 1
            if self.move_on_last_read and self.hub_reads == 2:
                self.responses[probe.HUB]["sha"] = "c" * 40
        return self.responses[url.removeprefix(probe.HOST)]


def active_reader():
    reader = FakeReader()
    reader.responses["/api/summary"]["imported_count"] = 1
    reader.responses["/api/meta"] = {
        "import_enabled": True, "review_write_enabled": True, "storage_state": "REMOTE_COMMIT_BEFORE_ACK",
        "write_activation": {"evidence_class": "DECLARED", "mode": "active", "backend": probe.ACTIVE_BACKEND},
    }
    reader.responses["/api/storage/status"] = {"backend": probe.ACTIVE_BACKEND, "state": "READY",
                                             "evidence_class": "DECLARED", "receipt_minted": False}
    reader.responses["/api/detections?q=j001&origin=published"] = reader.responses["/api/detections?q=j001"]
    reader.responses["/api/export?consensus=confirmed&limit=2&origin=published"] = reader.responses["/api/export?consensus=confirmed&limit=2"]
    item = {"id": "import:" + "d" * 64, "event_id": None, "catalogue": "Blind import",
            "region": "Undisclosed during review", "origin": "OPERATOR_IMPORT", "published_consensus": None,
            "review_count": None, "waveform_ready": True, "score_status": "BLIND_REVIEW_PENDING",
            "confirmability_score": None}
    reader.responses["/api/detections?origin=imported&limit=200&offset=0"] = {"total": 1, "offset": 0, "items": [item]}
    reader.responses["/api/export?origin=imported&limit=200&offset=0"] = {
        "total": 1, "count": 1, "offset": 0, "items": [item.copy()], "truncated": False,
        "next_offset": None, "receipt_minted": False, "query": {"origin": "imported"},
    }
    return reader


def test_active_mode_keeps_private_cards_blind_and_checks_anonymous_denial():
    reader, report = active_reader(), {"checks": []}
    probe.verify(SHA, reader, report, expected_mode="active")
    assert report["expected_mode"] == "active" and reader.hub_reads == 2
    assert reader.denied_reads == [probe.HOST + "/api/custody/detections", probe.HOST + "/api/receipts",
                                   probe.HOST + "/api/detections/import%3A" + "d" * 64]


def test_active_service_never_passes_the_default_read_only_check():
    with pytest.raises(probe.VerificationError, match="Public panel inventory mismatch"):
        probe.verify(SHA, active_reader(), {"checks": []})


def test_invalid_expected_mode_is_rejected_before_network_access():
    reader = FakeReader()
    with pytest.raises(probe.VerificationError, match="Expected mode"):
        probe.verify(SHA, reader, {"checks": []}, expected_mode="auto")
    assert reader.hub_reads == 0


@pytest.mark.parametrize("section,key,value", [
    ("/api/meta", "import_enabled", False), ("/api/meta", "review_write_enabled", False),
    ("/api/meta", "storage_state", "LOCAL_FILE_UNVERIFIED_DURABILITY"),
    ("/api/meta", "write_activation", {"evidence_class": "MEASURED", "mode": "active", "backend": probe.ACTIVE_BACKEND}),
    ("/api/storage/status", "backend", "OTHER"), ("/api/storage/status", "state", "UNAVAILABLE"),
    ("/api/storage/status", "receipt_minted", True), ("/api/storage/status", "evidence_class", "MEASURED"),
])
def test_active_mode_requires_the_explicit_storage_contract(section, key, value):
    reader = active_reader()
    reader.responses[section][key] = value
    with pytest.raises(probe.VerificationError, match="Active (write configuration|storage readiness) mismatch"):
        probe.verify(SHA, reader, {"checks": []}, expected_mode="active")


@pytest.mark.parametrize("route", ["/api/detections?origin=imported&limit=200&offset=0",
                                   "/api/export?origin=imported&limit=200&offset=0"])
@pytest.mark.parametrize("key,value", [("event_id", "private-source-event"), ("reviews", []),
                                      ("source_sha256", "secret"), ("review_count", 1),
                                      ("id", "import:private-source-event"), ("confirmability_score", 0.5)])
def test_public_import_disclosure_blocks_active_verification(route, key, value):
    reader = active_reader()
    reader.responses[route]["items"][0][key] = value
    with pytest.raises(probe.VerificationError, match="Public import"):
        probe.verify(SHA, reader, {"checks": []}, expected_mode="active")


def test_active_mode_does_not_require_an_import_before_activation():
    reader = active_reader()
    reader.responses["/api/summary"]["imported_count"] = 0
    for route in ("/api/detections?origin=imported&limit=200&offset=0", "/api/export?origin=imported&limit=200&offset=0"):
        reader.responses[route].update(total=0, count=0, items=[])
    probe.verify(SHA, reader, {"checks": []}, expected_mode="active")
    assert len(reader.denied_reads) == 2


def test_anonymous_get_denial_uses_no_credentials_and_requires_explicit_401(monkeypatch):
    reader = probe.PublicReader()
    def denied(request, timeout):
        assert request.get_method() == "GET" and timeout == 20
        assert not request.has_header("Authorization") and not request.has_header("Cookie")
        raise HTTPError(request.full_url, 401, "SIMULATED", {"Cache-Control": "no-store"},
                        io.BytesIO(b'{"detail":"operator token required"}'))
    monkeypatch.setattr(reader.opener, "open", denied)
    reader.denied(probe.HOST + "/api/receipts")


@pytest.mark.parametrize("status,body,cache", [
    (200, b'{"detail":"operator token required"}', "no-store"),
    (503, b'{"detail":"operator token required"}', "no-store"),
    (302, b'{"detail":"operator token required"}', "no-store"),
    (401, b'{"detail":"operator token required","records":[]}', "no-store"),
    (401, b'{"detail":"operator token required"}', "public"),
    (401, b"x" * 4097, "no-store"),
])
def test_unexpected_anonymous_read_responses_fail_closed(monkeypatch, status, body, cache):
    reader = probe.PublicReader()
    def denied(request, timeout):
        raise HTTPError(request.full_url, status, "SIMULATED", {"Cache-Control": cache}, io.BytesIO(body))
    monkeypatch.setattr(reader.opener, "open", denied)
    with pytest.raises(probe.VerificationError):
        reader.denied(probe.HOST + "/api/receipts")


def test_complete_observations_require_stable_source_and_payload():
    reader, report = FakeReader(), {"checks": []}
    probe.verify(SHA, reader, report)
    assert reader.hub_reads == 2
    assert len(report["checks"]) == 9
    assert report["hub_revision"] == "b" * 40


def test_static_source_normalization_preserves_lone_carriage_returns(tmp_path):
    asset = tmp_path / "app.js"
    asset.write_bytes(b"first\r\nsecond\rliteral\n")
    assert probe.source_static_bytes(tmp_path, "app.js") == b"first\nsecond\rliteral\n"


def test_crlf_checkout_static_sources_match_deployed_lf_bytes(tmp_path):
    root = tmp_path / "crlf-checkout"
    needed = [
        "models/japan_assoc_logistic_v1.json",
        "models/receipts/train-0001.json",
        "data/source_manifest.json",
        "data/japan_panel.csv",
        *probe.STATIC.values(),
    ]
    for relative in needed:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (probe.ROOT / relative).read_bytes()
        if relative in probe.STATIC.values():
            data = probe.canonical_text_bytes(data).replace(b"\n", b"\r\n")
        target.write_bytes(data)

    reader = FakeReader()
    reader.static = {
        probe.HOST + route: probe.source_static_bytes(probe.ROOT, path)
        for route, path in probe.STATIC.items()
    }
    report = {"checks": []}
    probe.verify(SHA, reader, report, root=root)
    assert report["checks"][2]["route"] == "/"
    assert report["checks"][2]["sha256"] == probe.digest(reader.static[probe.HOST + "/"])


@pytest.mark.parametrize("fault,match", [
    ("source", "Runtime source mismatch"), ("runtime", "Running revision differs"),
    ("host", "Unexpected Space identity"), ("static", "Static source mismatch"),
    ("model", "Model bytes mismatch"), ("receipt", "Training receipt mismatch"),
    ("writes", "Public write configuration mismatch"), ("export", "Export filter mismatch"),
    ("changed", "Space changed"), ("minted", "read semantics mismatch"),
])
def test_mismatch_blocks_verification(fault, match):
    reader = FakeReader()
    if fault == "source":
        reader.responses["/api/build-info"]["build"]["revision"] = "c" * 40
    elif fault == "runtime":
        reader.responses[probe.HUB]["runtime"]["sha"] = "c" * 40
    elif fault == "host":
        reader.responses[probe.HUB]["host"] = "https://example.invalid"
    elif fault == "static":
        reader.static[probe.HOST + "/assets/app.js"] = b"stale"
    elif fault == "model":
        reader.responses["/api/evidence"]["model"]["sha256"] = "0" * 64
    elif fault == "receipt":
        reader.responses["/api/evidence"]["training_receipt"]["signature_status"] = "SIGNED"
    elif fault == "writes":
        reader.responses["/api/meta"]["review_write_enabled"] = True
    elif fault == "export":
        reader.responses["/api/export?consensus=confirmed&limit=2"]["items"][0]["published_consensus"] = "rejected"
    elif fault == "changed":
        reader.move_on_last_read = True
    elif fault == "minted":
        reader.responses["/api/evidence"]["receipt_minted"] = True
    with pytest.raises(probe.VerificationError, match=match):
        probe.verify(SHA, reader, {"checks": []})


def test_public_reader_rejects_other_targets_without_requesting_them():
    with pytest.raises(probe.VerificationError, match="Unexpected read target"):
        probe.PublicReader().read("https://example.invalid/api/evidence")


def test_public_reader_refuses_cacheable_api_and_malformed_json(monkeypatch):
    reader = probe.PublicReader()
    monkeypatch.setattr(reader, "read", lambda _: (b"{}", {"cache-control": "public"}))
    with pytest.raises(probe.VerificationError, match="cache policy"):
        reader.json(probe.HOST + "/api/evidence")
    monkeypatch.setattr(reader, "read", lambda _: (b"<html>", {"cache-control": "no-store"}))
    with pytest.raises(probe.VerificationError, match="valid JSON"):
        reader.json(probe.HOST + "/api/evidence")


def test_reader_is_bounded_get_without_credentials_and_preserves_network_failure(monkeypatch):
    reader = probe.PublicReader()

    def unavailable(request, *, timeout):
        assert request.get_method() == "GET"
        assert not request.has_header("Authorization")
        assert timeout == 20
        raise URLError("simulated DNS failure")

    monkeypatch.setattr(reader.opener, "open", unavailable)
    with pytest.raises(probe.VerificationError, match="unavailable from this runner"):
        reader.read(probe.HUB)


def test_truncated_response_is_an_unavailable_observation(monkeypatch):
    reader = probe.PublicReader()

    def truncated(*args, **kwargs):
        raise IncompleteRead(b"partial", 100)

    monkeypatch.setattr(reader.opener, "open", truncated)
    with pytest.raises(probe.VerificationError, match="unavailable from this runner"):
        reader.read(probe.HUB)


def test_malformed_runtime_retains_blocked_report(tmp_path, monkeypatch):
    reader = FakeReader()
    reader.responses[probe.HUB]["runtime"] = None
    output = tmp_path / "runtime.json"
    monkeypatch.setattr(probe, "PublicReader", lambda: reader)
    monkeypatch.setattr(sys, "argv", ["verify_deployment.py", "--sha", SHA, "--output", str(output)])
    assert probe.main() == 1
    result = json.loads(output.read_bytes())
    assert result["passed"] is False and result["evidence_class"] == "BLOCKED"
    assert result["error"] and not result["checks"]
