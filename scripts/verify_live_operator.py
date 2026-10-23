"""Explicit live SAMPLE writes and a separate GET-only restart readback.

Only the canonical Seismic Review Space is accepted. Credentials come from the
environment, are never printed, and are sent only in Authorization headers.
There are no retries: a lost POST acknowledgement leaves the outcome UNKNOWN.
This measures software with named service principals, not independent humans.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


HOST = "https://szlholdings-szl-seismic-review.hf.space"
SPACE = "SZLHOLDINGS/szl-seismic-review"
HUB = "https://huggingface.co/api/spaces/" + SPACE
REPOSITORY = "https://github.com/szl-holdings/szl-seismic-review"
BACKEND = "HF_PRIVATE_DATASET_CAS_V1"
MAX_BYTES = 2_000_000
ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("sequence", "event_kind", "subject_id", "actor", "payload_sha256",
          "previous_sha256", "created_at")
FEATURES = {"n_pha_total": "12", "n_sta": "5", "moveout_rms": "0.2",
            "az_gap": "120", "sec_az_gap": "60", "dist_nearest_sta_km": "20", "mag": "1.75"}
PUBLIC_CARD_FIELDS = {"id", "event_id", "catalogue", "region", "origin", "published_consensus",
                      "review_count", "waveform_ready", "score_status", "confirmability_score"}


class VerificationError(Exception):
    """A required observation did not match; messages contain no response data."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate credential identity.")
        result[key] = value
    return result


class Credentials:
    def __init__(self, environ):
        self.operator = environ.get("SZL_REVIEW_WRITE_TOKEN", "")
        self.custodian = environ.get("SZL_REVIEW_CUSTODIAN_ID", "")
        try:
            reviewers = json.loads(environ.get("SZL_REVIEWER_TOKENS_JSON", "{}"),
                                   object_pairs_hook=unique_pairs)
        except (ValueError, TypeError):
            raise VerificationError("Invalid reviewer credential configuration.") from None
        require(isinstance(reviewers, dict) and len(reviewers) >= 2,
                "Two distinct reviewer credentials are required.")
        identities = [self.custodian, *reviewers]
        require(all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", v)
                    for v in identities) and self.custodian not in reviewers,
                "Distinct custodian and reviewer identities are required.")
        self.secrets = [self.operator, *reviewers.values()]
        require(all(isinstance(v, str) and 32 <= len(v) <= 512
                    and re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,2}", v) for v in self.secrets),
                "Credentials must be 32-512 character ASCII bearer tokens.")
        require(len(set(self.secrets)) == len(self.secrets), "Credentials must be distinct.")
        self.reviewers = dict(sorted(reviewers.items())[:2])

    def identities(self):
        return {"custodian": self.custodian, "reviewers": list(self.reviewers)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LiveClient:
    def __init__(self, credentials, report, opener=None, read_only=False):
        self.credentials, self.report = credentials, report
        self.opener = opener if opener is not None else build_opener(NoRedirect())
        self.read_only = read_only

    def request(self, method, path, *, token=None, payload=None, body=None, expected=200):
        require(method in {"GET", "POST"}, "Unsupported HTTP method.")
        require(not self.read_only or method == "GET", "Readback cannot issue writes.")
        url = HOST + path if path.startswith("/api/") else path
        parsed = urlsplit(url)
        require(url == HUB or (parsed.scheme == "https" and parsed.netloc == urlsplit(HOST).netloc
                and parsed.path.startswith("/api/") and not parsed.fragment), "Unexpected request target.")
        require(url != HUB or (method == "GET" and token is None), "Hub requests must be credential-free GETs.")
        require(not any(secret in url for secret in self.credentials.secrets), "Credentials cannot appear in URLs.")
        require(token is None or token in self.credentials.secrets, "Unknown request credential.")
        headers = {"User-Agent": "szl-seismic-live-operator-verification", "Cache-Control": "no-cache"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if payload is not None:
            body = canonical(payload)
            headers["Content-Type"] = "application/json"
        elif body is not None:
            headers["Content-Type"] = "text/csv; charset=utf-8"
        require(method == "POST" or body is None, "GET cannot contain a request body.")
        request = Request(url, data=body, headers=headers, method=method)
        self.report["pending_request"] = {"method": method, "path": parsed.path}
        try:
            try:
                response = self.opener.open(request, timeout=30)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code if isinstance(response, HTTPError) else response.status
                self.report["requests"].append({"method": method, "path": parsed.path, "status": status})
                require(status == expected, "Unexpected HTTP status.")
                require(not 300 <= status < 400, "Redirects are forbidden.")
                require(response.geturl() == url, "Response target changed.")
                if url != HUB:
                    require(response.headers.get("Cache-Control") == "no-store", "API cache policy mismatch.")
                if expected != 200:
                    raw = response.read(4097)
                    require(len(raw) <= 4096, "Access denial response exceeded the size limit.")
                    require(not any(secret.encode() in raw for secret in self.credentials.secrets),
                            "Response contained credential material.")
                    self.report.pop("pending_request", None)
                    return {}
                raw = response.read(MAX_BYTES + 1)
                require(len(raw) <= MAX_BYTES, "Response exceeded the size limit.")
                require(not any(secret.encode() in raw for secret in self.credentials.secrets),
                        "Response contained credential material.")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise VerificationError("Response was not valid JSON.") from None
                require(isinstance(data, dict), "Expected a JSON object.")
                self.report.pop("pending_request", None)
                return data
        except (URLError, TimeoutError, OSError, HTTPException):
            raise VerificationError("Request unavailable; a POST outcome may be UNKNOWN. No retry was issued.") from None


def source_guard(client, source):
    require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source), "Expected a full source SHA.")
    build = client.request("GET", "/api/build-info")
    require(build.get("build", {}).get("revision") == source
            and build.get("source_repository") == REPOSITORY
            and build.get("evidence_class") == "DECLARED" and build.get("receipt_minted") is False,
            "Runtime source binding mismatch.")
    hub = client.request("GET", HUB)
    revision = hub.get("sha")
    require(hub.get("id") == SPACE and hub.get("host") == HOST
            and isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision)
            and hub.get("runtime", {}).get("stage") == "RUNNING"
            and hub["runtime"].get("sha") == revision, "Hub identity or running revision mismatch.")
    return revision


