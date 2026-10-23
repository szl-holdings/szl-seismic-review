"""Actual local SAMPLE transactions; provider identity and transport are SIMULATED.

No request in this module reaches Hugging Face. The adapter exercises the real
application API, while synthetic provider replies test the live harness gates.
"""

import copy
import io
import json
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import app as service
import verify_live_operator as probe


SHA = "a" * 40
REVISION = "b" * 40


class Response(io.BytesIO):
    def __init__(self, url, value=None, *, raw=None, status=200, cache="no-store"):
        super().__init__(json.dumps(value).encode() if raw is None else raw)
        self.status = self.code = status
        self.url = url
        self.headers = {"Cache-Control": cache}

    def geturl(self):
        return self.url


class LocalServiceAdapter:
    """Redirect no network traffic: call ASGI locally and fake only provider state."""
    def __init__(self, app):
        self.app = app
        self.requests = []
        self.overrides = {}
        self.process_boot_id = "0" * 32

    def open(self, request, timeout):
        assert timeout == 30
        self.requests.append(request)
        path = urlsplit(request.full_url).path
        if request.full_url == probe.HUB:
            value = {"id": probe.SPACE, "host": probe.HOST, "sha": REVISION,
                     "runtime": {"stage": "RUNNING", "sha": REVISION}}
        elif path == "/api/build-info":
            value = {"build": {"revision": SHA}, "source_repository": probe.REPOSITORY,
                     "evidence_class": "DECLARED", "receipt_minted": False}
        elif path == "/api/meta":
            value = {"import_enabled": True, "review_write_enabled": True,
                     "storage_state": "REMOTE_COMMIT_BEFORE_ACK", "write_activation": {
                         "mode": "active", "backend": probe.BACKEND, "evidence_class": "DECLARED"}}
        elif path == "/api/storage/status":
            value = {"backend": probe.BACKEND, "state": "READY", "evidence_class": "DECLARED",
                     "receipt_minted": False, "process_boot_id": self.process_boot_id}
        else:
            local = self.app.request(request.method, request.full_url.removeprefix(probe.HOST),
                                     content=request.data, headers=dict(request.header_items()))
            value = local.json()
            if path in self.overrides:
                value = self.overrides[path](value)
            return Response(request.full_url, value, status=local.status_code,
                            cache=local.headers.get("cache-control"))
        if path in self.overrides:
            value = self.overrides[path](value)
        return Response(request.full_url, value)


@pytest.fixture
def local(tmp_path, monkeypatch):
    environ = {"SZL_REVIEW_WRITE_TOKEN": secrets.token_urlsafe(32),
               "SZL_REVIEW_CUSTODIAN_ID": "sample-custodian-service",
               "SZL_REVIEWER_TOKENS_JSON": json.dumps({
                   "sample-reviewer-a-service": secrets.token_urlsafe(32),
                   "sample-reviewer-b-service": secrets.token_urlsafe(32)})}
    credentials = probe.Credentials(environ)
    monkeypatch.delenv("SPACE_ID", raising=False)
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "local-only.sqlite3")
    monkeypatch.setattr(service, "REMOTE_STORE", None)
    monkeypatch.setattr(service, "WRITE_TOKEN", credentials.operator)
    monkeypatch.setattr(service, "CUSTODIAN_ID", credentials.custodian)
    monkeypatch.setattr(service, "REVIEWER_TOKENS", credentials.reviewers)
    service.init_database()
    report = {"requests": [], "checks": []}
    adapter = LocalServiceAdapter(TestClient(service.app, raise_server_exceptions=False))
    return environ, credentials, report, adapter


def client_for(local, *, read_only=False):
    _, credentials, report, adapter = local
    return probe.LiveClient(credentials, report, adapter, read_only=read_only)


