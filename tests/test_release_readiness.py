"""Offline release-gate tests. All GitHub responses below are simulated."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "release_readiness", Path(__file__).parents[1] / "scripts/check_release_readiness.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = "a" * 40


class FakeReader:
    def __init__(self):
        self.responses = {
            gate.ROOT: {"full_name": gate.REPOSITORY, "default_branch": "main"},
            gate.ROOT + "/branches/main": {"name": "main", "commit": {"sha": SHA}},
            gate.ROOT + f"/commits/{SHA}/check-runs": {"total_count": 0, "check_runs": []},
        }
        self.alert_pages = {1: ([], False)}
        self.calls = []
        for index, (filename, names) in enumerate(gate.WORKFLOWS.items(), start=1):
            workflow_id, run_id = index, index + 100
            path = ".github/workflows/" + filename
            run = {"id": run_id, "workflow_id": workflow_id, "path": path,
                   "event": "push", "head_branch": "main", "head_sha": SHA,
                   "repository": {"full_name": gate.REPOSITORY},
                   "head_repository": {"full_name": gate.REPOSITORY},
                   "run_number": 1, "run_attempt": 1,
                   "status": "completed", "conclusion": "success"}
            self.responses[gate.ROOT + "/actions/workflows/" + filename] = {
                "id": workflow_id, "path": path, "state": "active"}
            self.responses[gate.ROOT + f"/actions/workflows/{workflow_id}/runs"] = {
                "total_count": 1, "workflow_runs": [run]}
            self.responses[gate.ROOT + f"/actions/runs/{run_id}"] = deepcopy(run)
            self.responses[gate.ROOT + f"/actions/runs/{run_id}/attempts/1/jobs"] = {
                "total_count": len(names), "jobs": [
                    {"id": run_id * 10 + i, "name": name, "run_id": run_id,
                     "head_sha": SHA, "run_attempt": 1, "status": "completed", "conclusion": "success"}
                    for i, name in enumerate(names)]}

    def get(self, path, params=None):
        self.calls.append((path, params))
        return deepcopy(self.responses[path])

    def get_array_page(self, path, params):
        self.calls.append((path, params))
        assert path == gate.ROOT + "/code-scanning/alerts"
        assert params["ref"] == "refs/heads/main"
        assert params["state"] == "open" and params["tool_name"] == "CodeQL"
        return deepcopy(self.alert_pages[params["page"]])


def first_run(reader):
    return reader.responses[gate.ROOT + "/actions/workflows/1/runs"]["workflow_runs"][0]


def test_success_requires_both_exact_workflows_and_rechecks_tip():
    reader = FakeReader()
    result = gate.check_readiness(reader, SHA)
    assert result["eligible"]
    assert [row["run_id"] for row in result["workflows"]] == [101, 102]
    assert result["workflows"][0]["required_jobs"] == ["verify", "browser"]
    assert sum(path.endswith("/branches/main") for path, _ in reader.calls) == 2
    assert all(params["head_sha"] == SHA and params["event"] == "push"
               for path, params in reader.calls if path.endswith("/runs"))


@pytest.mark.parametrize("field,value", [
    ("event", "pull_request"), ("head_sha", "b" * 40), ("workflow_id", 999),
    ("path", ".github/workflows/spoof.yml"), ("status", "in_progress"),
    ("conclusion", "failure"), ("head_repository", {"full_name": "someone/fork"}),
])
def test_wrong_or_unsuccessful_run_is_blocked(field, value):
    reader = FakeReader()
    first_run(reader)[field] = value
    with pytest.raises(gate.ReadinessError):
        gate.check_readiness(reader, SHA)


def test_latest_failed_push_is_not_hidden_by_older_success():
    reader = FakeReader()
    new_run = {**first_run(reader), "id": 999, "run_number": 2, "conclusion": "failure"}
    runs = reader.responses[gate.ROOT + "/actions/workflows/1/runs"]
    runs.update(total_count=2, workflow_runs=[first_run(reader), new_run])
    with pytest.raises(gate.ReadinessError, match="successfully"):
        gate.check_readiness(reader, SHA)


@pytest.mark.parametrize("change", ["missing", "skipped", "wrong_attempt", "wrong_sha", "duplicate"])
def test_expected_jobs_must_have_exact_identity_and_succeed(change):
    reader = FakeReader()
    response = reader.responses[gate.ROOT + "/actions/runs/101/attempts/1/jobs"]
    job = response["jobs"][0]
    if change == "missing":
        response.update(total_count=0, jobs=[])
    elif change == "skipped":
        job["conclusion"] = "skipped"
    elif change == "wrong_attempt":
        job["run_attempt"] = 2
    elif change == "wrong_sha":
        job["head_sha"] = "b" * 40
    else:
        response["jobs"].append({**job, "id": 999})
        response["total_count"] = len(response["jobs"])
    with pytest.raises(gate.ReadinessError):
        gate.check_readiness(reader, SHA)


@pytest.mark.parametrize("browser_state", ["missing", "failure", "skipped"])
def test_successful_backend_does_not_replace_browser_verification(browser_state):
    reader = FakeReader()
    response = reader.responses[gate.ROOT + "/actions/runs/101/attempts/1/jobs"]
    if browser_state == "missing":
        response["jobs"] = [job for job in response["jobs"] if job["name"] != "browser"]
        response["total_count"] = len(response["jobs"])
    else:
        next(job for job in response["jobs"] if job["name"] == "browser")["conclusion"] = browser_state
    with pytest.raises(gate.ReadinessError):
        gate.check_readiness(reader, SHA)


def test_new_run_attempt_invalidates_previous_job_results():
    reader = FakeReader()
    reader.responses[gate.ROOT + "/actions/runs/101"]["run_attempt"] = 2
    with pytest.raises(gate.ReadinessError, match="attempt changed"):
        gate.check_readiness(reader, SHA)


def test_new_push_run_during_verification_is_blocked():
    reader = FakeReader()
    original = reader.get
    count = 0

    def new_run(path, params=None):
        nonlocal count
        result = original(path, params)
        if path.endswith("/workflows/1/runs"):
            count += 1
            if count == 2:
                result["workflow_runs"].append({**first_run(reader), "id": 999, "run_number": 2})
                result["total_count"] = 2
        return result

    reader.get = new_run
    with pytest.raises(gate.ReadinessError, match="Latest workflow run changed"):
        gate.check_readiness(reader, SHA)


@pytest.mark.parametrize("on_read", [1, 2])
def test_stale_tip_before_or_after_checks_is_blocked(on_read):
    reader = FakeReader()
    original = reader.get
    count = 0

    def moving_tip(path, params=None):
        nonlocal count
        result = original(path, params)
        if path.endswith("/branches/main"):
            count += 1
            if count == on_read:
                result["commit"]["sha"] = "b" * 40
        return result

    reader.get = moving_tip
    with pytest.raises(gate.ReadinessError, match="default-branch tip"):
        gate.check_readiness(reader, SHA)


@pytest.mark.parametrize("count", [0, 501])
def test_missing_or_unbounded_run_inventory_is_blocked(count):
    reader = FakeReader()
    reader.responses[gate.ROOT + "/actions/workflows/1/runs"] = {
        "total_count": count, "workflow_runs": []}
    with pytest.raises(gate.ReadinessError):
        gate.check_readiness(reader, SHA)


def test_collection_reads_second_page_and_rejects_incomplete_inventory():
    class Pages:
        def get(self, path, params):
            page = params["page"]
            return {"total_count": 101, "jobs": [
                {"id": i} for i in (range(1, 101) if page == 1 else [101])]}

    assert len(gate.collection(Pages(), gate.ROOT + "/jobs", "jobs")) == 101
    reader = FakeReader()
    reader.responses[gate.ROOT + "/actions/workflows/1/runs"]["total_count"] = 2
    with pytest.raises(gate.ReadinessError, match="Duplicate"):
        gate.check_readiness(reader, SHA)


def test_missing_credentials_emit_structured_blocked_result(monkeypatch, capsys):
    import json
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert gate.main(["--sha", SHA]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["evidence_class"] == "BLOCKED"
    assert result["eligible"] is False
    assert result["command"][-1] == SHA


def test_unauthorized_is_not_retried_and_does_not_echo_credentials():
    from urllib.error import HTTPError
    reader = gate.GitHubReader("synthetic-test-token")
    calls = []

    def unauthorized(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 401, "synthetic-test-token", {}, None)

    reader.opener.open = unauthorized
    with pytest.raises(gate.ReadinessError, match="HTTP 401; no retry") as failure:
        reader.get(gate.ROOT)
    assert len(calls) == 1
    assert "synthetic-test-token" not in str(failure.value)


def trusted_codeql_check():
    return {"id": 500, "name": "CodeQL", "head_sha": SHA, "status": "completed",
            "conclusion": "success", "app": {"id": 57789, "slug": "github-advanced-security",
                                                "owner": {"login": "github", "id": 9919}}}


@pytest.mark.parametrize("status,conclusion", [("completed", "failure"), ("in_progress", None)])
def test_successful_analysis_does_not_override_failed_or_pending_codeql_result(status, conclusion):
    reader = FakeReader()
    check = {**trusted_codeql_check(), "status": status, "conclusion": conclusion}
    reader.responses[gate.ROOT + f"/commits/{SHA}/check-runs"] = {"total_count": 1, "check_runs": [check]}
    with pytest.raises(gate.ReadinessError, match="CodeQL result check"):
        gate.check_readiness(reader, SHA)


def test_no_main_result_check_requires_readable_clean_open_alert_inventory():
    reader = FakeReader()
    evidence = gate.check_readiness(reader, SHA)["codeql_results"]
    assert evidence["separate_result_emitted"] is False
    assert evidence["open_alert_count"] == 0
    assert any(path.endswith("/code-scanning/alerts") for path, _ in reader.calls)


@pytest.mark.parametrize("has_result", [False, True])
def test_open_alerts_block_with_or_without_a_successful_result_check(has_result):
    reader = FakeReader()
    if has_result:
        reader.responses[gate.ROOT + f"/commits/{SHA}/check-runs"] = {
            "total_count": 1, "check_runs": [trusted_codeql_check()]}
    reader.alert_pages[1] = ([{"number": 1, "state": "open", "tool": {"name": "CodeQL"}}], False)
    with pytest.raises(gate.ReadinessError, match="Unresolved CodeQL"):
        gate.check_readiness(reader, SHA)


def test_unreadable_alert_api_blocks_without_a_result_check():
    reader = FakeReader()

    def forbidden(path, params):
        raise gate.ReadinessError("GitHub HTTP 403; no retry was attempted.")

    reader.get_array_page = forbidden
    with pytest.raises(gate.ReadinessError, match="HTTP 403"):
        gate.check_readiness(reader, SHA)


@pytest.mark.parametrize("spoof", ["id", "slug", "owner"])
def test_codeql_name_from_untrusted_app_cannot_pass(spoof):
    reader = FakeReader()
    check = trusted_codeql_check()
    check["app"][spoof] = {"login": "other", "id": 2} if spoof == "owner" else "other"
    reader.responses[gate.ROOT + f"/commits/{SHA}/check-runs"] = {"total_count": 1, "check_runs": [check]}
    with pytest.raises(gate.ReadinessError, match="untrusted app"):
        gate.check_readiness(reader, SHA)


def test_truncated_empty_alert_inventory_is_not_clean():
    reader = FakeReader()
    reader.alert_pages[1] = ([], True)
    with pytest.raises(gate.ReadinessError, match="unexpected continuation"):
        gate.check_readiness(reader, SHA)


def test_alert_pagination_is_bounded_and_exhausted():
    reader = FakeReader()
    for page in range(1, gate.MAX_PAGES + 1):
        reader.alert_pages[page] = ([{"number": page}], True)
    with pytest.raises(gate.ReadinessError, match="pagination limit"):
        gate.check_readiness(reader, SHA)
    reader.alert_pages = {1: ([{"number": 1}], True), 2: ([], False)}
    with pytest.raises(gate.ReadinessError, match="Unresolved CodeQL"):
        gate.check_readiness(reader, SHA)