def active_guard(client):
    meta = client.request("GET", "/api/meta")
    activation = meta.get("write_activation", {})
    require(meta.get("import_enabled") is True and meta.get("review_write_enabled") is True
            and meta.get("storage_state") == "REMOTE_COMMIT_BEFORE_ACK"
            and activation.get("mode") == "active" and activation.get("backend") == BACKEND
            and activation.get("evidence_class") == "DECLARED", "Active storage configuration mismatch.")
    process_boot_id = storage_process(client)
    credentials = client.credentials
    for identity, role, token in [(credentials.custodian, "custodian", credentials.operator),
                                  *((identity, "reviewer", token) for identity, token in credentials.reviewers.items())]:
        access = client.request("GET", "/api/access", token=token)
        require(access.get("identity") == identity and access.get("role") == role
                and access.get("evidence_class") == "DECLARED" and access.get("receipt_minted") is False,
                "Server-bound principal identity mismatch.")
    return process_boot_id


def storage_process(client):
    storage = client.request("GET", "/api/storage/status")
    require(storage.get("backend") == BACKEND and storage.get("state") == "READY"
            and storage.get("evidence_class") == "DECLARED" and storage.get("receipt_minted") is False,
            "Remote storage is not declared ready.")
    boot = storage.get("process_boot_id")
    require(isinstance(boot, str) and re.fullmatch(r"[0-9a-f]{32}", boot), "Process boot identity unavailable.")
    return boot


def receipts(client):
    data = client.request("GET", "/api/receipts", token=client.credentials.operator)
    entries = data.get("entries")
    require(isinstance(entries, list) and data.get("count") == len(entries)
            and data.get("chain_self_consistent") is True and data.get("signature_status") == "UNSIGNED"
            and data.get("receipt_minted") is False, "Receipt inventory or evidence boundary mismatch.")
    previous = "0" * 64
    for sequence, entry in enumerate(entries, 1):
        require(isinstance(entry, dict) and all(field in entry for field in FIELDS), "Incomplete receipt fields.")
        fields = {key: entry[key] for key in FIELDS}
        require(entry["sequence"] == sequence and entry["previous_sha256"] == previous
                and digest(canonical(fields)) == entry.get("receipt_sha256"), "Receipt chain mismatch.")
        previous = entry["receipt_sha256"]
    require(data.get("head_sha256") == (previous if entries else None), "Receipt head mismatch.")
    return {"count": len(entries), "head_sha256": data["head_sha256"], "entries": entries}


