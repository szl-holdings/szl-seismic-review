"""SZL Seismic Review: evidence-bound catalogue scoring and blind review.

The published Japan fixture is read-only. Operator imports and fresh reviews
require a configured bearer token and actual five-station waveform evidence.
"""

import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MODEL_PATH = ROOT / "models/japan_assoc_logistic_v1.json"
DEFAULT_DB_PATH = ROOT / "work/review.sqlite3" if os.name == "nt" else Path("/tmp/szl-seismic-review.sqlite3")
DB_PATH = Path(os.environ.get("SZL_REVIEW_DB_PATH", str(DEFAULT_DB_PATH)))
WRITE_TOKEN = os.environ.get("SZL_REVIEW_WRITE_TOKEN", "")
REVIEWER_TOKENS = json.loads(os.environ.get("SZL_REVIEWER_TOKENS_JSON", "{}"))
if (not isinstance(REVIEWER_TOKENS, dict)
        or any(not isinstance(reviewer, str) or not reviewer or len(reviewer) > 100
               or not isinstance(token, str) or not token
               for reviewer, token in REVIEWER_TOKENS.items())
        or len(set(REVIEWER_TOKENS.values())) != len(REVIEWER_TOKENS)
        or (WRITE_TOKEN and WRITE_TOKEN in REVIEWER_TOKENS.values())):
    raise RuntimeError("reviewer credentials must be unique nonempty strings distinct from the operator token")
SOURCE_URL = "https://doi.org/10.5281/zenodo.22059219"
EARTHARXIV_URL = "https://eartharxiv.org/repository/view/14864/"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def bounded_body(request: Request, limit: int, label: str) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, f"{label} exceeds {limit} bytes")
        body.extend(chunk)
    return bytes(body)


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def consensus(votes: list[str]) -> str:
    counts = Counter(votes)
    maximum = max(counts.values())
    leaders = [name for name, count in counts.items() if count == maximum]
    if len(leaders) != 1 or leaders[0] == "uncertain":
        return "unresolved"
    return "confirmed" if leaders[0] == "real" else "rejected"


MANIFEST = json.loads((DATA / "source_manifest.json").read_text(encoding="utf-8"))
for name, expected in MANIFEST["derived_sha256"].items():
    if digest_bytes((DATA / name).read_bytes()) != expected:
        raise RuntimeError(f"fixture checksum mismatch: {name}")
MODEL = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
if MODEL["source_sha256"] != MANIFEST["derived_sha256"]:
    raise RuntimeError("model and fixture source hashes do not match")
TRAINING_RECEIPT_PATHS = sorted((ROOT / "models/receipts").glob("train-*.json"))
if not TRAINING_RECEIPT_PATHS:
    raise RuntimeError("training receipt is missing")
previous_receipt_hash = "0" * 64
for expected_sequence, path in enumerate(TRAINING_RECEIPT_PATHS, start=1):
    training_receipt = json.loads(path.read_text(encoding="utf-8"))
    unsigned_fields = {key: value for key, value in training_receipt.items() if key != "receipt_sha256"}
    if (training_receipt["sequence"] != expected_sequence
            or training_receipt["previous_receipt_sha256"] != previous_receipt_hash
            or digest_bytes(canonical_json(unsigned_fields)) != training_receipt["receipt_sha256"]):
        raise RuntimeError("training receipt hash chain mismatch")
    previous_receipt_hash = training_receipt["receipt_sha256"]
if (training_receipt["model_sha256"] != digest_bytes(MODEL_PATH.read_bytes())
        or training_receipt["dataset_sha256"] != MANIFEST["derived_sha256"]
        or training_receipt["harness_sha256"] != digest_bytes(TRAINING_RECEIPT_PATHS[-1].with_suffix(".harness.py").read_bytes())
        or training_receipt["evaluation"] != MODEL["evaluation"]
        or training_receipt["seed"] != MODEL["training_seed"]
        or training_receipt["signature_status"] != "UNSIGNED"):
    raise RuntimeError("training receipt does not bind the served model and fixture")
PANEL = read_csv(DATA / "japan_panel.csv")
VERDICTS = read_csv(DATA / "japan_verdicts.csv")
VOTES: dict[str, list[dict]] = defaultdict(list)
for verdict in VERDICTS:
    VOTES[verdict["panel_id"]].append(verdict)
