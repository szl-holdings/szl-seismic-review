"""Read-only, fail-closed publication preflight using GitHub's Actions API."""

import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY = "szl-holdings/szl-seismic-review"
ROOT = f"/repos/{REPOSITORY}"
MAX_PAGES = 5
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
WORKFLOWS = {
    "ci.yml": ("verify", "browser"),
    "codeql.yml": (
        "analyze / Analyze (python)",
        "analyze / Analyze (javascript-typescript)",
    ),
}


class ReadinessError(Exception):
    """The source is not eligible, or required evidence could not be read."""


def require(condition, message):
    if not condition:
        raise ReadinessError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubReader:
    def __init__(self, token):
        require(bool(token), "GH_TOKEN is required; anonymous evidence is not accepted.")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def _read(self, path, params=None):
        require(path.startswith(ROOT + "/") or path == ROOT, "Unexpected API path.")
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "szl-seismic-release-readiness",
        })
        try:
            with self.opener.open(request, timeout=20) as response:
                require(response.status == 200, "GitHub did not return HTTP 200.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                require(len(raw) <= MAX_RESPONSE_BYTES, "GitHub response exceeded the size limit.")
                result = json.loads(raw)
                links = response.headers.get("Link", "")
        except HTTPError as exc:
            # Never echo request headers, provider response bodies, or credentials.
            raise ReadinessError(f"GitHub HTTP {exc.code}; no retry was attempted.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ReadinessError("GitHub evidence was unavailable or malformed; no retry was attempted.") from None
        return result, links

    def get(self, path, params=None):
        result, _ = self._read(path, params)
        require(isinstance(result, dict), "Expected a GitHub JSON object.")
        return result

    def get_array_page(self, path, params):
        # Array endpoints have no total_count; retain continuation evidence.
        result, links = self._read(path, params)
        require(isinstance(result, list), "Expected a GitHub JSON array.")
        next_page = False
        for link in links.split(",") if links else []:
            match = re.fullmatch(r'\s*<[^>]+>;\s*rel="(next|prev|first|last)"\s*', link)
            require(match is not None, "Malformed GitHub pagination Link header.")
            next_page = next_page or match[1] == "next"
        return result, next_page


def positive_int(value):
    return type(value) is int and value > 0


def collection(reader, path, key, params=None):
    """Exhaust bounded pagination; never treat a partial inventory as evidence."""
    found = []
    ids = set()
    expected_count = None
    for page in range(1, MAX_PAGES + 1):
        data = reader.get(path, {**(params or {}), "per_page": 100, "page": page})
        count, items = data.get("total_count"), data.get(key)
        require(type(count) is int and 0 <= count <= MAX_PAGES * 100,
                "GitHub collection has an invalid or excessive total_count.")
        require(expected_count is None or expected_count == count,
                "GitHub collection changed during pagination.")
        expected_count = count
        require(isinstance(items, list) and len(items) <= 100, "Malformed GitHub collection.")
        for item in items:
            require(isinstance(item, dict) and positive_int(item.get("id")), "Malformed GitHub item.")
            require(item["id"] not in ids, "Duplicate GitHub item; pagination is incomplete.")
            ids.add(item["id"])
            found.append(item)
        require(len(found) <= count, "GitHub collection count is inconsistent.")
        if len(found) == count:
            return found
        require(bool(items), "GitHub pagination ended before the declared total.")
    raise ReadinessError("GitHub pagination limit reached before completing the inventory.")


def check_tip(reader, sha):
    repository = reader.get(ROOT)
    require(repository.get("full_name") == REPOSITORY and repository.get("default_branch") == "main",
            "The expected repository must have main as its default branch.")
    branch = reader.get(ROOT + "/branches/main")
    require(branch.get("name") == "main" and branch.get("commit", {}).get("sha") == sha,
            "Requested revision is not the current default-branch tip.")


def array_collection(reader, path, params):
    found = []
    numbers = set()
    for page in range(1, MAX_PAGES + 1):
        items, has_next = reader.get_array_page(path, {**params, "per_page": 100, "page": page})
        require(isinstance(items, list) and len(items) <= 100, "Malformed GitHub alert page.")
        require(type(has_next) is bool, "Missing GitHub alert pagination evidence.")
        require(bool(items) or not has_next, "Empty alert page has an unexpected continuation.")
        for item in items:
            require(isinstance(item, dict) and positive_int(item.get("number")), "Malformed GitHub alert.")
            require(item["number"] not in numbers, "Duplicate alert; pagination is incomplete.")
            numbers.add(item["number"])
            found.append(item)
        if not has_next:
            # A full last page could be truncated; fetch the next numbered page.
            if len(items) < 100:
                return found
    raise ReadinessError("GitHub alert pagination limit reached before completing the inventory.")


def inspect_codeql_results(reader, sha):
    checks = collection(reader, ROOT + f"/commits/{sha}/check-runs", "check_runs",
                        {"check_name": "CodeQL", "filter": "latest"})
    for check in checks:
        app = check.get("app", {})
        owner = app.get("owner", {})
        # GitHub's CodeQL result app, verified from an actual repository result.
        # Actions analysis jobs are checked separately and cannot substitute here.
        require(check.get("name") == "CodeQL" and check.get("head_sha") == sha,
                "CodeQL result identity does not match the requested revision.")
        require(app.get("id") == 57789 and app.get("slug") == "github-advanced-security"
                and owner.get("login") == "github" and owner.get("id") == 9919,
                "CodeQL result was emitted by an untrusted app.")
        require(check.get("status") == "completed" and check.get("conclusion") == "success",
                "CodeQL result check has not completed successfully.")
    # A main commit may have no separate result check. Always inspect open alerts
    # on the guarded default branch; absence of a check is never an alert waiver.
    # https://docs.github.com/en/rest/code-scanning/code-scanning#list-code-scanning-alerts-for-a-repository
    alerts = array_collection(reader, ROOT + "/code-scanning/alerts",
                              {"ref": "refs/heads/main", "state": "open", "tool_name": "CodeQL"})
    require(not alerts, "Unresolved CodeQL alerts on main block publication.")
    return {"source_revision": sha, "result_check_ids": [check["id"] for check in checks],
            "separate_result_emitted": bool(checks), "alert_ref": "refs/heads/main",
            "open_alert_count": 0, "tool_name": "CodeQL"}


def validate_run(run, workflow_id, path, sha):
    require(run.get("workflow_id") == workflow_id and run.get("path") == path,
            "Run does not belong to the expected workflow file and ID.")
    require(run.get("event") == "push" and run.get("head_branch") == "main"
            and run.get("head_sha") == sha,
            "Run is not a push for the requested main revision.")
    require(run.get("repository", {}).get("full_name") == REPOSITORY
            and run.get("head_repository", {}).get("full_name") == REPOSITORY,
            "Run belongs to an unexpected repository.")
    require(positive_int(run.get("id")) and positive_int(run.get("run_number"))
            and positive_int(run.get("run_attempt")), "Run identity is incomplete.")


def successful(run):
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "Latest required workflow run has not completed successfully.")


def inspect_workflow(reader, filename, expected_jobs, sha):
    path = ".github/workflows/" + filename
    workflow = reader.get(ROOT + "/actions/workflows/" + filename)
    workflow_id = workflow.get("id")
    require(positive_int(workflow_id) and workflow.get("path") == path
            and workflow.get("state") == "active", "Expected workflow is absent, changed, or inactive.")
    runs = collection(reader, ROOT + f"/actions/workflows/{workflow_id}/runs", "workflow_runs",
                      {"event": "push", "branch": "main", "head_sha": sha})
    require(bool(runs), "No push run exists for the required workflow at this source revision.")
    for run in runs:
        validate_run(run, workflow_id, path, sha)
    run = max(runs, key=lambda item: (item["run_number"], item["id"]))
    successful(run)
    run_id, attempt = run["id"], run["run_attempt"]
    jobs = collection(reader, ROOT + f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    for name in expected_jobs:
        matched = [job for job in jobs if job.get("name") == name]
        require(len(matched) == 1, f"Expected exactly one successful job: {name}.")
        job = matched[0]
        require(job.get("run_id") == run_id and job.get("head_sha") == sha
                and job.get("run_attempt") == attempt, "Job identity does not match the selected run attempt.")
        successful(job)
    # A rerun starting during job enumeration must not reuse old successful jobs.
    current = reader.get(ROOT + f"/actions/runs/{run_id}")
    validate_run(current, workflow_id, path, sha)
    require(current["id"] == run_id and current["run_attempt"] == attempt,
            "Workflow run attempt changed during verification.")
    successful(current)
    latest_runs = collection(reader, ROOT + f"/actions/workflows/{workflow_id}/runs", "workflow_runs",
                             {"event": "push", "branch": "main", "head_sha": sha})
    require(bool(latest_runs), "Workflow run evidence disappeared during verification.")
    for candidate in latest_runs:
        validate_run(candidate, workflow_id, path, sha)
    latest = max(latest_runs, key=lambda item: (item["run_number"], item["id"]))
    require(latest["id"] == run_id and latest["run_attempt"] == attempt,
            "Latest workflow run changed during verification.")
    successful(latest)
    return {"workflow": path, "workflow_id": workflow_id, "run_id": run_id,
            "run_attempt": attempt, "source_revision": sha,
            "required_jobs": list(expected_jobs),
            "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"}


def check_readiness(reader, sha):
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha),
            "Requested revision must be an exact lowercase 40-character Git SHA.")
    check_tip(reader, sha)
    evidence = [inspect_workflow(reader, name, jobs, sha) for name, jobs in WORKFLOWS.items()]
    codeql_results = inspect_codeql_results(reader, sha)
    check_tip(reader, sha)
    return {"eligible": True, "evidence_class": "MEASURED", "workflows": evidence,
            "codeql_results": codeql_results,
            "boundary": "Read-only CI observation; publication, runtime and scientific validation are separate."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    result = {"schema": "szl.seismic-release-readiness/v1", "repository": REPOSITORY,
              "source_revision": args.sha,
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "command": ["python", "scripts/check_release_readiness.py", "--sha", args.sha]}
    try:
        result.update(check_readiness(GitHubReader(os.environ.get("GH_TOKEN", "")), args.sha))
    except ReadinessError as exc:
        result.update(eligible=False, evidence_class="BLOCKED", reason=str(exc))
    except (AttributeError, KeyError, TypeError, ValueError):
        result.update(eligible=False, evidence_class="BLOCKED", reason="GitHub evidence was malformed.")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
