"""Explicit one-time private store bootstrap and isolated provider CAS check.

Never runs in the application. Uses the operator's existing Hub credentials;
never installs them in a Space. Existing main state is never overwritten.
Qualification commits are retained on a new branch, not on the live state head.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_store import (DATABASE_FILE, MANIFEST_FILE, REPOSITORY, SCHEMA,
                          HubDriver, canonical, digest, open_snapshot, require,
                          snapshot_manifest)


def provision(api, token, report):
    from huggingface_hub import CommitOperationAdd, hf_hub_download
    from huggingface_hub.errors import RepositoryNotFoundError, HfHubHTTPError
    created = False
    try:
        info = api.repo_info(REPOSITORY, repo_type="dataset")
    except RepositoryNotFoundError as error:
        # A 401/403 or hidden resource must not be treated as an empty store.
        require(error.response is not None and error.response.status_code == 404,
                "state repository access unavailable")
        api.create_repo(REPOSITORY, repo_type="dataset", private=True, exist_ok=False)
        created = True
        info = api.repo_info(REPOSITORY, repo_type="dataset")
    require(info.id == REPOSITORY and info.private is True, "expected private state repository")
    report["created_repository"] = created
    files = set(api.list_repo_files(REPOSITORY, repo_type="dataset", revision=info.sha))
    if DATABASE_FILE not in files and MANIFEST_FILE not in files:
        require(created and files <= {".gitattributes"}, "only a newly created empty repository can bootstrap")
        db = sqlite3.connect(":memory:")
        try:
            db.executescript(SCHEMA)
            db.commit()
            payload = db.serialize()
            manifest = snapshot_manifest(payload, db)
        finally:
            db.close()
        result = api.create_commit(REPOSITORY, repo_type="dataset", parent_commit=info.sha,
                                   commit_message="Bootstrap private Seismic Review state",
                                   operations=[CommitOperationAdd(DATABASE_FILE, payload),
                                               CommitOperationAdd(MANIFEST_FILE, canonical(manifest))])
        report["bootstrap_revision"] = result.oid
    else:
        require({DATABASE_FILE, MANIFEST_FILE} <= files, "incomplete state repository")
    driver = HubDriver(token)
    head, payload, manifest = driver.read()
    db = open_snapshot(payload, manifest)
    db.close()
    report.update(state_revision=head, state_sha256=digest(payload), state_bytes=len(payload),
                  receipt_count=manifest["receipts"]["count"])

    branch = "qualification-" + uuid.uuid4().hex
    report["qualification_branch"] = branch
    api.create_branch(REPOSITORY, repo_type="dataset", branch=branch, revision=head)
    probe = canonical({"schema": "szl.seismic.storage-probe/v1", "evidence_class": "SAMPLE",
                       "nonce": uuid.uuid4().hex, "parent": head})
    result = api.create_commit(REPOSITORY, repo_type="dataset", revision=branch, parent_commit=head,
                               commit_message="Qualify conditional snapshot commit",
                               operations=[CommitOperationAdd("qualification.json", probe)])
    accepted = result.oid
    conflict = False
    try:
        api.create_commit(REPOSITORY, repo_type="dataset", revision=branch, parent_commit=head,
                          commit_message="Stale parent must be rejected",
                          operations=[CommitOperationAdd("qualification.json", probe + b"\n")])
    except HfHubHTTPError as error:
        conflict = error.response is not None and error.response.status_code == 412
    require(conflict, "provider did not reject stale parent with HTTP 412")
    path = hf_hub_download(REPOSITORY, "qualification.json", repo_type="dataset",
                           revision=accepted, token=token, endpoint="https://huggingface.co")
    require(Path(path).read_bytes() == probe, "provider immutable readback mismatch")
    require(api.repo_info(REPOSITORY, repo_type="dataset", revision=branch).sha == accepted,
            "stale update changed qualification head")
    require(api.repo_info(REPOSITORY, repo_type="dataset").sha == head,
            "qualification changed live main state")
    report.update(qualification_revision=accepted, stale_parent_http_status=412,
                  immutable_readback=True, live_state_unchanged=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "use a fresh evidence path")
    report = {"schema": "szl.seismic.store-provision/v1", "repository": REPOSITORY,
              "evidence_class": "BLOCKED", "passed": False,
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "boundary": "Provider CAS and immutable readback; live application restart qualification is separate."}
    try:
        from huggingface_hub import HfApi, get_token
        token = get_token()
        require(bool(token), "operator Hub authentication unavailable")
        provision(HfApi(endpoint="https://huggingface.co", token=token), token, report)
        report.update(passed=True, evidence_class="MEASURED")
    except Exception as error:
        report["error_class"] = type(error).__name__
        report["error"] = "Provisioning or qualification failed; inspect provider state before retrying."
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