def same_receipts(before, after):
    require(before == after, "A rejected request or GET changed the receipt inventory.")


def sample_inputs(run_id):
    require(isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run_id), "Invalid SAMPLE run identifier.")
    catalogue = "SAMPLE-live-" + run_id
    rows = [{"event_id": catalogue + "-" + str(i), **FEATURES} for i in (1, 2)]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    body = stream.getvalue().encode()
    source_hash = digest(body)
    cases = []
    for index, row in enumerate(rows, 1):
        waveform = {"source_uri": "sample://software-acceptance/" + row["event_id"],
                    "sample_rate_hz": 100.0,
                    "stations": [{"index": station, "samples": [0.0, float(index * station), 0.0, -float(index * station)],
                                  "p_offset_s": 0.01, "s_offset_s": 0.03} for station in range(1, 6)]}
        cases.append({"id": "import:" + digest(canonical({"source_sha256": source_hash, "event_id": row["event_id"]})),
                      "event_id": row["event_id"], "waveform_sha256": digest(canonical(waveform)), "waveform": waveform})
    return catalogue, body, source_hash, cases


def detail_path(case):
    return "/api/detections/" + quote(case["id"], safe="")


def blind_detail(client, case, token, attached=True):
    data = client.request("GET", detail_path(case), token=token)
    require(set(data) == PUBLIC_CARD_FIELDS | {"waveform", "waveform_sha256", "blind"}
            and data.get("id") == case["id"] and data.get("blind") is True
            and data.get("event_id") is None and data.get("confirmability_score") is None
            and not any(key in data for key in ("features", "metadata", "reviews", "source_sha256")),
            "Blind case exposed private fields.")
    expected = {key: value for key, value in case["waveform"].items() if key != "source_uri"} if attached else None
    require(data.get("waveform") == expected
            and data.get("waveform_sha256") == (case["waveform_sha256"] if attached else None),
            "Blind waveform mismatch.")


def revealed_detail(client, case, token, expected_reviews):
    data = client.request("GET", detail_path(case), token=token)
    require(data.get("blind") is False and data.get("event_id") == case["event_id"]
            and data.get("waveform") == case["waveform"] and data.get("waveform_sha256") == case["waveform_sha256"]
            and data.get("features") == FEATURES, "Revealed case state mismatch.")
    actual = {row["reviewer_id"]: {key: row[key] for key in ("verdict", "note", "waveform_sha256")}
              for row in data.get("reviews", [])}
    require(actual == expected_reviews and len(data.get("reviews", [])) == len(expected_reviews),
            "Locked reviewer records mismatch.")


def public_redaction(client, catalogue, cases):
    for endpoint in ("/api/detections", "/api/export"):
        for hidden in (catalogue, cases[0]["event_id"], cases[0]["waveform"]["source_uri"]):
            # Search q is bounded to 100 characters; the catalogue matches both cases.
            if len(hidden) > 100:
                continue
            result = client.request("GET", endpoint + "?" + urlencode({"q": hidden}))
            require(result.get("total") == 0 and result.get("items") == [], "Public search exposed private metadata.")
        for case in cases:
            result = client.request("GET", endpoint + "?" + urlencode({"q": case["id"]}))
            items = result.get("items", [])
            require(result.get("total") == 1 and len(items) == 1 and items[0].get("id") == case["id"],
                    "Public case discovery mismatch.")
            card = items[0]
            require(set(card) == PUBLIC_CARD_FIELDS
                    and card.get("event_id") is None and card.get("confirmability_score") is None
                    and card.get("catalogue") == "Blind import"
                    and not any(key in card for key in ("features", "metadata", "reviews", "waveform", "source_sha256")),
                    "Public card exposed private evidence.")
            if endpoint == "/api/export":
                require(result.get("receipt_minted") is False and result.get("evidence_class") == "DECLARED",
                        "Export evidence boundary mismatch.")


