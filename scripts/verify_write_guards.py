"""Verify deny-by-default access on the canonical Space in an explicit mode.

Only empty invalid write bodies are sent, without credentials or source data.
No accepted transactions or positive workflow claims are made by this check.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from verify_deployment import HOST, MODES, PublicReader, require, verify


PROBES = (("GET", "/api/custody/detections"), ("GET", "/api/receipts"),
          ("POST", "/api/catalogues/import?catalogue=access-probe&region=unqualified"),
          ("POST", "/api/detections/import:access-probe-missing/waveform"),
          ("POST", "/api/reviews"))


def probe(reader, method, path, expected_mode="read-only"):
    require(expected_mode in MODES, "Expected mode must be read-only or active.")
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
        status = 503 if expected_mode == "read-only" else 401
        require(response.code == status, f"Hosted access did not fail closed with HTTP {status}.")
        require(response.headers.get("Cache-Control") == "no-store", "Access response is cacheable.")
        value = json.loads(raw)
        detail = ("hosted writes blocked: durable review storage is not qualified"
                  if expected_mode == "read-only" else "reviewer token required"
                  if path == "/api/reviews" else "operator token required")
        require(value == {"detail": detail}, "Expected the explicit hosted storage or authentication gate.")
        return {"method": method, "path": path, "status": response.code, "passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--mode", choices=MODES, default="read-only")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"schema": "szl.seismic.write-guards/v1", "evidence_class": "BLOCKED", "passed": False,
              "source_revision": args.sha, "expected_mode": args.mode,
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "probes": [], "boundary": "Unauthenticated denial checks only. No accepted live import, attachment, or review is claimed."}
    try:
        reader = PublicReader()
        report["runtime_before"] = {"checks": []}
        verify(args.sha, reader, report["runtime_before"], expected_mode=args.mode)
        meta = reader.json(HOST + "/api/meta")
        expected_class = "BLOCKED" if args.mode == "read-only" else "DECLARED"
        require(meta.get("write_activation", {}).get("evidence_class") == expected_class,
                "Write activation evidence differs from the expected mode.")
        for method, path in PROBES:
            report["probes"].append(probe(reader, method, path, expected_mode=args.mode))
        report["runtime_after"] = {"checks": []}
        verify(args.sha, reader, report["runtime_after"], expected_mode=args.mode)
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