def test_real_local_write_and_get_only_readback_preserve_existing_receipts(local):
    _, credentials, report, adapter = local
    client = client_for(local)
    # This prior catalogue is deliberately outside the acceptance run. The
    # harness must preserve it and append exactly five receipts after it.
    catalogue, csv_body, _, _ = probe.sample_inputs("0" * 32)
    client.request("POST", "/api/catalogues/import?catalogue=" + catalogue + "&region=japan_forearc",
                   token=credentials.operator, body=csv_body)
    baseline = probe.receipts(client)
    states = []
    probe.verify_write(SHA, client, report, lambda state: states.append(copy.deepcopy(state)))
    state = states[-1]
    assert state["complete"] is True and len(states) == 7
    assert state["baseline"] == {"count": 1, "head_sha256": baseline["head_sha256"]}
    assert state["final"]["count"] == 6 and len(state["receipts"]) == 5
    assert [r["event_kind"] for r in state["receipts"]] == [
        "CATALOGUE_IMPORTED", "WAVEFORM_ATTACHED", "WAVEFORM_ATTACHED", "VERDICT_LOCKED", "VERDICT_LOCKED"]
    assert state["cases"][0]["waveform_sha256"] != state["cases"][1]["waveform_sha256"]
    before = len(adapter.requests)
    # A fresh TestClient and SQLite connection reopen the stored local state.
    adapter.app.close()
    adapter.app = TestClient(service.app, raise_server_exceptions=False)
    adapter.process_boot_id = "1" * 32  # Provider restart identity is SIMULATED in this offline test.
    probe.verify_readback(SHA, client_for(local, read_only=True), report, state)
    assert all(request.method == "GET" for request in adapter.requests[before:])
    final = probe.receipts(client)
    assert final["entries"][0] == baseline["entries"][0]
    assert final["count"] == 6
    assert all(not request.has_header("Cookie") for request in adapter.requests)
    assert all(not request.has_header("Authorization") for request in adapter.requests
               if request.full_url == probe.HUB)
    rendered = json.dumps([states, report])
    assert all(secret not in rendered for secret in credentials.secrets)


@pytest.mark.parametrize("path,key,value", [
    ("/api/build-info", "build", {"revision": "c" * 40}),
    ("/api/build-info", "receipt_minted", True),
    ("/api/build-info", "source_repository", "https://example.invalid/repo"),
    ("/api/spaces/" + probe.SPACE, "sha", "c" * 40),
    ("/api/meta", "import_enabled", False),
    ("/api/meta", "write_activation", {"mode": "read-only"}),
    ("/api/storage/status", "state", "UNAVAILABLE"),
    ("/api/storage/status", "process_boot_id", None),
    ("/api/access", "identity", "different-principal"),
    ("/api/access", "role", "different-role"),
])
def test_wrong_source_storage_or_identity_stops_before_any_write(local, path, key, value):
    adapter = local[3]
    adapter.overrides[path] = lambda data: {**data, key: value}
    with pytest.raises(probe.VerificationError):
        probe.verify_write(SHA, client_for(local), local[2], lambda state: None)
    assert all(request.method == "GET" for request in adapter.requests)


@pytest.mark.parametrize("field,value", [("complete", False), ("source_sha", "c" * 40),
                                        ("host", "https://example.invalid"),
                                        ("identities", {}), ("hub_revision", "c" * 40),
                                        ("final", {"count": 99, "head_sha256": "0" * 64})])
def test_readback_rejects_state_or_revision_drift_without_writes(local, field, value):
    states = []
    probe.verify_write(SHA, client_for(local), local[2], lambda state: states.append(copy.deepcopy(state)))
    state = states[-1]
    state[field] = value
    local[3].process_boot_id = "1" * 32
    before = len(local[3].requests)
    with pytest.raises(probe.VerificationError):
        probe.verify_readback(SHA, client_for(local, read_only=True), local[2], state)
    assert all(request.method == "GET" for request in local[3].requests[before:])


def test_restart_readback_requires_a_changed_process_identity(local):
    states = []
    probe.verify_write(SHA, client_for(local), local[2], lambda state: states.append(copy.deepcopy(state)))
    before = len(local[3].requests)
    with pytest.raises(probe.VerificationError, match="different process boot identity"):
        probe.verify_readback(SHA, client_for(local, read_only=True), local[2], states[-1])
    assert all(request.method == "GET" for request in local[3].requests[before:])


@pytest.mark.parametrize("field,value", [("previous_sha256", "f" * 64), ("actor", "altered"),
                                        ("receipt_sha256", "e" * 64), ("sequence", 2)])
