"""Real loopback-only operator workflow checks using SAMPLE input data.

No response mocks or live-target option exist. Three temporary credentials are
generated in memory and passed only to the child process and local requests.
The resulting MEASURED execution concerns local application behavior; SAMPLE
traces and verdicts establish neither scientific validity nor hosted durability.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import traceback
from urllib.parse import quote, unquote, urlsplit

from playwright.async_api import async_playwright, expect

from browser_smoke import ROOT, check, ready, wait_for_server


CATALOGUE = "SAMPLE-browser-custody"
REGION = "japan_forearc"
REVIEWERS = ("SAMPLE-reviewer-A", "SAMPLE-reviewer-B")
EVENTS = ("SAMPLE-event-alpha", "SAMPLE-event-beta")
DATA_CLASS = "SAMPLE"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sample_inputs() -> tuple[bytes, dict[str, dict]]:
    fields = ["event_id", "n_pha_total", "n_sta", "moveout_rms", "az_gap",
              "sec_az_gap", "dist_nearest_sta_km", "mag", "time", "lat", "lon", "dep"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    waveforms = {}
    for ordinal, event in enumerate(EVENTS):
        writer.writerow({"event_id": event, "n_pha_total": 10, "n_sta": 5,
                         "moveout_rms": 0.2, "az_gap": 160, "sec_az_gap": 80,
                         "dist_nearest_sta_km": 12, "mag": 1.5,
                         "time": "SAMPLE-NOT-AN-OBSERVATION", "lat": 0, "lon": 0, "dep": 0})
        waveforms[event] = {
            "source_uri": f"sample://local-browser/{event}", "sample_rate_hz": 100,
            "stations": [
                {"samples": [round(math.sin((station + 1) * (sample + 1) * 0.17 + ordinal), 6)
                             for sample in range(64)], "p_offset_s": 0.05, "s_offset_s": 0.25}
                for station in range(5)
            ],
        }
    return output.getvalue().encode("utf-8"), waveforms


def stop_server(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def start_server(env: dict, log):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--no-access-log"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        health = wait_for_server(process, base_url)
    except Exception:
        stop_server(process)
        raise
    return process, base_url, health


async def api(context, base_url, method, path, *, token=None, body=None, expected=200):
    parsed = urlsplit(base_url)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
    assert path.startswith("/api/") and not path.startswith("//")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    options = {"headers": headers, "timeout": 15_000, "max_redirects": 0}
    if body is not None:
        if isinstance(body, bytes):
            headers["Content-Type"] = "text/csv; charset=utf-8"
            options["data"] = body
        else:
            options["data"] = body
    requester = context.request if hasattr(context, "request") else context
    response = await requester.fetch(base_url + path, method=method, **options)
    statuses = {expected} if isinstance(expected, int) else set(expected)
    assert response.status in statuses, f"{method} {path.split('?')[0]} returned {response.status}; expected {sorted(statuses)}"
    return await response.json()


async def receipt_state(context, base_url, custodian, expected_count):
    value = await api(context, base_url, "GET", "/api/receipts", token=custodian)
    assert value["count"] == expected_count and value["chain_self_consistent"] is True
    assert value["signature_status"] == "UNSIGNED"
    return value


async def screenshot(page, output, name):
    await page.screenshot(path=str(output / name), full_page=True, animations="disabled",
                          mask=[page.locator("#operator-token"), page.locator("#reviewer-token")],
                          timeout=60_000)


async def open_reviewer(page, identifier, token):
    if await page.locator("#detail-drawer").get_attribute("aria-hidden") == "false":
        await page.locator("#close-drawer").click()
    await page.locator(f'[data-record="{identifier}"]').click()
    await expect(page.locator("#reviewer-access")).to_be_visible()
    await page.locator("#reviewer-token").fill(token)

    async def matches_credential(response):
        return (unquote(urlsplit(response.url).path) == f"/api/detections/{identifier}"
                and response.request.method == "GET"
                and await response.request.header_value("authorization") == f"Bearer {token}")

    async with page.expect_response(matches_credential) as pending:
        await page.locator("#open-reviewer-case").click()
    response = await pending.value
    assert response.ok
    value = await response.json()
    await expect(page.locator("#detail-content")).to_have_attribute("aria-busy", "false")
    return value


async def workflow(base_url, output, report, credentials, csv_bytes, waveforms, channel):
    custodian, reviewer_a, reviewer_b = credentials
    case_ids, waveform_hashes = {}, {}
    report["browser_errors"], report["external_requests"] = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**({"channel": channel} if channel else {}))
        report["browser"] = {"name": channel or "Chromium", "version": browser.version}
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        context.set_default_timeout(15_000)

        async def loopback_guard(route):
            if urlsplit(route.request.url).netloc != urlsplit(base_url).netloc:
                report["external_requests"].append({"url": route.request.url.split("?")[0],
                                                     "method": route.request.method})
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await context.route("**/*", loopback_guard)
        page = await context.new_page()
        page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
        try:
            with check(report, "Local write capabilities use generated separate credentials", fixture=DATA_CLASS):
                meta = await api(context, base_url, "GET", "/api/meta")
                assert meta["import_enabled"] is True and meta["review_write_enabled"] is True
                report["local_storage_state"] = meta["storage_state"]
                assert (await page.goto(base_url, wait_until="domcontentloaded")).ok
                await ready(page)
                await receipt_state(context, base_url, custodian, 0)

            import_path = "/api/catalogues/import?catalogue=" + CATALOGUE + "&region=" + REGION
            with check(report, "Unauthenticated and reviewer roles cannot import or enumerate custody", fixture=DATA_CLASS):
                for token in (None, reviewer_a, reviewer_b):
                    await api(context, base_url, "POST", import_path, token=token,
                              body=csv_bytes, expected={401, 403})
                    await api(context, base_url, "GET", "/api/custody/detections", token=token, expected={401, 403})
                await receipt_state(context, base_url, custodian, 0)

            with check(report, "Actual UI CSV import commits two SAMPLE events and one receipt", fixture=DATA_CLASS):
                await page.locator("#operator-token").fill(custodian)
                await page.locator("#import-catalogue").fill(CATALOGUE)
                await page.locator("#import-region").fill(REGION)
                await page.locator("#import-file").set_input_files({"name": "SAMPLE-events.csv", "mimeType": "text/csv", "buffer": csv_bytes})
                async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/catalogues/import"
                                                and r.request.method == "POST") as pending:
                    await page.locator("#import-button").click()
                response = await pending.value
                assert response.ok
                imported = await response.json()
                assert imported["imported"] == 2 and imported["source_sha256"] == digest(csv_bytes)
                await expect(page.locator("#import-feedback")).to_contain_text("Imported 2 records")
                assert await page.locator("#import-file").evaluate("element => element.files.length") == 0
                await ready(page)
                report["import"] = imported
                await receipt_state(context, base_url, custodian, 1)

            with check(report, "Actual duplicate-import UI rejection permits a different file without another write", fixture=DATA_CLASS):
                before = await receipt_state(context, base_url, custodian, 1)
                import_requests = []

                def track_import(request):
                    if urlsplit(request.url).path == "/api/catalogues/import" and request.method == "POST":
                        import_requests.append(request.method)

                page.on("request", track_import)
                try:
                    await page.locator("#import-file").set_input_files({
                        "name": "SAMPLE-events-duplicate.csv", "mimeType": "text/csv", "buffer": csv_bytes,
                    })
                    async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/catalogues/import"
                                                    and r.request.method == "POST") as pending:
                        await page.locator("#import-button").click()
                    response = await pending.value
                    assert response.status == 409
                    await expect(page.locator("#import-feedback")).to_contain_text("this CSV was already imported")
                    await expect(page.locator("#import-feedback")).not_to_contain_text("UNKNOWN")
                    await expect(page.locator("#retry-import-refresh")).to_have_count(0)
                    replacement = csv_bytes.replace(EVENTS[0].encode("ascii"), b"SAMPLE-event-not-submitted")
                    assert digest(replacement) != digest(csv_bytes)
                    await page.locator("#import-file").set_input_files({
                        "name": "SAMPLE-different-NOT-SUBMITTED.csv", "mimeType": "text/csv", "buffer": replacement,
                    })
                    await expect(page.locator("#import-button")).to_be_enabled()
                    await page.locator("#import-file").set_input_files([])
                    await expect(page.locator("#import-button")).to_be_disabled()
                    assert (await api(context, base_url, "GET", "/api/detections?origin=imported"))["total"] == 2
                    assert await receipt_state(context, base_url, custodian, 1) == before
                    assert len(import_requests) == 1
                    report["duplicate_import_ui"] = {"http_status": 409, "post_requests": 1,
                                                     "replacement_file_submitted": False,
                                                     "receipt_head_unchanged": True}
                finally:
                    page.remove_listener("request", track_import)

            with check(report, "Authenticated custody UI maps each event to its opaque case ID", fixture=DATA_CLASS):
                async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/custody/detections") as pending:
                    await page.locator("#custody-load").click()
                response = await pending.value
                assert response.ok
                custody = await response.json()
                assert custody["total"] == 2 and len(custody["items"]) == 2
                for item in custody["items"]:
                    assert item["event_id"] in EVENTS and item["catalogue"] == CATALOGUE
                    assert item["source_sha256"] == digest(csv_bytes) and item["waveform_ready"] is False
                    case_ids[item["event_id"]] = item["id"]
                    await expect(page.locator("#custody-rows")).to_contain_text(item["event_id"])
                    await expect(page.locator(f'[data-custody-record="{item["id"]}"]')).to_be_visible()
                assert len(set(case_ids.values())) == 2
                report["sample_case_ids"] = case_ids
                await screenshot(page, output, "SAMPLE-custody-mapping.png")

            with check(report, "Operator token changes clear private custody rows until explicit reload", fixture=DATA_CLASS):
                before = await receipt_state(context, base_url, custodian, 1)
                custody_requests = []

                def track_custody(request):
                    if urlsplit(request.url).path == "/api/custody/detections":
                        custody_requests.append(request.method)

                page.on("request", track_custody)
                try:
                    for changed_token in ("", reviewer_a, custodian):
                        await page.locator("#operator-token").fill(changed_token)
                        await expect(page.locator("[data-custody-record]")).to_have_count(0)
                        for event in EVENTS:
                            await expect(page.locator("#custody-rows")).not_to_contain_text(event)
                        await expect(page.locator("#custody-results")).to_have_attribute("aria-busy", "false")
                    await expect(page.locator("#custody-load")).to_be_enabled()
                    assert not custody_requests, "Changing credentials must not request custody records"
                    async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/custody/detections") as pending:
                        await page.locator("#custody-load").click()
                    response = await pending.value
                    assert response.ok
                    reloaded = await response.json()
                    assert {item["event_id"]: item["id"] for item in reloaded["items"]} == case_ids
                    await expect(page.locator("#custody-results")).to_have_attribute("aria-busy", "false")
                    await expect(page.locator("#custody-status")).to_contain_text("of 2 custody records")
                    await expect(page.locator("[data-custody-record]")).to_have_count(2)
                    assert custody_requests == ["GET"]
                    assert await receipt_state(context, base_url, custodian, 1) == before
                    report["custody_token_invalidation"] = {"automatic_requests": 0,
                                                            "explicit_reload_requests": 1,
                                                            "receipt_head_unchanged": True}
                finally:
                    page.remove_listener("request", track_custody)

            with check(report, "Each mapped case receives its intended distinct five-station traces", fixture=DATA_CLASS):
                for ordinal, event in enumerate(EVENTS):
                    identifier = case_ids[event]
                    await page.locator(f'[data-custody-record="{identifier}"]').click()
                    await expect(page.locator("#waveform-file")).to_be_visible()
                    waveform = waveforms[event]
                    await page.locator("#waveform-file").set_input_files({
                        "name": f"{event}-traces.json", "mimeType": "application/json",
                        "buffer": json.dumps(waveform).encode("utf-8"),
                    })
                    async with page.expect_response(lambda r: unquote(urlsplit(r.url).path) == f"/api/detections/{identifier}/waveform"
                                                    and r.request.method == "POST") as pending:
                        await page.locator("#attach-waveform").click()
                    response = await pending.value
                    assert response.ok
                    attached = await response.json()
                    assert attached["stations"] == 5
                    waveform_hashes[event] = attached["waveform_sha256"]
                    await expect(page.locator("#detail-content")).to_contain_text(attached["waveform_sha256"])
                    stored = await api(context, base_url, "GET", "/api/detections/" + quote(identifier), token=custodian)
                    assert stored["waveform_sha256"] == attached["waveform_sha256"]
                    assert [s["samples"] for s in stored["waveform"]["stations"]] == [s["samples"] for s in waveform["stations"]]
                    assert len({tuple(s["samples"]) for s in stored["waveform"]["stations"]}) == 5
                    await receipt_state(context, base_url, custodian, 2 + ordinal)
                    await page.locator("#close-drawer").click()
                assert len(set(waveform_hashes.values())) == 2
                report["sample_waveform_sha256"] = waveform_hashes

            identifier = case_ids[EVENTS[0]]
            with check(report, "Wrong roles and duplicate import or attachment cannot add receipts", fixture=DATA_CLASS):
                before = await receipt_state(context, base_url, custodian, 3)
                for token in (None, reviewer_a, reviewer_b):
                    await api(context, base_url, "POST", f"/api/detections/{quote(identifier)}/waveform",
                              token=token, body=waveforms[EVENTS[0]], expected={401, 403})
                await api(context, base_url, "POST", "/api/reviews", token=custodian,
                          body={"detection_id": identifier, "verdict": "confirmed"}, expected={401, 403})
                await api(context, base_url, "POST", import_path, token=custodian, body=csv_bytes, expected=409)
                await api(context, base_url, "POST", f"/api/detections/{quote(identifier)}/waveform",
                          token=custodian, body=waveforms[EVENTS[1]], expected=409)
                assert await receipt_state(context, base_url, custodian, 3) == before

            with check(report, "Reviewer A is blind until an actual UI verdict locks and reveals only A", fixture=DATA_CLASS):
                await page.locator("#operator-token").fill("")
                await expect(page.locator("#custody-rows")).not_to_contain_text(EVENTS[0])
                blind = await open_reviewer(page, identifier, reviewer_a)
                assert blind["blind"] is True and "features" not in blind and "metadata" not in blind
                assert "source_uri" not in blind["waveform"]
                await expect(page.locator("#detail-content")).not_to_contain_text(EVENTS[0])
                await page.locator("#review-note").fill("SAMPLE local software workflow; not scientific adjudication.")
                async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/reviews"
                                                and r.request.method == "POST") as pending:
                    await page.locator('[data-verdict="confirmed"]').click()
                response = await pending.value
                assert response.ok
                saved = await response.json()
                assert saved["saved"] is True and saved["reviewer_id"] == REVIEWERS[0]
                await expect(page.locator("#detail-content")).to_contain_text("Reviewed detection " + EVENTS[0])
                await expect(page.locator("#detail-content")).to_contain_text(waveforms[EVENTS[0]]["source_uri"])
                await screenshot(page, output, "SAMPLE-reviewer-A-locked.png")
                report["reviewer_a_receipt"] = saved["receipt"]
                await receipt_state(context, base_url, custodian, 4)

            with check(report, "Reviewer B remains blind after A, then locks a distinct UI verdict", fixture=DATA_CLASS):
                blind = await open_reviewer(page, identifier, reviewer_b)
                assert blind["blind"] is True and "reviews" not in blind and "features" not in blind
                assert "source_uri" not in blind["waveform"]
                await expect(page.locator("#detail-content")).not_to_contain_text(EVENTS[0])
                await expect(page.locator("#detail-content")).not_to_contain_text(REVIEWERS[0])
                await screenshot(page, output, "SAMPLE-reviewer-B-still-blind.png")
                await page.locator("#review-note").fill("SAMPLE disagreement exercises identity isolation only.")
                async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/reviews"
                                                and r.request.method == "POST") as pending:
                    await page.locator('[data-verdict="rejected"]').click()
                response = await pending.value
                assert response.ok
                saved = await response.json()
                assert saved["saved"] is True and saved["reviewer_id"] == REVIEWERS[1]
                await expect(page.locator("#detail-content")).to_contain_text("Reviewed detection " + EVENTS[0])
                report["reviewer_b_receipt"] = saved["receipt"]
                await receipt_state(context, base_url, custodian, 5)

            with check(report, "Locked verdict retries and public GETs preserve the receipt head", fixture=DATA_CLASS):
                before = await receipt_state(context, base_url, custodian, 5)
                for token in (reviewer_a, reviewer_b):
                    await api(context, base_url, "POST", "/api/reviews", token=token,
                              body={"detection_id": identifier, "verdict": "unresolved"}, expected=409)
                for endpoint in ("/api/detections?origin=imported", "/api/export?origin=imported"):
                    public = await api(context, base_url, "GET", endpoint)
                    assert public["total"] == 2
                    for item in public["items"]:
                        assert item["event_id"] is None and item["confirmability_score"] is None
                        assert "waveform" not in item and "reviews" not in item
                    assert not any(event in json.dumps(public) for event in EVENTS)
                revealed = await api(context, base_url, "GET", "/api/detections/" + quote(identifier), token=reviewer_a)
                assert {v["reviewer_id"]: v["verdict"] for v in revealed["reviews"]} == {
                    REVIEWERS[0]: "confirmed", REVIEWERS[1]: "rejected"}
                assert revealed["waveform_sha256"] == waveform_hashes[EVENTS[0]]
                second_blind = await api(context, base_url, "GET", "/api/detections/" + quote(case_ids[EVENTS[1]]), token=reviewer_a)
                assert second_blind["blind"] is True
                report["receipt_before_restart"] = await receipt_state(context, base_url, custodian, 5)
                assert report["receipt_before_restart"] == before
                assert not report["browser_errors"] and not report["external_requests"]
        except Exception:
            try:
                await screenshot(page, output, "SAMPLE-failure.png")
            except Exception:
                report["failure_screenshot_available"] = False
            raise
        finally:
            await context.close()
            await browser.close()
    return case_ids, waveform_hashes


async def verify_restart(base_url, report, credentials, case_ids, waveform_hashes):
    custodian, reviewer_a, reviewer_b = credentials
    async with async_playwright() as playwright:
        context = await playwright.request.new_context(timeout=15_000)
        try:
            with check(report, "A new local server process retains cases, traces, verdicts and receipt head", fixture=DATA_CLASS):
                custody = await api(context, base_url, "GET", "/api/custody/detections", token=custodian)
                assert custody["total"] == 2
                assert {r["event_id"]: r["id"] for r in custody["items"]} == case_ids
                for event, identifier in case_ids.items():
                    for token in (reviewer_a, reviewer_b):
                        value = await api(context, base_url, "GET", "/api/detections/" + quote(identifier), token=token)
                        assert value["waveform_sha256"] == waveform_hashes[event]
                        assert value["blind"] is (event == EVENTS[1])
                        if event == EVENTS[0]:
                            assert {v["reviewer_id"]: v["verdict"] for v in value["reviews"]} == {
                                REVIEWERS[0]: "confirmed", REVIEWERS[1]: "rejected"}
                await api(context, base_url, "POST", "/api/reviews", token=reviewer_a,
                          body={"detection_id": case_ids[EVENTS[0]], "verdict": "unresolved"}, expected=409)
                report["receipt_after_restart"] = await receipt_state(context, base_url, custodian, 5)
                assert report["receipt_after_restart"] == report["receipt_before_restart"]
        finally:
            await context.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT.parent / ("seismic-operator-local-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    parser.add_argument("--browser-channel", default=None)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    credentials = tuple(secrets.token_urlsafe(32) for _ in range(3))
    assert len(set(credentials)) == 3
    csv_bytes, waveforms = sample_inputs()
    report = {
        "schema": "szl.seismic.local-operator-workflow/v1", "evidence_class": "BLOCKED",
        "input_evidence_class": DATA_CLASS, "passed": False, "checks": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Real browser and API requests to an isolated loopback service with SAMPLE events, traces and verdicts; no response mocks.",
        "boundary": "Local process-restart persistence only. No hosted storage durability, external write authority, trace authenticity or independent scientific replication is established.",
        "sample_csv_sha256": digest(csv_bytes),
        "credential_handling": "Three unique generated credentials held in process memory; omitted from artifacts and screenshots.",
        "source_sha256": {name: digest((ROOT / name).read_bytes()) for name in
                          ("app.py", "frontend/app.js", "frontend/index.html", "tests/browser_operator_workflows.py")},
    }
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("SZL_REVIEW_") or name in {"SZL_SOURCE_REVISION", "SPACE_ID"}:
            env.pop(name, None)
    env.update({"SZL_REVIEW_DB_PATH": str(output / "SAMPLE-review.sqlite3"),
                "SZL_REVIEW_WRITE_TOKEN": credentials[0],
                "SZL_REVIEWER_TOKENS_JSON": json.dumps(dict(zip(REVIEWERS, credentials[1:]))),
                "PYTHONDONTWRITEBYTECODE": "1"})
    process = None
    try:
        with (output / "server.log").open("w", encoding="utf-8") as log:
            process, base_url, report["initial_health"] = start_server(env, log)
            report["initial_pid"] = process.pid
            case_ids, hashes = asyncio.run(workflow(base_url, output, report, credentials, csv_bytes,
                                                   waveforms, args.browser_channel))
            stop_server(process)
            process = None
            process, base_url, report["restarted_health"] = start_server(env, log)
            report["restarted_pid"] = process.pid
            assert report["initial_pid"] != report["restarted_pid"]
            asyncio.run(verify_restart(base_url, report, credentials, case_ids, hashes))
            assert all(digest((ROOT / name).read_bytes()) == sha
                       for name, sha in report["source_sha256"].items()), "Source changed during local execution"
            report.update(passed=True, evidence_class="MEASURED")
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        stop_server(process)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        log_path = output / "server.log"
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            for token in credentials:
                log_text = log_text.replace(token, "[REDACTED]")
            log_path.write_text(log_text, encoding="utf-8")
        # Verify the retained local database and other artifacts contain no
        # generated bearer credentials. Tokens are never database values.
        for path in output.iterdir():
            if path.is_file():
                if any(token.encode("ascii") in path.read_bytes() for token in credentials):
                    report.update(passed=False, evidence_class="BLOCKED",
                                  error="Generated credential found in retained artifact " + path.name)
        encoded = json.dumps(report, indent=2) + "\n"
        for token in credentials:
            encoded = encoded.replace(token, "[REDACTED]")
        (output / "results.json").write_text(encoded, encoding="utf-8")
    safe_report = json.loads(encoded)
    if not safe_report["passed"]:
        print(safe_report.get("error", "Local workflow failed"), file=sys.stderr)
    print(json.dumps({"passed": safe_report["passed"], "checks": len(safe_report["checks"]),
                      "evidence_class": safe_report["evidence_class"], "input_evidence_class": DATA_CLASS,
                      "output": str(output)}), flush=True)
    return 0 if safe_report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
