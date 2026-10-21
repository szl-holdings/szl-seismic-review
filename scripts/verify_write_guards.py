"""Verify deny-by-default access on the canonical read-only Space.

Only empty invalid write bodies are sent, without credentials or source data.
Positive transactions belong in the isolated local operator browser harness.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from verify_deployment import HOST, PublicReader, VerificationError, require, verify


PROBES = (("GET", "/api/custody/detections"), ("GET", "/api/receipts"),
          ("POST", "/api/catalogues/import?catalogue=access-probe&region=unqualified"),
          ("POST", "/api/detections/import:access-probe-missing/waveform"),
          ("POST", "/api/reviews"))


def probe(reader, method, path):
    require((method, path) in PROBES, "Unexpected access probe.")
    headers = {"User-Agent": "szl-seismic-access-verification", "Cache-Control": "no-cache"}
    body = None
    if method == "POST":
        body = b"" if path.startswith("/api/catalogues/import?") else b"{}"
        headers["Content-Type"] = "text/csv" if not body else "application/json"
    request = Request(HOST + path, data=body, method=method, headers=headers)
    try:
        response = reader.opener.open(request, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(4097)
        require(len(raw) <= 4096, "Access denial response exceeded size limit.")
        require(response.code == 503, "Hosted access did not fail closed with HTTP 503.")
        require(response.headers.get("Cache-Control") == "no-store", "Access response is cacheable.")
        value = json.loads(raw)
        require(value == {"detail": "hosted writes blocked: durable review storage is not qualified"},
                "Expected the explicit hosted storage gate.")
        return {"method": method, "path": path, "status": response.code, "passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"schema": "szl.seismic.write-guards/v1", "evidence_class": "BLOCKED", "passed": False,
              "source_revision": args.sha, "observed_at": datetime.now(timezone.utc).isoformat(),
              "probes": [], "boundary": "Unauthenticated denial checks only. No accepted live import, attachment, or review is claimed."}
    try:
        reader = PublicReader()
        report["runtime_before"] = {"checks": []}
        verify(args.sha, reader, report["runtime_before"])
        meta = reader.json(HOST + "/api/meta")
        require(meta.get("write_activation", {}).get("evidence_class") == "BLOCKED",
                "Expected hosted writes blocked before any access probe.")
        for method, path in PROBES:
            report["probes"].append(probe(reader, method, path))
        report["runtime_after"] = {"checks": []}
        verify(args.sha, reader, report["runtime_after"])
        require(report["runtime_before"] == report["runtime_after"], "Runtime changed during access probes.")
        report.update(passed=True, evidence_class="MEASURED")
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
