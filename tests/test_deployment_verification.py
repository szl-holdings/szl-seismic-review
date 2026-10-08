"""Offline fault injection for runtime verification. Provider replies are SIMULATED."""

import importlib.util
from http.client import IncompleteRead
import json
from pathlib import Path
import sys
from urllib.error import URLError

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
        self.static = {probe.HOST + path: (probe.ROOT / file).read_bytes() for path, file in probe.STATIC.items()}
        self.hub_reads = 0
        self.move_on_last_read = False

    def read(self, url):
        return self.static[url], {}

    def json(self, url):
        if url == probe.HUB:
            self.hub_reads += 1
            if self.move_on_last_read and self.hub_reads == 2:
                self.responses[probe.HUB]["sha"] = "c" * 40
        return self.responses[url.removeprefix(probe.HOST)]


def test_complete_observations_require_stable_source_and_payload():
    reader, report = FakeReader(), {"checks": []}
    probe.verify(SHA, reader, report)
    assert reader.hub_reads == 2
    assert len(report["checks"]) == 9
    assert report["hub_revision"] == "b" * 40


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