PUBLISHED = {f"published:{row['panel_id']}": row for row in PANEL}
FEATURES = tuple(MODEL["features"])


def score_features(region: str, values: dict) -> dict:
    if region != MODEL["region"]:
        return {"score_status": "UNQUALIFIED_REGION", "confirmability_score": None}
    transformed = []
    for name in FEATURES:
        try:
            raw = float(values[name])
        except (KeyError, TypeError, ValueError, OverflowError):
            return {"score_status": "MISSING_FEATURES", "confirmability_score": None}
        if not math.isfinite(raw) or (name in MODEL["transforms"] and raw < 0):
            return {"score_status": "INVALID_FEATURES", "confirmability_score": None}
        if name in ("az_gap", "sec_az_gap") and not 0 <= raw <= 360:
            return {"score_status": "INVALID_FEATURES", "confirmability_score": None}
        if name in ("n_pha_total", "n_sta", "moveout_rms", "dist_nearest_sta_km") and raw < 0:
            return {"score_status": "INVALID_FEATURES", "confirmability_score": None}
        transformed.append(math.log1p(raw) if name in MODEL["transforms"] else raw)
    if float(values["n_sta"]) > float(values["n_pha_total"]):
        return {"score_status": "INVALID_FEATURES", "confirmability_score": None}
    if any(value < minimum or value > maximum for value, minimum, maximum in zip(
        transformed, MODEL["training_min"], MODEL["training_max"]
    )):
        return {"score_status": "OUT_OF_SUPPORT", "confirmability_score": None}
    linear = MODEL["intercept"] + sum(
        coefficient * (value - mean) / scale
        for value, mean, scale, coefficient in zip(
            transformed, MODEL["mean"], MODEL["scale"], MODEL["coefficient"]
        )
    )
    probability = 1 / (1 + math.exp(-max(-700, min(700, linear))))
    return {
        "score_status": "JAPAN_PANEL_RESEARCH",
        "confirmability_score": round(probability, 6),
    }


# Opt-in input contract. /api/score applies it only when a request carries an
# "input_contract" key; score_features and catalogue import never consult it.
PLAIN_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
FIXTURE_BASIS = "holds in all 200 published fixture rows; not physical law"
INPUT_CONTRACT_V1 = {
    "version": "szl.seismic-input/v1",
    "model_id": MODEL["id"],
    "unit_policy": "REFUSE_MISMATCH_NEVER_CONVERT",
    "value_forms": ("finite JSON number (not boolean) or plain decimal string: optional leading minus, "
                    "ASCII digits, optional fraction; no exponent, whitespace, underscore, inf or nan"),
    "features": {
        "n_pha_total": {"unit": "count", "kind": "integer", "domain": {"min": 0},
                        "definition": "total phase count"},
        "n_sta": {"unit": "count", "kind": "integer", "domain": {"min": 0},
                  "definition": "station count"},
        "moveout_rms": {"unit": "s", "kind": "real", "domain": {"min": 0},
                        "definition": "travel-time move-out RMS"},
        "az_gap": {"unit": "deg", "kind": "real", "domain": {"min": 0, "max": 360},
                   "definition": "primary azimuth gap"},
        "sec_az_gap": {"unit": "deg", "kind": "real", "domain": {"min": 0, "max": 360},
                       "definition": "secondary azimuth gap; fixture rows match a second-largest-gap reading",
                       "definition_status": "UNVERIFIED"},
        "dist_nearest_sta_km": {"unit": "km", "kind": "real", "domain": {"min": 0},
                                "definition": "nearest station distance"},
        "mag": {"unit": "magnitude", "kind": "real", "domain": {},
                "definition": "catalogue magnitude", "scale": "UNDECLARED_IN_SOURCE_FIXTURE"},
    },
    "observed_invariants": [
        {"code": "PHASES_EXCEED_TWO_PER_STATION", "holds_when": "n_pha_total <= 2 * n_sta",
         "effect": "ADVISORY", "evidence_class": "DATA_OBSERVED", "basis": FIXTURE_BASIS},
        {"code": "SECONDARY_GAP_EXCEEDS_PRIMARY", "holds_when": "sec_az_gap <= az_gap",
         "effect": "ADVISORY", "evidence_class": "DATA_OBSERVED", "basis": FIXTURE_BASIS,
         "definition_status": "UNVERIFIED"},
        {"code": "GAPS_SUM_EXCEEDS_360", "holds_when": "az_gap + sec_az_gap <= 360",
         "effect": "ADVISORY", "evidence_class": "DATA_OBSERVED", "basis": FIXTURE_BASIS,
         "definition_status": "UNVERIFIED"},
    ],
    "claim": ("Conformance checks declared units, value forms and domains only. It is not scientific "
              "qualification and does not mean an input lies inside the training support."),
}


