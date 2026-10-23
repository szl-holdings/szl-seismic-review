"""Storage fault injection with SAMPLE SQLite state; no provider requests."""

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import secrets
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_store as storage


SCHEMA = """
CREATE TABLE detections (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, catalogue TEXT NOT NULL,
    region TEXT NOT NULL, source_sha256 TEXT NOT NULL,
    features_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
    waveform_json TEXT, waveform_sha256 TEXT, imported_at TEXT NOT NULL
);
CREATE TABLE reviews (
    detection_id TEXT NOT NULL REFERENCES detections(id),
    reviewer_id TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT NOT NULL,
    waveform_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (detection_id, reviewer_id)
);
CREATE TABLE write_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL, actor TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def hashed(value):
    return hashlib.sha256(value).hexdigest()


def memory_database(schema=SCHEMA):
    db = sqlite3.connect(":memory:")
    db.executescript(schema)
    return db


def add_receipt(db, subject, kind="CATALOGUE_IMPORTED"):
    previous = db.execute(
        "SELECT sequence,receipt_sha256 FROM write_receipts ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    fields = dict(sequence=previous[0] + 1 if previous else 1, event_kind=kind,
                  subject_id=subject, actor="SAMPLE-operator",
                  payload_sha256=hashed(subject.encode()),
                  previous_sha256=previous[1] if previous else "0" * 64,
                  created_at="2026-10-21T00:00:00+00:00")
    receipt = hashed(encoded(fields))
    db.execute("INSERT INTO write_receipts VALUES (?,?,?,?,?,?,?,?)",
               (fields["sequence"], kind, subject, fields["actor"],
                fields["payload_sha256"], fields["previous_sha256"],
                receipt, fields["created_at"]))
    return receipt


def add_detection(db, identifier="SAMPLE-case", receipt=True):
    db.execute("INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?)",
               (identifier, identifier, "SAMPLE-catalogue", "SAMPLE-region",
                hashed(identifier.encode()), "{}", "{}", None, None,
                "2026-10-21T00:00:00+00:00"))
    if receipt:
        add_receipt(db, identifier)


def snapshot(db, operation="SAMPLE-bootstrap"):
    """Build independent fixture metadata, including deliberately invalid DBs."""
    db.commit()
    payload = db.serialize()
    rows = db.execute("SELECT sequence,receipt_sha256 FROM write_receipts ORDER BY sequence").fetchall()
    manifest = dict(schema="szl.seismic.snapshot/v1", backend=storage.BACKEND,
                    repository=storage.REPOSITORY, bytes=len(payload), sha256=hashed(payload),
                    receipts={"count": len(rows), "head_sha256": rows[-1][1] if rows else None},
                    operation_id=operation, created_at="2026-10-21T00:00:00+00:00",
                    signature_status="UNSIGNED")
    return payload, manifest


class FakeDriver:
    """Immutable revisions and atomic branch CAS, with explicit fault modes."""

    def __init__(self, db=None):
        initial = db or memory_database()
        self.versions = {}
        self.reads = []
        self.publishes = []
        self.mode = "normal"
        self.failure_canary = secrets.token_urlsafe(32)
        self._save(*snapshot(initial))
        if db is None:
            initial.close()

    def _save(self, payload, manifest):
        oid = hashed(payload + encoded(manifest))[:40]
        self.versions[oid] = (payload, copy.deepcopy(manifest))
        self.head = oid
        return oid

    def read(self, revision=None):
        self.reads.append(revision)
        if self.mode == "unavailable" or (self.mode in {"readback_failure", "timeout_read_failure"} and self.publishes):
            raise RuntimeError(self.failure_canary)
        oid = revision or self.head
        payload, manifest = self.versions[oid]
        if self.mode == "readback_mismatch" and revision is not None:
            manifest = {**manifest, "operation_id": "SAMPLE-different-operation"}
        if self.mode == "readback_wrong_revision" and revision is not None:
            oid = "f" * 40 if oid != "f" * 40 else "e" * 40
        return oid, payload, copy.deepcopy(manifest)

    def publish(self, parent, payload, manifest):
        self.publishes.append((parent, payload, copy.deepcopy(manifest)))
        if parent != self.head:
            raise storage.StorageConflict("state changed; reload before retrying")
        if self.mode == "timeout_before":
            raise storage.StorageUncertain("remote commit outcome unknown")
        oid = self._save(payload, manifest)
        if self.mode == "timeout_after_later":
            db = sqlite3.connect(":memory:")
            try:
                db.deserialize(payload)
                add_detection(db, "SAMPLE-later-writer")
                self._save(*snapshot(db, operation="SAMPLE-later-operation"))
            finally:
                db.close()
        if self.mode in {"timeout_after", "timeout_after_later", "timeout_read_failure"}:
            raise storage.StorageUncertain("remote commit outcome unknown")
        return oid


def contents(driver):
    payload, manifest = driver.versions[driver.head]
    db = storage.open_snapshot(payload, manifest)
    try:
        return ([row[0] for row in db.execute("SELECT id FROM detections ORDER BY id")],
                [tuple(row) for row in db.execute("SELECT * FROM write_receipts ORDER BY sequence")])
    finally:
        db.close()


def test_read_only_and_status_never_publish_or_mint_receipts():
    driver = FakeDriver()
    original = driver.head
    store = storage.SnapshotStore(driver)
    assert store.status()["receipt_minted"] is False
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0
    assert driver.head == original
    assert driver.publishes == []
    assert contents(driver) == ([], [])


def test_entity_and_receipt_acknowledged_only_after_exact_revision_readback():
    driver = FakeDriver()
    parent = driver.head
    store = storage.SnapshotStore(driver)
    with store.transaction() as db:
        add_detection(db)
        assert driver.head == parent
        assert driver.publishes == []
    assert len(driver.publishes) == 1
    assert driver.publishes[0][0] == parent
    assert driver.reads[-1] == driver.head
    cases, receipts = contents(driver)
    assert cases == ["SAMPLE-case"]
    assert len(receipts) == 1
    # A new process/store reads the committed state without a process cache.
    with storage.SnapshotStore(driver).transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM write_receipts").fetchone()[0] == 1
    assert len(driver.publishes) == 1


def test_application_failure_rolls_back_entity_and_receipt_without_publish():
    driver = FakeDriver()
    parent = driver.head
    fail_after_write = Mock(side_effect=ValueError("SAMPLE-abort"))
    with pytest.raises(ValueError, match="SAMPLE-abort"):
        with storage.SnapshotStore(driver).transaction() as db:
            add_detection(db)
            fail_after_write()
    fail_after_write.assert_called_once()
    assert driver.head == parent
    assert driver.publishes == []
    assert contents(driver) == ([], [])


@pytest.mark.parametrize("receipt_count", [0, 2])
def test_each_changed_transaction_requires_exactly_one_new_receipt(receipt_count):
    driver = FakeDriver()
    with pytest.raises(storage.StorageError):
        with storage.SnapshotStore(driver).transaction() as db:
            add_detection(db, receipt=False)
            for number in range(receipt_count):
                add_receipt(db, f"SAMPLE-receipt-{number}")
    assert driver.publishes == []
    assert contents(driver) == ([], [])


def test_rewritten_previous_receipt_is_rejected_even_when_new_chain_is_consistent():
    db = memory_database()
    add_detection(db, "SAMPLE-old")
    driver = FakeDriver(db)
    db.close()
    original = driver.head
    with pytest.raises(storage.StorageError):
        with storage.SnapshotStore(driver).transaction() as changed:
            changed.execute("DELETE FROM write_receipts")
            add_receipt(changed, "SAMPLE-forged-old")
            add_detection(changed, "SAMPLE-new")
    assert driver.publishes == []
    assert driver.head == original


def test_competing_writer_412_does_not_overwrite_or_retry():
    driver = FakeDriver()
    first = storage.SnapshotStore(driver)
    second = storage.SnapshotStore(driver)
    parent = driver.head
    with pytest.raises(storage.StorageConflict):
        with first.transaction() as first_db:
            add_detection(first_db, "SAMPLE-losing-case")
            with second.transaction() as second_db:
                add_detection(second_db, "SAMPLE-winning-case")
    assert len(driver.publishes) == 2
    assert [entry[0] for entry in driver.publishes] == [parent, parent]
    cases, receipts = contents(driver)
    assert cases == ["SAMPLE-winning-case"]
    assert len(receipts) == 1


def test_timeout_after_commit_reconciles_exact_operation_without_resubmitting():
    driver = FakeDriver()
    driver.mode = "timeout_after"
    with storage.SnapshotStore(driver).transaction() as db:
        add_detection(db)
    assert len(driver.publishes) == 1
    assert driver.reads == [None, None]
    assert len(contents(driver)[1]) == 1


def test_timeout_before_commit_remains_unknown_and_never_retries():
    driver = FakeDriver()
    driver.mode = "timeout_before"
    parent = driver.head
    with pytest.raises(storage.StorageUncertain):
        with storage.SnapshotStore(driver).transaction() as db:
            add_detection(db)
    assert len(driver.publishes) == 1
    assert driver.head == parent
    assert contents(driver) == ([], [])


def test_uncertain_commit_followed_by_other_operation_cannot_false_acknowledge():
    driver = FakeDriver()
    driver.mode = "timeout_after_later"
    with pytest.raises(storage.StorageUncertain):
        with storage.SnapshotStore(driver).transaction() as db:
            add_detection(db)
    assert len(driver.publishes) == 1
    cases, receipts = contents(driver)
    assert cases == ["SAMPLE-case", "SAMPLE-later-writer"]
    assert len(receipts) == 2


@pytest.mark.parametrize("mode", ["readback_failure", "readback_mismatch", "readback_wrong_revision", "timeout_read_failure"])
def test_failed_commit_readback_is_unknown_sanitized_and_never_retried(mode):
    driver = FakeDriver()
    driver.mode = mode
    with pytest.raises(storage.StorageUncertain) as error:
        with storage.SnapshotStore(driver).transaction() as db:
            add_detection(db)
    assert driver.failure_canary not in str(error.value)
    assert len(driver.publishes) == 1
    # Remote state may already be committed despite the error sent to the caller.
    assert len(contents(driver)[1]) == 1


def test_unavailable_read_never_uses_previous_process_state_or_leaks_provider_error():
    driver = FakeDriver()
    store = storage.SnapshotStore(driver)
    assert store.status()["receipt_minted"] is False
    driver.mode = "unavailable"
    with pytest.raises(storage.StorageError) as error:
        with store.transaction():
            pytest.fail("unavailable authoritative state must not yield a database")
    assert driver.failure_canary not in str(error.value)
    assert driver.publishes == []


@pytest.mark.parametrize("corruption", ["hash", "sqlite", "foreign_key", "chain", "receipt_head", "view", "table"])
def test_corrupt_state_fails_closed_without_bootstrap_or_publish(corruption):
    db = memory_database()
    add_detection(db)
    if corruption == "foreign_key":
        db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)",
                   ("SAMPLE-missing", "SAMPLE-A", "confirmed", "SAMPLE", "a" * 64, "SAMPLE"))
    elif corruption == "chain":
        db.execute("UPDATE write_receipts SET previous_sha256=?", ("f" * 64,))
    elif corruption == "view":
        db.execute("CREATE VIEW extra_view AS SELECT * FROM detections")
    elif corruption == "table":
        db.execute("CREATE TABLE unexpected(value TEXT)")
    driver = FakeDriver(db)
    db.close()
    payload, manifest = driver.versions[driver.head]
    if corruption == "hash":
        manifest["sha256"] = "f" * 64
    elif corruption == "sqlite":
        payload = b"SAMPLE-this-is-not-SQLite"
        manifest.update(bytes=len(payload), sha256=hashed(payload))
    elif corruption == "receipt_head":
        manifest["receipts"] = {"count": 0, "head_sha256": None}
    driver.versions[driver.head] = payload, manifest
    original = driver.head
    with pytest.raises(storage.StorageError):
        storage.SnapshotStore(driver).status()
    assert driver.head == original
    assert driver.publishes == []


@pytest.mark.parametrize("schema", [
    SCHEMA.replace("id TEXT PRIMARY KEY", "id TEXT"),
    SCHEMA.replace(" REFERENCES detections(id)", ""),
    SCHEMA.replace("PRIMARY KEY (detection_id, reviewer_id)", "UNIQUE (detection_id)"),
    SCHEMA.replace("event_id TEXT NOT NULL", "event_id TEXT"),
    SCHEMA.replace("metadata_json TEXT NOT NULL", "metadata_json INTEGER NOT NULL"),
])
def test_same_table_names_with_weakened_schema_are_rejected(schema):
    db = memory_database(schema)
    driver = FakeDriver(db)
    db.close()
    with pytest.raises(storage.StorageError):
        storage.SnapshotStore(driver).status()
    assert driver.publishes == []


@pytest.mark.parametrize("part", ["database", "manifest"])
def test_read_size_limit_blocks_before_sqlite_open(part):
    driver = FakeDriver()
    payload, manifest = driver.versions[driver.head]
    if part == "database":
        payload = b"x" * (storage.MAX_DATABASE_BYTES + 1)
        manifest.update(bytes=len(payload), sha256=hashed(payload))
        with pytest.raises(storage.StorageError):
            storage.open_snapshot(payload, manifest)
    else:
        with pytest.raises(storage.StorageError):
            storage.decode_manifest(b" " * (storage.MAX_MANIFEST_BYTES + 1))


def test_duplicate_manifest_keys_cannot_change_snapshot_identity():
    with pytest.raises(storage.StorageError):
        storage.decode_manifest(b'{"repository":"SAMPLE-one","repository":"SAMPLE-two"}')


def mocked_hub_driver():
    driver = object.__new__(storage.HubDriver)
    driver.token = secrets.token_urlsafe(32)
    driver.api = Mock()
    driver.api.repo_info.return_value = SimpleNamespace(
        id=storage.REPOSITORY, private=True, sha="1" * 40)
    return driver


@pytest.mark.parametrize("invalid", ["public", "wrong_repository", "missing", "oversized"])
def test_hub_driver_rejects_wrong_identity_privacy_or_artifact_size_before_download(monkeypatch, invalid):
    import huggingface_hub
    driver = mocked_hub_driver()
    driver.api.get_paths_info.return_value = [
        SimpleNamespace(path=storage.DATABASE_FILE, size=4096),
        SimpleNamespace(path=storage.MANIFEST_FILE, size=500)]
    if invalid == "public":
        driver.api.repo_info.return_value.private = False
    elif invalid == "wrong_repository":
        driver.api.repo_info.return_value.id = "SAMPLE/wrong-state"
    elif invalid == "missing":
        driver.api.get_paths_info.return_value = []
    else:
        driver.api.get_paths_info.return_value[0].size = storage.MAX_DATABASE_BYTES + 1
    download = Mock(side_effect=AssertionError("inadmissible state must not download"))
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    with pytest.raises(storage.StorageError):
        storage.SnapshotStore(driver).status()
    download.assert_not_called()
    driver.api.create_commit.assert_not_called()


def test_hub_downloads_both_files_from_resolved_oid_and_fixed_endpoint(monkeypatch, tmp_path):
    import huggingface_hub
    driver = mocked_hub_driver()
    fixture = FakeDriver()
    payload, manifest = fixture.versions[fixture.head]
    files = {storage.DATABASE_FILE: payload, storage.MANIFEST_FILE: encoded(manifest)}
    driver.api.get_paths_info.return_value = [
        SimpleNamespace(path=name, size=len(value)) for name, value in files.items()]
    def download(repo_id, filename, **kwargs):
        assert repo_id == storage.REPOSITORY
        assert kwargs["revision"] == "1" * 40
        assert kwargs["repo_type"] == "dataset"
        assert kwargs["token"] == driver.token
        assert kwargs["endpoint"] == "https://huggingface.co"
        path = tmp_path / filename
        path.write_bytes(files[filename])
        return str(path)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    oid, downloaded, metadata = driver.read()
    assert oid == "1" * 40
    assert downloaded == payload
    assert metadata == manifest
    assert driver.api.get_paths_info.call_args.kwargs["revision"] == oid


def test_hub_driver_rejects_a_different_oid_for_explicit_revision_before_download(monkeypatch):
    import huggingface_hub
    driver = mocked_hub_driver()
    download = Mock(side_effect=AssertionError("wrong immutable revision must not download"))
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    with pytest.raises(storage.StorageError):
        driver.read("2" * 40)
    driver.api.get_paths_info.assert_not_called()
    download.assert_not_called()


@pytest.mark.parametrize("status", [412, 403, 429, 500, None])
def test_hub_publish_failure_is_sanitized_classified_and_not_retried(status):
    import httpx
    from huggingface_hub.errors import HfHubHTTPError
    driver = mocked_hub_driver()
    canary = secrets.token_urlsafe(32)
    if status is None:
        error = TimeoutError(canary)
    else:
        response = httpx.Response(status, request=httpx.Request("POST", "https://huggingface.co"))
        error = HfHubHTTPError(canary, response=response)
    driver.api.create_commit.side_effect = error
    fixture = FakeDriver()
    payload, manifest = fixture.versions[fixture.head]
    expected = storage.StorageConflict if status == 412 else storage.StorageUncertain
    with pytest.raises(expected) as raised:
        driver.publish("1" * 40, payload, manifest)
    assert canary not in str(raised.value)
    assert driver.token not in str(raised.value)
    driver.api.create_commit.assert_called_once()
    arguments = driver.api.create_commit.call_args.kwargs
    assert arguments["parent_commit"] == "1" * 40
    assert arguments["revision"] == "main"
    assert arguments["repo_type"] == "dataset"
    assert {operation.path_in_repo for operation in arguments["operations"]} == {
        storage.DATABASE_FILE, storage.MANIFEST_FILE}


@pytest.mark.parametrize("outcome", ["success", "conflict", "timeout"])
def test_pinned_sdk_commit_posts_once_with_parent_and_both_files(monkeypatch, outcome):
    """Exercise the installed SDK serializer/POST path without provider requests."""
    import httpx
    from huggingface_hub import _commit_api
    driver = storage.HubDriver(secrets.token_urlsafe(32))

    def prepare_files(**kwargs):
        for operation in kwargs["additions"]:
            operation._upload_mode = "regular"

    monkeypatch.setattr(driver.api, "preupload_lfs_files", prepare_files)
    session = Mock()
    request = httpx.Request("POST", "https://huggingface.co/api/datasets/" + storage.REPOSITORY + "/commit/main")
    if outcome == "timeout":
        session.post.side_effect = httpx.ReadTimeout("SAMPLE-response-lost", request=request)
    else:
        session.post.return_value = httpx.Response(
            412 if outcome == "conflict" else 200, request=request,
            json={"commitOid": "2" * 40,
                  "commitUrl": "https://huggingface.co/datasets/" + storage.REPOSITORY + "/commit/" + "2" * 40})
    monkeypatch.setattr(_commit_api, "get_session", lambda: session)
    fixture = FakeDriver()
    payload, manifest = fixture.versions[fixture.head]
    if outcome == "success":
        assert driver.publish("1" * 40, payload, manifest) == "2" * 40
    else:
        expected = storage.StorageConflict if outcome == "conflict" else storage.StorageUncertain
        with pytest.raises(expected):
            driver.publish("1" * 40, payload, manifest)
    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args == (str(request.url),)
    items = [json.loads(line) for line in kwargs["content"].splitlines()]
    assert items[0]["value"]["parentCommit"] == "1" * 40
    assert {item["value"]["path"] for item in items[1:]} == {storage.DATABASE_FILE, storage.MANIFEST_FILE}


@pytest.fixture
def remote_service(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app as service
    driver = FakeDriver()
    tokens = {identity: secrets.token_urlsafe(32) for identity in ("custodian", "A", "B")}
    local_path = tmp_path / "must-not-exist.sqlite3"
    monkeypatch.setenv("SPACE_ID", "SZLHOLDINGS/szl-seismic-review")
    monkeypatch.setattr(service, "DB_PATH", local_path)
    monkeypatch.setattr(service, "REMOTE_STORE", storage.SnapshotStore(driver))
    monkeypatch.setattr(service, "ACTIVATION_MODE", "active")
    monkeypatch.setattr(service, "WRITE_TOKEN", tokens["custodian"])
    monkeypatch.setattr(service, "CUSTODIAN_ID", "SAMPLE-custodian")
    monkeypatch.setattr(service, "REVIEWER_TOKENS", {identity: tokens[identity] for identity in ("A", "B")})
    service.init_database()
    with TestClient(service.app, raise_server_exceptions=False) as client:
        yield service, client, driver, local_path, {
            identity: {"Authorization": "Bearer " + token} for identity, token in tokens.items()}
    assert not local_path.exists()


def remote_sample_csv(service):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=["event_id", *service.FEATURES, "time"])
    writer.writeheader()
    writer.writerow({"event_id": "SAMPLE-private-source", "time": "SAMPLE",
                     **{key: service.PANEL[0][key] for key in service.FEATURES}})
    return stream.getvalue()


def test_remote_application_transactions_and_reviewer_isolation(remote_service):
    service, client, driver, _, auth = remote_service
    import_route = "/api/catalogues/import?catalogue=SAMPLE-private&region=japan_forearc"
    assert client.post(import_route, content=remote_sample_csv(service), headers=auth["A"]).status_code == 401
    assert not driver.reads and not driver.publishes
    assert client.post(import_route, content=remote_sample_csv(service), headers=auth["custodian"]).status_code == 200
    identity = client.get("/api/custody/detections", headers=auth["custodian"]).json()["items"][0]["id"]
    waveform = {"source_uri": "sample://private-source", "sample_rate_hz": 10,
                "stations": [{"samples": [0, 1, -1, 0]} for _ in range(5)]}
    assert client.post(f"/api/detections/{identity}/waveform", json=waveform, headers=auth["custodian"]).status_code == 200
    assert client.get(f"/api/detections/{identity}", headers=auth["A"]).json()["blind"] is True
    assert client.post("/api/reviews", json={"detection_id": identity, "verdict": "confirmed", "note": "SAMPLE A"}, headers=auth["A"]).status_code == 200
    assert client.get(f"/api/detections/{identity}", headers=auth["A"]).json()["event_id"] == "SAMPLE-private-source"
    still_blind = client.get(f"/api/detections/{identity}", headers=auth["B"]).json()
    assert still_blind["blind"] is True and still_blind.get("event_id") is None and "reviews" not in still_blind
    assert "SAMPLE-private-source" not in json.dumps(still_blind)
    assert client.post("/api/reviews", json={"detection_id": identity, "verdict": "rejected", "note": "SAMPLE B"}, headers=auth["B"]).status_code == 200
    assert client.post("/api/reviews", json={"detection_id": identity, "verdict": "confirmed"}, headers=auth["B"]).status_code == 409
    receipts = client.get("/api/receipts", headers=auth["custodian"]).json()
    assert len(driver.publishes) == 4 and len(receipts["entries"]) == 4
    assert {entry["actor"] for entry in receipts["entries"]} == {"SAMPLE-custodian", "A", "B"}
    assert client.get("/api/storage/status").json()["backend"] == storage.BACKEND
    assert len(driver.publishes) == 4


def test_read_only_remote_mode_allows_authenticated_recovery_reads_but_no_writes(remote_service, monkeypatch):
    service, client, driver, _, auth = remote_service
    monkeypatch.setattr(service, "ACTIVATION_MODE", "read-only")
    assert client.get("/api/access", headers=auth["custodian"]).json()["identity"] == "SAMPLE-custodian"
    assert client.get("/api/receipts", headers=auth["custodian"]).status_code == 200
    assert client.get("/api/custody/detections", headers=auth["custodian"]).status_code == 200
    assert client.post("/api/catalogues/import?catalogue=SAMPLE&region=SAMPLE", content=remote_sample_csv(service), headers=auth["custodian"]).status_code == 503
    assert client.post("/api/detections/SAMPLE/waveform", json={}, headers=auth["custodian"]).status_code == 503
    assert client.post("/api/reviews", json={}, headers=auth["A"]).status_code == 503
    assert driver.publishes == []


@pytest.mark.parametrize("mode,expected", [("unavailable", "BLOCKED"), ("timeout_read_failure", "UNKNOWN")])
def test_remote_application_storage_failure_never_acknowledges_or_creates_local_db(remote_service, mode, expected):
    service, client, driver, local_path, auth = remote_service
    driver.mode = mode
    response = client.post("/api/catalogues/import?catalogue=SAMPLE&region=SAMPLE",
                           content=remote_sample_csv(service), headers=auth["custodian"])
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert response.json()["evidence_class"] == expected
    assert driver.failure_canary not in response.text
    assert not local_path.exists()
    assert len(driver.publishes) == (0 if mode == "unavailable" else 1)
