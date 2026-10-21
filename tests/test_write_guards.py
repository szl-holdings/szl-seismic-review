"""Offline transport checks; all responses here are SIMULATED."""

import io
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_write_guards as guards
from verify_deployment import VerificationError


class Response(io.BytesIO):
    def __init__(self, status=503, body=b'{"detail":"hosted writes blocked: durable review storage is not qualified"}', cache="no-store"):
        super().__init__(body)
        self.code = status
        self.headers = {"Cache-Control": cache}


def test_all_probes_use_fixed_target_no_credentials_and_empty_invalid_bodies(monkeypatch):
    reader = guards.PublicReader()
    requests = []

    def response(request, timeout):
        requests.append(request)
        assert request.full_url.startswith(guards.HOST + "/api/") and timeout == 20
        assert not request.has_header("Authorization") and not request.has_header("Cookie")
        assert request.data in (None, b"", b"{}")
        raise HTTPError(request.full_url, 503, "SIMULATED", {"Cache-Control": "no-store"},
                        io.BytesIO(b'{"detail":"hosted writes blocked: durable review storage is not qualified"}'))

    monkeypatch.setattr(reader.opener, "open", response)
    assert all(guards.probe(reader, *item)["passed"] for item in guards.PROBES)
    assert len(requests) == 5
    with pytest.raises(VerificationError, match="Unexpected access probe"):
        guards.probe(reader, "POST", "/api/score")
    assert len(requests) == 5


@pytest.mark.parametrize("response", [Response(200), Response(401), Response(302),
                                    Response(body=b"x" * 4097), Response(body=b"{}"),
                                    Response(cache="public")])
def test_fail_open_or_unexpected_denial_never_passes(monkeypatch, response):
    reader = guards.PublicReader()
    monkeypatch.setattr(reader.opener, "open", lambda *a, **k: response)
    with pytest.raises(VerificationError):
        guards.probe(reader, *guards.PROBES[0])


def test_network_failure_is_not_an_access_denial(monkeypatch):
    reader = guards.PublicReader()

    def unavailable(*args, **kwargs):
        raise URLError("SIMULATED network failure")

    monkeypatch.setattr(reader.opener, "open", unavailable)
    with pytest.raises(URLError):
        guards.probe(reader, *guards.PROBES[0])