def custody(client, catalogue, source_hash, cases, attached=True):
    result = client.request("GET", "/api/custody/detections?" + urlencode({"q": catalogue, "limit": 200}),
                            token=client.credentials.operator)
    require(result.get("total") == 2 and result.get("receipt_minted") is False
            and result.get("evidence_class") == "DECLARED", "Custody inventory mismatch.")
    items = {item["id"]: item for item in result.get("items", [])}
    require(len(result.get("items", [])) == 2 and set(items) == {case["id"] for case in cases},
            "Custody case identities mismatch.")
    for case in cases:
        row = items[case["id"]]
        require(row.get("event_id") == case["event_id"] and row.get("source_sha256") == source_hash
                and row.get("catalogue") == catalogue and row.get("region") == "japan_forearc"
                and row.get("waveform_sha256") == (case["waveform_sha256"] if attached else None),
                "Custody source mapping mismatch.")


def review_payload(case, verdict):
    return {"detection_id": case["id"], "verdict": verdict,
            "note": "SAMPLE software acceptance; service principal, no scientific or human-independent verdict."}


def observed_receipt(response, expected, state):
    receipt = response.get("receipt", {})
    require(receipt.get("signature_status") == "UNSIGNED" and isinstance(receipt.get("sequence"), int)
            and isinstance(receipt.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]),
            "Write acknowledgement lacks an unsigned receipt.")
    state["acknowledgements"].append({**expected, "sequence": receipt["sequence"], "receipt_sha256": receipt["sha256"]})


def check_appended(state, final):
    baseline = state["baseline"]
    count = baseline["count"]
    require(final["count"] == count + 5, "Unexpected receipt delta; concurrent writes or missing acknowledgement.")
    require((final["entries"][count - 1]["receipt_sha256"] if count else None) == baseline["head_sha256"],
            "Baseline receipt head changed.")
    appended = final["entries"][count:]
    require(len(state["acknowledgements"]) == 5, "Expected five write acknowledgements.")
    for entry, expected in zip(appended, state["acknowledgements"]):
        require(all(entry.get(key) == value for key, value in expected.items()), "Write receipt does not bind the intended mutation.")
    return appended


