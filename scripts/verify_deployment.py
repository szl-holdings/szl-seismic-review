"""Bounded, credential-free GET checks of the published Seismic Review Space.

Run from the exact source checkout. This is a separate runtime observation from
the uploader, not an independent party's scientific replication or certification.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
SPACE = "SZLHOLDINGS/szl-seismic-review"
HOST = "https://szlholdings-szl-seismic-review.hf.space"
HUB = f"https://huggingface.co/api/spaces/{SPACE}"
REPOSITORY = "https://github.com/szl-holdings/szl-seismic-review"
MAX_BYTES = 2_000_000
STATIC = {"/": "frontend/index.html", "/assets/app.js": "frontend/app.js",
          "/assets/app.css": "frontend/app.css"}


class VerificationError(Exception):
    """A required observation was unavailable or did not match this source."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicReader:
    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def read(self, url):
        require(url == HUB or url.startswith(HOST + "/"), "Unexpected read target.")
        request = Request(url, headers={"User-Agent": "szl-seismic-runtime-verification",
                                        "Cache-Control": "no-cache"}, method="GET")
        try:
            with self.opener.open(request, timeout=20) as response:
                require(response.status == 200, "Expected HTTP 200.")
                data = response.read(MAX_BYTES + 1)
                require(len(data) <= MAX_BYTES, "Response exceeded the size limit.")
                return data, {key.lower(): value for key, value in response.headers.items()}
        except HTTPError as exc:
            raise VerificationError(f"Public endpoint returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise VerificationError("Public endpoint unavailable from this runner.") from None

    def json(self, url):
        data, headers = self.read(url)
        if url.startswith(HOST + "/api/"):
            require(headers.get("cache-control") == "no-store", "API cache policy mismatch.")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise VerificationError("Public endpoint did not return valid JSON.") from None
        require(isinstance(value, dict), "Expected a JSON object.")
        return value


def verify(source, reader, report, root=ROOT):
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source)), "Expected a full source SHA.")
    model_bytes = (root / "models/japan_assoc_logistic_v1.json").read_bytes()
    model = json.loads(model_bytes)
    manifest = json.loads((root / "data/source_manifest.json").read_bytes())
    receipt_bytes = sorted((root / "models/receipts").glob("train-*.json"))[-1].read_bytes()
    receipt = json.loads(receipt_bytes)
    with (root / "data/japan_panel.csv").open(encoding="utf-8", newline="") as handle:
        panel_count = sum(1 for _ in csv.DictReader(handle))

    hub = reader.json(HUB)
    require(hub.get("id") == SPACE and hub.get("host") == HOST, "Unexpected Space identity.")
    revision = hub.get("sha", "")
    require(isinstance(revision, str) and bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "Hub revision unavailable.")
    require(hub.get("runtime", {}).get("stage") == "RUNNING", "Space is not reported RUNNING.")
    require(hub["runtime"].get("sha") == revision, "Running revision differs from Hub revision.")
    report["hub_revision"] = revision
    report["checks"].append("Hub identity and running revision agree")

    def binding():
        value = reader.json(HOST + "/api/build-info")
        require(value.get("build", {}).get("revision") == source, "Runtime source mismatch.")
        require(value.get("evidence_class") == "DECLARED" and value.get("receipt_minted") is False,
                "Runtime binding evidence or read semantics mismatch.")
        require(value.get("source_repository") == REPOSITORY, "Source repository mismatch.")
        return value

    build = binding()
    report["checks"].append("Runtime source matches expected GitHub revision")
    for route, path in STATIC.items():
        actual, _ = reader.read(HOST + route)
        require(actual == (root / path).read_bytes(), f"Static source mismatch: {route}")
        report["checks"].append({"route": route, "sha256": digest(actual)})

    evidence = reader.json(HOST + "/api/evidence")
    require(evidence.get("schema") == "szl.seismic.evidence/v1" and evidence.get("receipt_minted") is False,
            "Evidence schema or read semantics mismatch.")
    served_model, served_receipt = evidence.get("model", {}), evidence.get("training_receipt", {})
    require(served_model.get("sha256") == digest(model_bytes), "Model bytes mismatch.")
    require(served_model.get("evaluation") == model["evaluation"] and served_model.get("evidence_class") == model["evidence_class"],
            "Model evaluation or evidence class mismatch.")
    require(served_receipt.get("sha256") == digest(receipt_bytes)
            and served_receipt.get("chain_head_sha256") == receipt["receipt_sha256"]
            and served_receipt.get("signature_status") == receipt["signature_status"], "Training receipt mismatch.")
    require(evidence.get("source", {}).get("files_sha256") == manifest["derived_sha256"], "Source data identity mismatch.")
    require(served_model.get("independent_replay") == "UNAVAILABLE"
            and evidence.get("limits", {}).get("new_region_validation") == "UNAVAILABLE", "Scientific limits mismatch.")
    require(evidence.get("runtime") == build, "Evidence runtime binding mismatch.")
    report["checks"].append("Model, evaluation, data and receipt identities retain their evidence limits")

    summary = reader.json(HOST + "/api/summary")
    require(summary.get("published_count") == panel_count and summary.get("imported_count") == 0,
            "Public panel inventory mismatch.")
    require(all(summary.get(key) == value for key, value in model["class_counts"].items()), "Panel verdict counts mismatch.")
    search = reader.json(HOST + "/api/detections?q=j001")
    require(search.get("total") == 1 and len(search.get("items", [])) == 1
            and search["items"][0].get("id") == "published:J001", "Published case search mismatch.")
    exported = reader.json(HOST + "/api/export?consensus=confirmed&limit=2")
    require(exported.get("count") == 2 and len(exported.get("items", [])) == 2
            and exported.get("total") == model["class_counts"]["confirmed"]
            and exported.get("truncated") is True and exported.get("next_offset") == 2
            and exported.get("receipt_minted") is False, "Bounded export metadata mismatch.")
    require(all(item.get("published_consensus") == "confirmed" for item in exported["items"]), "Export filter mismatch.")
    report["checks"].append("Published counts, case search and bounded filtered export")
    meta = reader.json(HOST + "/api/meta")
    require(meta.get("import_enabled") is False and meta.get("review_write_enabled") is False
            and meta.get("storage_state") == "READ_ONLY", "Public write configuration mismatch.")
    report["checks"].append("Public service remains read-only")

    require(binding() == build, "Runtime source changed during verification.")
    final_hub = reader.json(HUB)
    require(final_hub.get("sha") == revision and final_hub.get("runtime", {}).get("sha") == revision
            and final_hub.get("runtime", {}).get("stage") == "RUNNING", "Space changed during verification.")
    report["checks"].append("Source and Hub revisions remained stable during verification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"schema": "szl.seismic.runtime-verification/v1", "evidence_class": "BLOCKED",
              "observed_at": datetime.now(timezone.utc).isoformat(), "expected_source": args.sha,
              "application_url": HOST, "passed": False, "checks": [],
              "boundary": "Separate public GET readback from the uploader; same operator, no independent scientific replication or certification."}
    try:
        verify(args.sha, PublicReader(), report)
        report.update(passed=True, evidence_class="MEASURED")
    except (VerificationError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        report["error"] = str(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