def contract_number(value: Any) -> float | None:
    """Parse a JSON number (not bool) or a plain decimal string; refuse every other form."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return math.inf
    if isinstance(value, str) and PLAIN_DECIMAL.fullmatch(value):
        return float(value)
    return None


def check_input_contract(values: Any, declared_units: Any) -> dict:
    """Check feature values against INPUT_CONTRACT_V1. Pure and stdlib-only.

    A declared unit must equal the contract unit exactly; a mismatch is refused,
    never converted. Advisory relations are DATA_OBSERVED and never change status.
    """
    values = values if isinstance(values, dict) else {}
    units = declared_units if isinstance(declared_units, dict) else {}
    violations = []
    parsed = {}
    for name in FEATURES:
        spec = INPUT_CONTRACT_V1["features"][name]
        declared = units.get(name)
        if declared is None:
            violations.append({"feature": name, "code": "UNIT_UNDECLARED", "expected": spec["unit"]})
        elif declared != spec["unit"]:
            violations.append({"feature": name, "code": "UNIT_MISMATCH", "expected": spec["unit"]})
        number = contract_number(values.get(name))
        domain = spec["domain"]
        if number is None:
            violations.append({"feature": name, "code": "NON_NUMERIC",
                               "expected": "JSON number or plain decimal string"})
        elif (not math.isfinite(number) or number < domain.get("min", -math.inf)
              or number > domain.get("max", math.inf)):
            expected = ["finite"]
            if "min" in domain:
                expected.append(f">= {domain['min']}")
            if "max" in domain:
                expected.append(f"<= {domain['max']}")
            violations.append({"feature": name, "code": "DOMAIN", "expected": ", ".join(expected)})
        elif spec["kind"] == "integer" and not number.is_integer():
            violations.append({"feature": name, "code": "NON_INTEGER_COUNT", "expected": "integer"})
        else:
            parsed[name] = number
    flagged = set()
    if {"n_pha_total", "n_sta"} <= parsed.keys() and parsed["n_pha_total"] > 2 * parsed["n_sta"]:
        flagged.add("PHASES_EXCEED_TWO_PER_STATION")
    if {"az_gap", "sec_az_gap"} <= parsed.keys():
        if parsed["sec_az_gap"] > parsed["az_gap"]:
            flagged.add("SECONDARY_GAP_EXCEEDS_PRIMARY")
        if parsed["az_gap"] + parsed["sec_az_gap"] > 360:
            flagged.add("GAPS_SUM_EXCEEDS_360")
    return {
        "version": INPUT_CONTRACT_V1["version"],
        "status": "VIOLATION" if violations else "CONFORMS",
        "violations": violations,
        "advisory": [dict(item) for item in INPUT_CONTRACT_V1["observed_invariants"] if item["code"] in flagged],
        "claim": INPUT_CONTRACT_V1["claim"],
    }


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_database():
    with database() as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS detections (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            catalogue TEXT NOT NULL,
            region TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            features_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            waveform_json TEXT,
            waveform_sha256 TEXT,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            detection_id TEXT NOT NULL REFERENCES detections(id),
            reviewer_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            note TEXT NOT NULL,
            waveform_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (detection_id, reviewer_id)
        );
        CREATE TABLE IF NOT EXISTS write_receipts (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            previous_sha256 TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


init_database()


def require_operator(authorization: str | None):
    if not WRITE_TOKEN:
        raise HTTPException(503, "operator writes are disabled on this deployment")
    supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied, WRITE_TOKEN):
        raise HTTPException(401, "operator token required")


def reviewer_identity(authorization: str | None) -> str:
    if not REVIEWER_TOKENS:
        raise HTTPException(503, "reviewer writes are disabled on this deployment")
    supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    for reviewer, token in REVIEWER_TOKENS.items():
        if hmac.compare_digest(supplied, token):
            return reviewer
    raise HTTPException(401, "reviewer token required")


def detail_identity(authorization: str | None) -> str | None:
    supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    if WRITE_TOKEN and hmac.compare_digest(supplied, WRITE_TOKEN):
        return None  # A source custodian may attach traces, but cannot claim a reviewer verdict.
    return reviewer_identity(authorization)


def receipt_fields(sequence: int, event_kind: str, subject_id: str, actor: str,
                   payload_sha256: str, previous_sha256: str, created_at: str) -> dict:
    return {"sequence": sequence, "event_kind": event_kind, "subject_id": subject_id,
            "actor": actor, "payload_sha256": payload_sha256,
            "previous_sha256": previous_sha256, "created_at": created_at}


def append_write_receipt(db: sqlite3.Connection, event_kind: str, subject_id: str,
                         actor: str, payload_sha256: str) -> dict:
    last = db.execute("SELECT sequence,receipt_sha256 FROM write_receipts ORDER BY sequence DESC LIMIT 1").fetchone()
    fields = receipt_fields(last["sequence"] + 1 if last else 1, event_kind, subject_id,
                            actor, payload_sha256, last["receipt_sha256"] if last else "0" * 64,
                            utc_now())
    receipt_sha256 = digest_bytes(canonical_json(fields))
    db.execute("INSERT INTO write_receipts VALUES (?,?,?,?,?,?,?,?)",
               (fields["sequence"], event_kind, subject_id, actor, payload_sha256,
                fields["previous_sha256"], receipt_sha256, fields["created_at"]))
    return {"sequence": fields["sequence"], "sha256": receipt_sha256,
            "signature_status": "UNSIGNED"}


def verify_write_receipts(db: sqlite3.Connection) -> dict:
    previous = "0" * 64
    count = 0
    valid = True
    for row in db.execute("SELECT * FROM write_receipts ORDER BY sequence"):
        count += 1
        fields = receipt_fields(row["sequence"], row["event_kind"], row["subject_id"],
                                row["actor"], row["payload_sha256"], row["previous_sha256"],
                                row["created_at"])
        if row["sequence"] != count or row["previous_sha256"] != previous or digest_bytes(canonical_json(fields)) != row["receipt_sha256"]:
            valid = False
        previous = row["receipt_sha256"]
    return {"count": count, "head_sha256": previous if count else None,
            "chain_self_consistent": valid, "signature_status": "UNSIGNED",
            "storage_durability": "UNVERIFIED"}


def published_card(identifier: str, row: dict) -> dict:
    votes = VOTES[row["panel_id"]]
    return {
        "id": identifier,
        "event_id": row["event_id"],
        "catalogue": row["source_catalog"],
        "region": "japan_forearc",
        "origin": "PUBLISHED_PANEL",
        "published_consensus": consensus([vote["verdict"] for vote in votes]),
        "review_count": len(votes),
        "waveform_ready": False,
        **score_features("japan_forearc", row),
    }


def imported_card(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "event_id": None,
        "catalogue": "Blind import",
        "region": "Undisclosed during review",
        "origin": "OPERATOR_IMPORT",
        "published_consensus": None,
        "review_count": None,
        "waveform_ready": bool(row["waveform_sha256"]),
        "score_status": "BLIND_REVIEW_PENDING",
        "confirmability_score": None,
    }


app = FastAPI(title="SZL Seismic Review", version="0.1.0", docs_url="/api/docs", redoc_url=None)
app.mount("/assets", StaticFiles(directory=ROOT / "frontend"), name="assets")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path == "/api/docs":
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self'; base-uri 'none'; object-src 'none'"
    else:
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'"
    return response


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend/index.html")


@app.get("/healthz")
def health():
    return {"ok": True, "fixture_verified": True, "model_id": MODEL["id"],
            "training_receipt_chain_self_consistent": True,
            "training_receipt_signature_status": "UNSIGNED"}


@app.get("/api/build-info")
def build_info():
    """Expose the publisher's file/environment agreement without minting receipts."""
    source_file = ROOT / "SOURCE_REVISION"
    try:
        revision = source_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        revision = ""
    bound = bool(re.fullmatch(r"[0-9a-f]{40}", revision)) and revision == os.environ.get("SZL_SOURCE_REVISION")
    return {
        "build": {"state": "OBSERVED" if bound else "UNKNOWN", "revision": revision if bound else None},
        "source_repository": "https://github.com/szl-holdings/szl-seismic-review",
        "evidence_class": "DECLARED" if bound else "UNKNOWN",
        "binding_basis": "publisher file and environment agreement; independent byte readback required",
        "receipt_minted": False,
    }