def verify_write(source, client, report, checkpoint):
    revision = source_guard(client, source)
    process_boot_id = active_guard(client)
    baseline = receipts(client)
    credentials = client.credentials
    reviewers = list(credentials.reviewers.items())
    (a, token_a), (b, token_b) = reviewers
    run_id = uuid.uuid4().hex
    catalogue, csv_body, source_hash, cases = sample_inputs(run_id)
    state = {"schema": "szl.seismic.live-operator-state/v1", "source_sha": source, "hub_revision": revision,
             "host": HOST, "run_id": run_id, "created_at": now(), "input_class": "SAMPLE",
             "process_boot_id": process_boot_id,
             "identities": credentials.identities(), "source_sha256": source_hash,
             "cases": [{key: value for key, value in case.items() if key != "waveform"} for case in cases],
             "baseline": {key: baseline[key] for key in ("count", "head_sha256")},
             "acknowledgements": [], "complete": False}
    checkpoint(state)
    url = "/api/catalogues/import?" + urlencode({"catalogue": catalogue, "region": "japan_forearc"})
    client.request("POST", url, token=token_a, body=csv_body, expected=401)
    client.request("POST", url, body=csv_body, expected=401)
    same_receipts(baseline, receipts(client))
    imported = client.request("POST", url, token=credentials.operator, body=csv_body)
    require(imported.get("imported") == 2 and imported.get("source_sha256") == source_hash, "Import acknowledgement mismatch.")
    observed_receipt(imported, {"event_kind": "CATALOGUE_IMPORTED", "subject_id": source_hash,
                     "actor": credentials.custodian,
                     "payload_sha256": digest(canonical({"source_sha256": source_hash, "record_count": 2,
                                                          "catalogue": catalogue, "region": "japan_forearc"}))}, state)
    checkpoint(state)
    custody(client, catalogue, source_hash, cases, attached=False)
    client.request("GET", "/api/custody/detections", token=token_a, expected=401)
    client.request("GET", "/api/receipts", token=token_a, expected=401)
    client.request("POST", url, token=credentials.operator, body=csv_body, expected=409)
    for case in cases:
        blind_detail(client, case, token_a, attached=False)
        client.request("GET", detail_path(case), expected=401)
        client.request("POST", detail_path(case) + "/waveform", token=token_a, payload=case["waveform"], expected=401)
        attached = client.request("POST", detail_path(case) + "/waveform", token=credentials.operator, payload=case["waveform"])
        require(attached.get("waveform_sha256") == case["waveform_sha256"] and attached.get("stations") == 5,
                "Waveform acknowledgement mismatch.")
        observed_receipt(attached, {"event_kind": "WAVEFORM_ATTACHED", "subject_id": case["id"],
                         "actor": credentials.custodian, "payload_sha256": case["waveform_sha256"]}, state)
        checkpoint(state)
        client.request("POST", detail_path(case) + "/waveform", token=credentials.operator, payload=case["waveform"], expected=409)
    case = cases[0]
    blind_detail(client, case, token_a)
    blind_detail(client, case, token_b)
    expected_reviews = {}
    for identity, token, verdict in ((a, token_a, "confirmed"), (b, token_b, "rejected")):
        payload = review_payload(case, verdict)
        client.request("POST", "/api/reviews", token=credentials.operator, payload=payload, expected=401)
        saved = client.request("POST", "/api/reviews", token=token, payload=payload)
        require(saved.get("saved") is True and saved.get("reviewer_id") == identity
                and saved.get("detection_id") == case["id"] and saved.get("verdict") == verdict
                and saved.get("waveform_sha256") == case["waveform_sha256"], "Review acknowledgement mismatch.")
        observed_receipt(saved, {"event_kind": "VERDICT_LOCKED", "subject_id": case["id"], "actor": identity,
                         "payload_sha256": digest(canonical({**payload, "reviewer_id": identity,
                                                              "waveform_sha256": case["waveform_sha256"]}))}, state)
        checkpoint(state)
        client.request("POST", "/api/reviews", token=token, payload=payload, expected=409)
        expected_reviews[identity] = {"verdict": verdict, "note": payload["note"], "waveform_sha256": case["waveform_sha256"]}
        revealed_detail(client, case, token, expected_reviews)
        if identity == a:
            blind_detail(client, case, token_b)
    final = receipts(client)
    state["receipts"] = check_appended(state, final)
    state["final"] = {key: final[key] for key in ("count", "head_sha256")}
    state["expected_reviews"] = expected_reviews
    verify_retained(client, state)
    require(source_guard(client, source) == revision, "Deployment changed during write verification.")
    require(storage_process(client) == process_boot_id, "Process changed during write verification.")
    same_receipts(final, receipts(client))
    state["complete"] = True
    checkpoint(state)
    report.update(run_id=run_id, hub_revision=revision, process_boot_id=process_boot_id,
                  receipt_baseline=state["baseline"],
                  receipt_final=state["final"], receipts=state["receipts"], cases=state["cases"])
    report["checks"].extend(["Identity-bound custody and two SAMPLE imports", "Two locked five-station SAMPLE attachments",
                             "Reviewer A reveal and reviewer B blindness before its distinct verdict",
                             "Wrong roles and duplicate writes refused without extra receipts",
                             "Five canonical unsigned receipts bind the mutations", "Public redaction and GETs mint no receipts"])


def verify_retained(client, state):
    catalogue, _, source_hash, cases = sample_inputs(state["run_id"])
    require(state["source_sha256"] == source_hash and state["cases"] == [
        {key: value for key, value in case.items() if key != "waveform"} for case in cases], "Saved SAMPLE state mismatch.")
    custody(client, catalogue, source_hash, cases)
    for token in client.credentials.reviewers.values():
        revealed_detail(client, cases[0], token, state["expected_reviews"])
        blind_detail(client, cases[1], token)
    for case in cases:
        blind_detail(client, case, client.credentials.operator)
    public_redaction(client, catalogue, cases)