def test_receipt_tampering_is_rejected(local, field, value):
    credentials = local[1]
    _, body, _, _ = probe.sample_inputs("0" * 32)
    client = client_for(local)
    client.request("POST", "/api/catalogues/import?catalogue=SAMPLE&region=japan_forearc",
                   token=credentials.operator, body=body)
    def tamper(data):
        data["entries"][0][field] = value
        return data
    local[3].overrides["/api/receipts"] = tamper
    with pytest.raises(probe.VerificationError, match="Receipt chain mismatch"):
        probe.receipts(client)


@pytest.mark.parametrize("configuration", [
    {"SZL_REVIEW_WRITE_TOKEN": "short"},
    {"SZL_REVIEW_CUSTODIAN_ID": "contains spaces"},
    {"SZL_REVIEWER_TOKENS_JSON": "[]"},
    {"SZL_REVIEWER_TOKENS_JSON": '{"duplicate":"' + "a" * 32 + '","duplicate":"' + "b" * 32 + '"}'},
    {"SZL_REVIEWER_TOKENS_JSON": json.dumps({"a": "x" * 32, "b": "x" * 32})},
])
def test_credentials_fail_without_exposing_values(local, configuration):
    with pytest.raises(probe.VerificationError) as error:
        probe.Credentials({**local[0], **configuration})
    assert all(secret not in str(error.value) for secret in local[1].secrets)


class SingleReply:
    def __init__(self, callback):
        self.callback = callback
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        return self.callback(request)


@pytest.mark.parametrize("url", ["http://szlholdings-szl-seismic-review.hf.space/api/meta",
                                 "https://evil.invalid/api/meta", probe.HOST + "/api/meta#fragment",
                                 "https://szlholdings-szl-seismic-review.hf.space.evil.invalid/api/meta"])
def test_transport_rejects_noncanonical_target_before_credentials_leave(local, url):
    with pytest.raises(probe.VerificationError, match="Unexpected request target"):
        client_for(local).request("GET", url, token=local[1].operator)
    assert local[3].requests == []


def test_readback_transport_cannot_issue_any_post(local):
    with pytest.raises(probe.VerificationError, match="Readback cannot issue writes"):
        client_for(local, read_only=True).request("POST", "/api/reviews", payload={})
    assert local[3].requests == []


@pytest.mark.parametrize("status,cache,target,raw", [
    (302, "no-store", probe.HOST + "/api/meta", b"{}"),
    (200, "public", probe.HOST + "/api/meta", b"{}"),
    (200, "no-store", "https://evil.invalid", b"{}"),
    (200, "no-store", probe.HOST + "/api/meta", b"x" * (probe.MAX_BYTES + 1)),
    (200, "no-store", probe.HOST + "/api/meta", b"[]"),
    (200, "no-store", probe.HOST + "/api/meta", b"not-json"),
], ids=["redirect", "cacheable", "wrong-target", "oversized", "array", "invalid-json"])
def test_transport_fails_closed_on_untrusted_response(local, status, cache, target, raw):
    opener = SingleReply(lambda request: Response(target, status=status, cache=cache, raw=raw))
    client = probe.LiveClient(local[1], local[2], opener)
    with pytest.raises(probe.VerificationError):
        client.request("GET", "/api/meta")
    assert opener.calls == 1


def test_lost_post_ack_is_unknown_without_retry_or_secret_logging(local):
    def lost(request):
        raise URLError("SIMULATED " + local[1].operator)
    opener = SingleReply(lost)
    client = probe.LiveClient(local[1], local[2], opener)
    with pytest.raises(probe.VerificationError, match="UNKNOWN") as error:
        client.request("POST", "/api/reviews", token=local[1].operator, payload={})
    assert opener.calls == 1 and local[2]["pending_request"]["method"] == "POST"
    assert local[1].operator not in str(error.value) + json.dumps(local[2])


@pytest.mark.parametrize("status", [200, 401])
def test_credential_echo_is_rejected_even_for_expected_denial(local, status):
    opener = SingleReply(lambda request: Response(request.full_url, raw=local[1].operator.encode(), status=status))
    with pytest.raises(probe.VerificationError, match="credential material"):
        probe.LiveClient(local[1], local[2], opener).request("GET", "/api/meta", expected=status)