@app.get("/api/meta")
def meta():
    return {
        "title": "SZL Seismic Review",
        "source_url": SOURCE_URL,
        "paper_url": EARTHARXIV_URL,
        "source_license": "CC BY 4.0",
        "source_version": MANIFEST["source_version"],
        "model": {
            "id": MODEL["id"],
            "evidence_class": MODEL["evidence_class"],
            "region": MODEL["region"],
            "n_train": MODEL["n_train"],
            "evaluation": MODEL["evaluation"],
            "claim": MODEL["claim"],
        },
        "review_write_enabled": bool(REVIEWER_TOKENS),
        "import_enabled": bool(WRITE_TOKEN),
        "storage_state": "LOCAL_FILE_UNVERIFIED_DURABILITY" if WRITE_TOKEN or REVIEWER_TOKENS else "READ_ONLY",
        "waveforms_in_reference_archive": False,
    }


@app.get("/api/summary")
def summary():
    labels = [consensus([vote["verdict"] for vote in VOTES[row["panel_id"]]]) for row in PANEL]
    counts = Counter(labels)
    with database() as db:
        imported_count = db.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        ready = db.execute("SELECT COUNT(*) FROM detections WHERE waveform_sha256 IS NOT NULL").fetchone()[0]
    return {
        "total": len(PANEL) + imported_count,
        "published_count": len(PANEL),
        "imported_count": imported_count,
        "confirmed": counts["confirmed"],
        "rejected": counts["rejected"],
        "unresolved": counts["unresolved"],
        "ready_for_review": ready,
        "source_label": "PUBLISHED_PANEL / JAPAN / ZENODO 1.1.0",
    }