def verify_readback(source, client, report, state):
    require(state.get("schema") == "szl.seismic.live-operator-state/v1" and state.get("complete") is True
            and state.get("source_sha") == source and state.get("host") == HOST and state.get("input_class") == "SAMPLE",
            "Completed state for this exact source and host is required.")
    require(state.get("identities") == client.credentials.identities(), "Readback identities differ from the write phase.")
    revision = source_guard(client, source)
    require(revision == state["hub_revision"], "Hub revision changed between phases.")
    process_boot_id = active_guard(client)
    prior_boot = state.get("process_boot_id")
    require(isinstance(prior_boot, str) and re.fullmatch(r"[0-9a-f]{32}", prior_boot)
            and process_boot_id != prior_boot, "Readback requires a different process boot identity after restart.")
    before = receipts(client)
    require({key: before[key] for key in ("count", "head_sha256")} == state["final"], "Retained receipt head or count mismatch.")
    require(check_appended(state, before) == state["receipts"], "Retained receipt fields differ.")
    verify_retained(client, state)
    require(source_guard(client, source) == revision, "Deployment changed during readback.")
    require(storage_process(client) == process_boot_id, "Process changed during restart readback.")
    same_receipts(before, receipts(client))
    report.update(run_id=state["run_id"], hub_revision=revision, process_boot_id=process_boot_id,
                  previous_process_boot_id=prior_boot, receipt_final=state["final"],
                  receipts=state["receipts"], cases=state["cases"])
    report["checks"].extend(["Changed process boot identity with unchanged source and Hub revision",
                             "Expected cases, waveforms, locked verdicts and five receipts survived",
                             "Public redaction, principal identities and unchanged GET receipt head"])


def save_json(path, value, secrets):
    rendered = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    # Defense in depth; expected fields are selected/validated before this point.
    for secret in secrets:
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    path.write_text(rendered, encoding="utf-8")


def exact_checkout(source):
    """Bind this acceptance harness and application to the requested source."""
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        require(head == source, "Live verification requires the exact source checkout.")
        subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "app.py", "review_store.py",
                        "frontend", "data", "models", "Dockerfile", "requirements.txt",
                        "scripts/verify_live_operator.py"], cwd=ROOT, check=True, capture_output=True)
        # git diff does not include untracked files; the new store and harness
        # must also exist in the selected commit.
        for path in ("review_store.py", "scripts/verify_live_operator.py"):
            subprocess.run(["git", "cat-file", "-e", "HEAD:" + path], cwd=ROOT,
                           check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        raise VerificationError("Live verification requires committed, unchanged application and harness source.") from None


def main(argv=None, *, environ=None, opener=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("write", "readback"), required=True)
    args = parser.parse_args(argv)
    report = {"schema": "szl.seismic.live-operator-verification/v1", "phase": args.phase,
              "evidence_class": "BLOCKED", "input_class": "SAMPLE", "signature_status": "UNSIGNED",
              "observed_at": now(), "expected_source": args.sha, "application_url": HOST,
              "passed": False, "checks": [], "requests": [],
              "boundary": "Software acceptance with SAMPLE inputs and named service principals; no independent human review, scientific replication or certification. Restart is performed and evidenced separately."}
    credentials = None
    state_path = args.output_dir / "expected-state.json"
    report_path = args.output_dir / (args.phase + "-report.json")
    try:
        require(re.fullmatch(r"[0-9a-f]{40}", args.sha), "Expected a full source SHA.")
        exact_checkout(args.sha)
        credentials = Credentials(os.environ if environ is None else environ)
        require(not report_path.exists(), "Phase report already exists; use a fresh output directory.")
        if args.phase == "write":
            require(not state_path.exists(), "State already exists; do not repeat SAMPLE writes.")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        client = LiveClient(credentials, report, opener, read_only=args.phase == "readback")
        if args.phase == "write":
            verify_write(args.sha, client, report, lambda state: save_json(state_path, state, credentials.secrets))
        else:
            require(state_path.stat().st_size <= MAX_BYTES, "Saved state exceeded the size limit.")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            verify_readback(args.sha, client, report, state)
        report.update(passed=True, evidence_class="MEASURED")
    except VerificationError as exc:
        report["error"] = str(exc)
    except Exception:
        # Exception text can include transport requests, filesystem paths or secrets.
        report["error"] = "Verification could not complete; observations remain BLOCKED and any pending POST outcome is UNKNOWN."
    secrets = credentials.secrets if credentials is not None else []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not report_path.exists():
        save_json(report_path, report, secrets)
    print(json.dumps({"passed": report["passed"], "evidence_class": report["evidence_class"], "phase": args.phase}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