def test_http_error_denial_uses_the_same_bounded_no_store_validation(local):
    def denied(request):
        raise HTTPError(request.full_url, 401, "SIMULATED", {"Cache-Control": "no-store"}, io.BytesIO(b"{}"))
    opener = SingleReply(denied)
    assert probe.LiveClient(local[1], local[2], opener).request("GET", "/api/receipts", expected=401) == {}
    assert "pending_request" not in local[2] and opener.calls == 1


def test_cli_checkpoint_is_secret_free_and_repeated_write_is_refused(local, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe, "exact_checkout", lambda source: None)
    output = tmp_path / "evidence"
    args = ["--sha", SHA, "--output-dir", str(output), "--phase", "write"]
    assert probe.main(args, environ=local[0], opener=local[3]) == 0
    original = (output / "write-report.json").read_bytes()
    before = len(local[3].requests)
    assert probe.main(args, environ=local[0], opener=local[3]) == 1
    assert len(local[3].requests) == before and (output / "write-report.json").read_bytes() == original
    local[3].process_boot_id = "1" * 32
    assert probe.main(args[:-1] + ["readback"], environ=local[0], opener=local[3]) == 0
    assert all(request.method == "GET" for request in local[3].requests[before:])
    all_output = capsys.readouterr().out + "".join(path.read_text() for path in output.iterdir())
    assert all(secret not in all_output for secret in local[1].secrets)


def test_lost_ack_after_actual_local_commit_preserves_unknown_checkpoint_without_retry(local, tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "exact_checkout", lambda source: None)
    adapter = local[3]
    original_open = adapter.open
    lost = []
    def lost_after_commit(request, timeout):
        response = original_open(request, timeout)
        if request.method == "POST" and response.status == 200:
            lost.append(request.full_url)
            response.close()
            raise URLError("SIMULATED lost acknowledgement")
        return response
    monkeypatch.setattr(adapter, "open", lost_after_commit)
    output = tmp_path / "uncertain-evidence"
    args = ["--sha", SHA, "--output-dir", str(output), "--phase", "write"]
    assert probe.main(args, environ=local[0], opener=adapter) == 1
    state = json.loads((output / "expected-state.json").read_text())
    report = json.loads((output / "write-report.json").read_text())
    assert len(lost) == 1 and state["complete"] is False and state["acknowledgements"] == []
    assert report["passed"] is False and report["evidence_class"] == "BLOCKED"
    assert report["pending_request"]["method"] == "POST" and "UNKNOWN" in report["error"]
    assert probe.receipts(client_for(local))["count"] == 1
    before = len(adapter.requests)
    assert probe.main(args, environ=local[0], opener=adapter) == 1
    assert len(adapter.requests) == before


def test_unexpected_private_field_in_public_cards_fails_verification(local):
    states = []
    probe.verify_write(SHA, client_for(local), local[2], lambda state: states.append(copy.deepcopy(state)))
    catalogue, _, _, cases = probe.sample_inputs(states[-1]["run_id"])
    def leak(data):
        for item in data["items"]:
            item["private_note"] = "SAMPLE hidden source metadata"
        return data
    local[3].overrides["/api/export"] = leak
    with pytest.raises(probe.VerificationError, match="Public card exposed private evidence"):
        probe.public_redaction(client_for(local), catalogue, cases)


def test_exact_checkout_requires_same_head_clean_files_and_tracked_harness(monkeypatch):
    calls = []
    monkeypatch.setattr(probe.subprocess, "check_output", lambda *args, **kwargs: SHA + "\n")
    monkeypatch.setattr(probe.subprocess, "run", lambda args, **kwargs: calls.append(args))
    probe.exact_checkout(SHA)
    assert calls[0][:5] == ["git", "diff", "--exit-code", "HEAD", "--"]
    assert "review_store.py" in calls[0] and "scripts/verify_live_operator.py" in calls[0]
    assert calls[-1] == ["git", "cat-file", "-e", "HEAD:scripts/verify_live_operator.py"]
    with pytest.raises(probe.VerificationError, match="exact source checkout"):
        probe.exact_checkout("c" * 40)
    def dirty(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git")
    monkeypatch.setattr(probe.subprocess, "run", dirty)
    with pytest.raises(probe.VerificationError, match="committed, unchanged"):
        probe.exact_checkout(SHA)