@app.get("/api/receipts")
def receipts(authorization: str | None = Header(None)):
    require_operator(authorization)
    with database() as db:
        return verify_write_receipts(db)


@app.get("/api/detections")
def detections(limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
               catalogue: str | None = None, origin: str | None = Query(None, pattern="^(published|imported)$")):
    cards = [] if origin == "imported" else [
        published_card(identifier, row) for identifier, row in PUBLISHED.items()
        if not catalogue or row["source_catalog"] == catalogue]
    with database() as db:
        imports = db.execute("SELECT * FROM detections ORDER BY imported_at DESC").fetchall()
    if not catalogue and origin != "published":
        cards = [imported_card(row) for row in imports] + cards
    return {"total": len(cards), "items": cards[offset : offset + limit]}


@app.get("/api/detections/{detection_id}")
def detection(detection_id: str, authorization: str | None = Header(None)):
    if detection_id in PUBLISHED:
        row = PUBLISHED[detection_id]
        return {
            **published_card(detection_id, row),
            "features": {name: float(row[name]) for name in FEATURES},
            "metadata": {name: row[name] for name in ("time", "lat", "lon", "dep")},
            "published_verdicts": VOTES[row["panel_id"]],
            "waveform": None,
            "source_url": SOURCE_URL,
        }
    with database() as db:
        row = db.execute("SELECT * FROM detections WHERE id=?", (detection_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "detection not found")
        reviewer_id = detail_identity(authorization)
        reviews = db.execute("SELECT reviewer_id,verdict,note,waveform_sha256,created_at FROM reviews WHERE detection_id=? ORDER BY created_at", (detection_id,)).fetchall()
    full_waveform = json.loads(row["waveform_json"]) if row["waveform_json"] else None
    blind_waveform = ({key: value for key, value in full_waveform.items() if key != "source_uri"}
                      if full_waveform else None)
    blind = {**imported_card(row), "waveform": blind_waveform,
             "waveform_sha256": row["waveform_sha256"], "blind": True}
    if not reviewer_id or not any(review["reviewer_id"] == reviewer_id for review in reviews):
        return blind
    imported_score = score_features(row["region"], json.loads(row["features_json"]))
    if imported_score["score_status"] == "JAPAN_PANEL_RESEARCH":
        imported_score["score_status"] = "UNVALIDATED_IMPORT"
    return {
        **blind,
        "blind": False,
        "event_id": row["event_id"],
        "features": json.loads(row["features_json"]),
        "metadata": json.loads(row["metadata_json"]),
        "waveform": full_waveform,
        "source_sha256": row["source_sha256"],
        "reviews": [dict(review) for review in reviews],
        **imported_score,
    }


@app.post("/api/score")
async def score(request: Request):
    body = await bounded_body(request, 16_384, "score request")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), dict):
        raise HTTPException(422, "region and features object required")
    contract_report = None
    if "input_contract" in payload:  # Opt-in only; without the key the response is unchanged.
        contract = payload["input_contract"]
        if not isinstance(contract, dict) or contract.get("version") != INPUT_CONTRACT_V1["version"]:
            raise HTTPException(422, f"input_contract needs version {INPUT_CONTRACT_V1['version']} and units")
        contract_report = check_input_contract(payload["features"], contract.get("units"))
        if contract_report["status"] != "CONFORMS":
            return {"model_id": MODEL["id"], "claim": MODEL["claim"],
                    "score_status": "INPUT_CONTRACT_VIOLATION", "confirmability_score": None,
                    "input_contract": contract_report}
    scored = score_features(payload.get("region", ""), payload["features"])
    if scored["score_status"] == "JAPAN_PANEL_RESEARCH":
        scored["score_status"] = "UNVALIDATED_QUERY"
    response = {"model_id": MODEL["id"], "claim": MODEL["claim"], **scored}
    if contract_report is not None:
        response["input_contract"] = contract_report
    return response


