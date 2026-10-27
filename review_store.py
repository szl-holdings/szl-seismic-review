"""Bounded review snapshots: private Hub commit CAS before acknowledgement.

SQLite is opened only in memory. The Hub's immutable commit is the durable
record; a process cache or bucket filesystem is never an authority. Intended
for a low-volume research pilot, with an 8 MiB database ceiling.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import uuid


BACKEND = "HF_PRIVATE_DATASET_CAS_V1"
REPOSITORY = "SZLHOLDINGS/szl-seismic-review-state"
MAX_DATABASE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 8192
DATABASE_FILE = "review.sqlite3"
MANIFEST_FILE = "manifest.json"
SCHEMA = """
CREATE TABLE detections (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, catalogue TEXT NOT NULL,
    region TEXT NOT NULL, source_sha256 TEXT NOT NULL, features_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL, waveform_json TEXT, waveform_sha256 TEXT,
    imported_at TEXT NOT NULL
);
CREATE TABLE reviews (
    detection_id TEXT NOT NULL REFERENCES detections(id), reviewer_id TEXT NOT NULL,
    verdict TEXT NOT NULL, note TEXT NOT NULL, waveform_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (detection_id, reviewer_id)
);
CREATE TABLE write_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL, actor TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def schema_identity(db):
    # SQLite records original whitespace in sqlite_master. Normalize only that
    # and IF NOT EXISTS; compare the full constraints, not just table names.
    def normalize(sql):
        return re.sub(r"\s+", "", sql.lower().replace("IF NOT EXISTS", "").replace("if not exists", "")).rstrip(";")
    return {name: normalize(sql) for name, sql in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'")}


def expected_schema():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        return schema_identity(db)
    finally:
        db.close()


class StorageError(Exception):
    """A sanitized error safe to expose without provider credentials."""


class StorageConflict(StorageError):
    pass


class StorageUncertain(StorageError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def require(condition, message):
    if not condition:
        raise StorageError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate manifest member")
        result[key] = value
    return result


def decode_manifest(raw):
    require(len(raw) <= MAX_MANIFEST_BYTES, "manifest exceeds limit")
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise StorageError("invalid manifest") from None
    require(isinstance(value, dict), "invalid manifest")
    return value


def check_database(db):
    require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
            "database integrity check failed")
    require(not db.execute("PRAGMA foreign_key_check").fetchall(),
            "database foreign key check failed")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    require(tables == {"detections", "reviews", "write_receipts", "sqlite_sequence"},
            "unexpected database schema")
    require(schema_identity(db) == expected_schema(), "database schema constraints mismatch")
    require(not db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone(),
            "unexpected database executable schema")
    previous = "0" * 64
    count = 0
    for row in db.execute("SELECT sequence,event_kind,subject_id,actor,payload_sha256,previous_sha256,receipt_sha256,created_at FROM write_receipts ORDER BY sequence"):
        count += 1
        sequence, kind, subject, actor, payload, predecessor, receipt, created = row
        fields = dict(sequence=sequence, event_kind=kind, subject_id=subject,
                      actor=actor, payload_sha256=payload, previous_sha256=predecessor,
                      created_at=created)
        require(sequence == count and predecessor == previous and digest(canonical(fields)) == receipt,
                "receipt chain mismatch")
        require(kind in {"CATALOGUE_IMPORTED", "WAVEFORM_ATTACHED", "VERDICT_LOCKED"},
                "unknown receipt kind")
        previous = receipt
    return {"count": count, "head_sha256": previous if count else None}


def open_snapshot(payload, manifest):
    require(0 < len(payload) <= MAX_DATABASE_BYTES, "database exceeds limit")
    require(manifest.get("schema") == "szl.seismic.snapshot/v1"
            and manifest.get("backend") == BACKEND
            and manifest.get("repository") == REPOSITORY,
            "snapshot identity mismatch")
    require(manifest.get("bytes") == len(payload) and manifest.get("sha256") == digest(payload),
            "snapshot bytes mismatch")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(payload)
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA foreign_keys=ON")
        require(check_database(db) == manifest.get("receipts"), "snapshot receipt head mismatch")
        db.row_factory = sqlite3.Row
        return db
    except Exception:
        db.close()
        raise


def snapshot_manifest(payload, db, operation_id=None):
    previous_factory = db.row_factory
    db.row_factory = None
    try:
        receipts = check_database(db)
    finally:
        db.row_factory = previous_factory
    require(len(payload) <= MAX_DATABASE_BYTES, "database exceeds limit")
    return {"schema": "szl.seismic.snapshot/v1", "backend": BACKEND,
            "repository": REPOSITORY, "bytes": len(payload), "sha256": digest(payload),
            "receipts": receipts, "operation_id": operation_id or str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "signature_status": "UNSIGNED"}


class HubDriver:
    """All network destinations are fixed to the dedicated private dataset."""

    def __init__(self, token):
        require(isinstance(token, str) and bool(token), "storage credential unavailable")
        from huggingface_hub import HfApi
        self.api = HfApi(endpoint="https://huggingface.co", token=token)
        self.token = token

    def read(self, revision=None):
        from huggingface_hub import hf_hub_download
        info = self.api.repo_info(REPOSITORY, repo_type="dataset", revision=revision or "main")
        require(info.id == REPOSITORY and info.private is True, "state repository must remain private")
        require(re.fullmatch(r"[0-9a-f]{40}", info.sha or "") is not None,
                "state revision unavailable")
        require(revision is None or info.sha == revision, "state revision mismatch")
        files = self.api.get_paths_info(REPOSITORY, [DATABASE_FILE, MANIFEST_FILE],
                                       repo_type="dataset", revision=info.sha)
        sizes = {entry.path: entry.size for entry in files}
        require(set(sizes) == {DATABASE_FILE, MANIFEST_FILE}
                and 0 < sizes[DATABASE_FILE] <= MAX_DATABASE_BYTES
                and 0 < sizes[MANIFEST_FILE] <= MAX_MANIFEST_BYTES,
                "state artifact sizes invalid")
        # Private snapshot files live only in a task-owned temporary directory.
        with tempfile.TemporaryDirectory(prefix="seismic-state-") as cache:
            values = {}
            for name in (MANIFEST_FILE, DATABASE_FILE):
                path = hf_hub_download(REPOSITORY, name, repo_type="dataset",
                                       revision=info.sha, token=self.token, cache_dir=cache,
                                       endpoint="https://huggingface.co")
                values[name] = Path(path).read_bytes()
                require(len(values[name]) == sizes[name], "state artifact length changed")
        return info.sha, values[DATABASE_FILE], decode_manifest(values[MANIFEST_FILE])

    def publish(self, parent, payload, manifest):
        from huggingface_hub import CommitOperationAdd
        from huggingface_hub.errors import HfHubHTTPError
        try:
            result = self.api.create_commit(
                REPOSITORY, repo_type="dataset", revision="main", parent_commit=parent,
                commit_message="Seismic review transaction " + manifest["operation_id"],
                operations=[CommitOperationAdd(DATABASE_FILE, payload),
                            CommitOperationAdd(MANIFEST_FILE, canonical(manifest))])
            return result.oid
        except HfHubHTTPError as error:
            if error.response is not None and error.response.status_code == 412:
                raise StorageConflict("state changed; reload before retrying") from None
            raise StorageUncertain("remote commit outcome unknown; inspect receipts before retrying") from None
        except Exception as error:
            # huggingface_hub 2.x delegates transport errors to httpx2 and
            # raises HTTPStatusError instead of HfHubHTTPError. Preserve the
            # same fail-closed CAS classification across both SDK families.
            response = getattr(error, "response", None)
            if getattr(response, "status_code", None) == 412:
                raise StorageConflict("state changed; reload before retrying") from None
            raise StorageUncertain("remote commit outcome unknown; inspect receipts before retrying") from None


class SnapshotStore:
    def __init__(self, driver):
        self.driver = driver
        self.lock = threading.RLock()

    def _read(self, revision=None):
        try:
            sha, payload, manifest = self.driver.read(revision)
            require(re.fullmatch(r"[0-9a-f]{40}", sha or "") is not None,
                    "state revision unavailable")
            require(revision is None or sha == revision, "state revision mismatch")
            db = open_snapshot(payload, manifest)
            return sha, payload, manifest, db
        except StorageError:
            raise
        except Exception:
            raise StorageError("durable review state unavailable or invalid") from None

    def status(self):
        with self.lock:
            _, _, _, db = self._read()
            db.close()
        return {"backend": BACKEND, "state": "READY", "evidence_class": "DECLARED",
                "commit_policy": "REMOTE_COMMIT_BEFORE_ACK", "receipt_minted": False}

    @contextmanager
    def transaction(self):
        with self.lock:
            parent, original, baseline, db = self._read()
            try:
                yield db
                db.commit()
                payload = db.serialize()
                if payload == original:
                    return
                require(db.total_changes > 0, "schema-only state changes are not admitted")
                manifest = snapshot_manifest(payload, db)
                old_count = baseline["receipts"]["count"]
                require(manifest["receipts"]["count"] == old_count + 1,
                        "each changed snapshot requires exactly one receipt")
                if old_count:
                    prior = db.execute("SELECT receipt_sha256 FROM write_receipts WHERE sequence=?", (old_count,)).fetchone()
                    require(prior is not None and prior[0] == baseline["receipts"]["head_sha256"],
                            "prior receipt prefix changed")
                try:
                    committed = self.driver.publish(parent, payload, manifest)
                except StorageConflict:
                    raise
                except StorageUncertain:
                    # Reconcile an uncertain submission once, by exact operation
                    # identity and full bytes. Never resubmit the mutation.
                    try:
                        _, observed, head, check = self._read()
                        check.close()
                        if observed == payload and head == manifest:
                            return
                    except StorageError:
                        raise StorageUncertain("remote commit outcome unknown; inspect receipts before retrying") from None
                    raise StorageUncertain("remote commit outcome unknown; inspect receipts before retrying") from None
                try:
                    _, observed, head, check = self._read(committed)
                    check.close()
                    require(observed == payload and head == manifest,
                            "committed snapshot readback mismatch")
                except StorageError:
                    raise StorageUncertain("remote commit outcome unknown; inspect receipts before retrying") from None
            finally:
                db.close()
