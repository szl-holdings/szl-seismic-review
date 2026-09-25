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
SOURCE_URL = "https://doi.org/10.5281/zenodo.22059219"
EARTHARXIV_URL = "https://eartharxiv.org/repository/view/14864/"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
        except (KeyError, TypeError, ValueError):
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
        """)


init_database()


def require_operator(authorization: str | None):
    if not WRITE_TOKEN:
        raise HTTPException(503, "operator writes are disabled on this deployment")
    supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied, WRITE_TOKEN):
        raise HTTPException(401, "operator token required")


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


def imported_card(row: sqlite3.Row, count: int = 0) -> dict:
    return {
        "id": row["id"],
        "event_id": None,
        "catalogue": "Blind import",
        "region": "Undisclosed during review",
        "origin": "OPERATOR_IMPORT",
        "published_consensus": None,
        "review_count": count,
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
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'"
    return response


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend/index.html")


@app.get("/healthz")
def health():
    return {"ok": True, "fixture_verified": True, "model_id": MODEL["id"]}


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
            "region": MODEL["region"],
            "n_train": MODEL["n_train"],
            "evaluation": MODEL["evaluation"],
            "claim": MODEL["claim"],
        },
        "review_write_enabled": bool(WRITE_TOKEN),
        "import_enabled": bool(WRITE_TOKEN),
        "storage_state": "LOCAL_FILE_UNVERIFIED_DURABILITY" if WRITE_TOKEN else "READ_ONLY",
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


@app.get("/api/detections")
def detections(limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0), catalogue: str | None = None):
    cards = [published_card(identifier, row) for identifier, row in PUBLISHED.items()
             if not catalogue or row["source_catalog"] == catalogue]
    with database() as db:
        imports = db.execute("SELECT * FROM detections ORDER BY imported_at DESC").fetchall()
        counts = {row[0]: row[1] for row in db.execute("SELECT detection_id, COUNT(*) FROM reviews GROUP BY detection_id")}
    cards.extend(imported_card(row, counts.get(row["id"], 0)) for row in imports
                 if not catalogue or row["catalogue"] == catalogue)
    return {"total": len(cards), "items": cards[offset : offset + limit]}


@app.get("/api/detections/{detection_id}")
def detection(detection_id: str, reviewer_id: str | None = None, authorization: str | None = Header(None)):
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
        require_operator(authorization)
        reviews = db.execute("SELECT reviewer_id,verdict,note,waveform_sha256,created_at FROM reviews WHERE detection_id=? ORDER BY created_at", (detection_id,)).fetchall()
    full_waveform = json.loads(row["waveform_json"]) if row["waveform_json"] else None
    blind_waveform = ({key: value for key, value in full_waveform.items() if key != "source_uri"}
                      if full_waveform else None)
    blind = {**imported_card(row, len(reviews)), "waveform": blind_waveform,
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
    body = await request.body()
    if len(body) > 16_384:
        raise HTTPException(413, "score request too large")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), dict):
        raise HTTPException(422, "region and features object required")
    scored = score_features(payload.get("region", ""), payload["features"])
    if scored["score_status"] == "JAPAN_PANEL_RESEARCH":
        scored["score_status"] = "UNVALIDATED_QUERY"
    return {"model_id": MODEL["id"], "claim": MODEL["claim"], **scored}


@app.post("/api/catalogues/import")
async def import_catalogue(request: Request, catalogue: str, region: str, authorization: str | None = Header(None)):
    require_operator(authorization)
    if not catalogue or len(catalogue) > 100 or not region or len(region) > 100:
        raise HTTPException(422, "catalogue and region must be 1-100 characters")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "CSV exceeds 1 MB")
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
        event_id = record["event_id"].strip()
        if not event_id or len(event_id) > 200 or event_id in seen:
            raise HTTPException(422, "blank, duplicate, or oversized event_id")
        seen.add(event_id)
        features = {name: record.get(name, "") for name in FEATURES}
        result = score_features(region, features)
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
    return {"imported": len(prepared), "source_sha256": source_hash, "waveforms_ready": 0}


@app.post("/api/detections/{detection_id}/waveform")
async def attach_waveform(detection_id: str, request: Request, authorization: str | None = Header(None)):
    require_operator(authorization)
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "waveform JSON exceeds 1 MB")
    try:
        evidence = json.loads(body)
    except json.JSONDecodeError:
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
        db.execute("UPDATE detections SET waveform_json=?,waveform_sha256=? WHERE id=?",
                   (encoded.decode("utf-8"), waveform_hash, detection_id))
    return {"waveform_sha256": waveform_hash, "stations": 5}


@app.post("/api/reviews")
async def submit_review(request: Request, authorization: str | None = Header(None)):
    require_operator(authorization)
    body = await request.body()
    if len(body) > 16_384:
        raise HTTPException(413, "review request too large")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(422, "review object required")
    detection_id = str(payload.get("detection_id", ""))
    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    verdict = payload.get("verdict")
    note = str(payload.get("note", ""))
    if not reviewer_id or len(reviewer_id) > 100 or len(note) > 2000 or verdict not in {"confirmed", "unresolved", "rejected"}:
        raise HTTPException(422, "reviewer, valid verdict, and note <=2000 chars required")
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
    return {"saved": True, "detection_id": detection_id, "reviewer_id": reviewer_id,
            "verdict": verdict, "waveform_sha256": row["waveform_sha256"]}