@app.post("/api/catalogues/import")
async def import_catalogue(request: Request, catalogue: str, region: str, authorization: str | None = Header(None)):
    require_operator(authorization)
    if not catalogue or len(catalogue) > 100 or not region or len(region) > 100:
        raise HTTPException(422, "catalogue and region must be 1-100 characters")
    body = await bounded_body(request, 1_000_000, "CSV")
    try:
        text = body.decode("utf-8-sig")
        records = list(csv.DictReader(io.StringIO(text)))
    except (UnicodeDecodeError, csv.Error):
        raise HTTPException(400, "invalid UTF-8 CSV") from None
    if not records or len(records) > 10_000:
        raise HTTPException(422, "CSV needs 1-10000 rows")
    if any("event_id" not in record for record in records):
        raise HTTPException(422, "event_id required")
    source_hash = digest_bytes(body)
    prepared = []
    seen = set()
    for record in records:
        event_id = (record["event_id"] or "").strip()
        if not event_id or len(event_id) > 200 or event_id in seen:
            raise HTTPException(422, "blank, duplicate, or oversized event_id")
        seen.add(event_id)
        features = {name: record.get(name, "") for name in FEATURES}
        result = score_features(MODEL["region"], features)
        if result["score_status"] in {"MISSING_FEATURES", "INVALID_FEATURES"}:
            raise HTTPException(422, f"invalid seven-feature row: {event_id}")
        metadata = {name: record.get(name, "") for name in ("time", "lat", "lon", "dep")}
        identifier = "import:" + digest_bytes(
            canonical_json({"source_sha256": source_hash, "event_id": event_id})
        )
        prepared.append((identifier, event_id, catalogue, region, source_hash,
                         json.dumps(features, sort_keys=True), json.dumps(metadata, sort_keys=True), utc_now()))
    with database() as db:
        try:
            db.executemany("INSERT INTO detections (id,event_id,catalogue,region,source_sha256,features_json,metadata_json,imported_at) VALUES (?,?,?,?,?,?,?,?)", prepared)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "this CSV was already imported") from None
        receipt = append_write_receipt(db, "CATALOGUE_IMPORTED", source_hash, "operator",
                                       digest_bytes(canonical_json({"source_sha256": source_hash,
                                                                    "record_count": len(prepared),
                                                                    "catalogue": catalogue, "region": region})))
    return {"imported": len(prepared), "source_sha256": source_hash, "waveforms_ready": 0,
            "receipt": receipt}


@app.post("/api/detections/{detection_id}/waveform")
async def attach_waveform(detection_id: str, request: Request, authorization: str | None = Header(None)):
    require_operator(authorization)
    body = await bounded_body(request, 1_000_000, "waveform JSON")
    try:
        evidence = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(evidence, dict) or not isinstance(evidence.get("source_uri"), str) or not evidence["source_uri"]:
        raise HTTPException(422, "waveform source_uri required")
    stations = evidence.get("stations")
    if not isinstance(stations, list) or len(stations) != 5:
        raise HTTPException(422, "exactly five blinded station traces required")
    try:
        rate = float(evidence["sample_rate_hz"])
        if not math.isfinite(rate) or rate <= 0 or rate > 1000:
            raise ValueError
        for station in stations:
            values = station["samples"]
            if not isinstance(values, list) or not 2 <= len(values) <= 10_000:
                raise ValueError
            if any(not math.isfinite(float(value)) for value in values):
                raise ValueError
            for name in ("p_offset_s", "s_offset_s"):
                if name in station and station[name] is not None and not math.isfinite(float(station[name])):
                    raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HTTPException(422, "invalid waveform trace shape or sample values") from None
    clean = {
        "source_uri": evidence["source_uri"],
        "sample_rate_hz": rate,
        "stations": [
            {"index": i + 1, "samples": [float(value) for value in station["samples"]],
             "p_offset_s": station.get("p_offset_s"), "s_offset_s": station.get("s_offset_s")}
            for i, station in enumerate(stations)
        ],
    }
    encoded = canonical_json(clean)
    waveform_hash = digest_bytes(encoded)
    with database() as db:
        row = db.execute("SELECT waveform_sha256 FROM detections WHERE id=?", (detection_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "imported detection not found")
        if row["waveform_sha256"]:
            raise HTTPException(409, "waveform evidence is locked after first attachment")
        updated = db.execute("UPDATE detections SET waveform_json=?,waveform_sha256=? WHERE id=? AND waveform_sha256 IS NULL",
                             (encoded.decode("utf-8"), waveform_hash, detection_id))
        if updated.rowcount != 1:
            raise HTTPException(409, "waveform evidence is locked after first attachment")
        receipt = append_write_receipt(db, "WAVEFORM_ATTACHED", detection_id, "operator", waveform_hash)
    return {"waveform_sha256": waveform_hash, "stations": 5, "receipt": receipt}


@app.post("/api/reviews")
async def submit_review(request: Request, authorization: str | None = Header(None)):
    reviewer_id = reviewer_identity(authorization)
    body = await bounded_body(request, 16_384, "review request")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(422, "review object required")
    detection_id = str(payload.get("detection_id", ""))
    verdict = payload.get("verdict")
    note = str(payload.get("note", ""))
    if "reviewer_id" in payload or len(note) > 2000 or not isinstance(verdict, str) or verdict not in {"confirmed", "unresolved", "rejected"}:
        raise HTTPException(422, "reviewer identity comes from the token; valid verdict and note <=2000 chars required")
    with database() as db:
        row = db.execute("SELECT waveform_sha256 FROM detections WHERE id=?", (detection_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "imported detection not found")
        if not row["waveform_sha256"]:
            raise HTTPException(409, "five-station waveform evidence required before review")
        try:
            db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)",
                       (detection_id, reviewer_id, verdict, note, row["waveform_sha256"], utc_now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "review already locked for this reviewer") from None
        receipt = append_write_receipt(db, "VERDICT_LOCKED", detection_id, reviewer_id,
                                       digest_bytes(canonical_json({"detection_id": detection_id,
                                                                    "reviewer_id": reviewer_id,
                                                                    "verdict": verdict, "note": note,
                                                                    "waveform_sha256": row["waveform_sha256"]})))
    return {"saved": True, "detection_id": detection_id, "reviewer_id": reviewer_id,
            "verdict": verdict, "waveform_sha256": row["waveform_sha256"], "receipt": receipt}
