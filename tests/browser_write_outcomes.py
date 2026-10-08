"""SIMULATED browser writes: acknowledgements survive failed follow-up reads.

The underlying localhost service has no write credentials. Every browser POST is
intercepted and fulfilled inside this suite; no write reaches the service. Dummy
tokens and case records are test fixtures, not real credentials or observations.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import traceback
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright, expect

from browser_smoke import ROOT, check, ready, wait_for_server


CASE_ID = "import:simulated-write-outcome"
HASH = "a" * 64
DUMMY_CUSTODIAN = "SIMULATED-CUSTODIAN-NOT-A-CREDENTIAL"
DUMMY_REVIEWER = "SIMULATED-REVIEWER-NOT-A-CREDENTIAL"
WAVEFORM = {"source_uri": "urn:simulated:browser-test", "sample_rate_hz": 20,
            "stations": [{"index": index, "samples": [0, 1, -1, 0]} for index in range(5)]}


def card(kind: str) -> dict:
    return {"id": CASE_ID, "event_id": None, "catalogue": "Blind import",
            "region": "Undisclosed during review", "origin": "OPERATOR_IMPORT",
            "published_consensus": None, "review_count": None,
            "waveform_ready": kind == "review", "score_status": "BLIND_REVIEW_PENDING",
            "confirmability_score": None}


async def scenario(context, base_url: str, output: Path, report: dict, kind: str,
                   *, failed_post: bool = False, failed_detail: bool = False) -> None:
    state = {"acknowledged": False, "post_count": 0, "post_rejected": failed_post,
             "summary_failed": True, "detail_failed": failed_detail,
             "detail_reads_after_ack": 0, "get_count": 0}
    name = f"{kind}-{'rejected-post-retry' if failed_post else 'acknowledged'}-{'detail-and-summary-failure' if failed_detail else 'summary-failure'}"
    page = await context.new_page()
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    async def intercept(route):
        request = route.request
        path = unquote(urlsplit(request.url).path)
        if request.method == "POST":
            state["post_count"] += 1
            expected = {"review": "/api/reviews", "waveform": f"/api/detections/{CASE_ID}/waveform", "import": "/api/catalogues/import"}[kind]
            assert path == expected, f"Unexpected simulated write: {path}"
            if kind == "review":
                assert request.post_data_json["detection_id"] == CASE_ID
                assert "reviewer_id" not in request.post_data_json
            if state["post_rejected"]:
                await route.fulfill(status=503, json={"detail": "SIMULATED write rejected before commit"})
                return
            state["acknowledged"] = True
            response = {"review": {"saved": True, "detection_id": CASE_ID},
                        "waveform": {"waveform_sha256": HASH},
                        "import": {"imported": 1, "source_sha256": HASH}}[kind]
            await route.fulfill(status=200, json=response)
            return
        state["get_count"] += 1
        if path == "/api/meta":
            response = await route.fetch()
            meta = await response.json()
            meta.update(import_enabled=True, review_write_enabled=True, storage_state="LOCAL_FILE_UNVERIFIED_DURABILITY")
            await route.fulfill(response=response, json=meta)
        elif path == "/api/summary" and state["acknowledged"] and state["summary_failed"]:
            await route.fulfill(status=503, json={"detail": "SIMULATED summary unavailable"})
        elif path == "/api/detections":
            await route.fulfill(json={"total": 1, "items": [card(kind)], "offset": 0, "limit": 20})
        elif path == f"/api/detections/{CASE_ID}":
            if state["acknowledged"]:
                state["detail_reads_after_ack"] += 1
                if state["detail_failed"]:
                    await route.fulfill(status=503, json={"detail": "SIMULATED detail unavailable"})
                    return
            # Deliberately stale blind read verifies that an acknowledged write
            # cannot be re-enabled by a lagging read projection.
            detail = {**card(kind), "blind": True, "waveform_sha256": HASH if kind == "review" else None,
                      "waveform": {key: value for key, value in WAVEFORM.items() if key != "source_uri"} if kind == "review" else None}
            await route.fulfill(json=detail)
        else:
            await route.continue_()

    await page.route(f"{base_url}/api/**", intercept)
    try:
        with check(report, name, fixture="SIMULATED_WRITES_AND_READ_FAILURES"):
            await page.goto(base_url, wait_until="domcontentloaded")
            await ready(page)
            await page.locator("#operator-token").fill(DUMMY_CUSTODIAN)
            if kind == "import":
                await page.locator("#import-catalogue").fill("simulated-browser-catalogue")
                await page.locator("#import-file").set_input_files({"name": "simulated.csv", "mimeType": "text/csv", "buffer": b"event_id,n_sta\nsimulated,5\n"})
                action = page.locator("#import-button")
                feedback = page.locator("#import-feedback")
            else:
                await page.locator(f'[data-record="{CASE_ID}"]').click()
                await expect(page.locator("#detail-content")).to_have_attribute("aria-busy", "false")
                if kind == "review":
                    await page.locator("#reviewer-token").fill(DUMMY_REVIEWER)
                    await page.locator("#open-reviewer-case").click()
                    await expect(page.locator("#detail-content")).to_have_attribute("aria-busy", "false")
                    await page.locator("#review-note").fill("SIMULATED browser regression")
                    action = page.locator('[data-verdict="confirmed"]')
                    feedback = page.locator("#review-feedback")
                else:
                    await page.locator("#waveform-file").set_input_files({"name": "simulated.json", "mimeType": "application/json", "buffer": json.dumps(WAVEFORM).encode()})
                    action = page.locator("#attach-waveform")
                    feedback = page.locator("#waveform-feedback")

            if failed_post:
                await action.click()
                await expect(feedback).to_contain_text("SIMULATED write rejected before commit")
                await expect(action).to_be_enabled()
                assert state["post_count"] == 1 and state["acknowledged"] is False
                assert await page.locator("#acknowledged-writes").count() == 0
                state["post_rejected"] = False

            await action.click()
            acknowledged = page.locator("#import-feedback" if kind == "import" else "#acknowledged-writes")
            notice = {"review": "Verdict locked", "waveform": "Five traces attached and locked", "import": "Imported 1 records"}[kind]
            await expect(acknowledged).to_contain_text(notice)
            await expect(acknowledged).to_contain_text("Summary counts could not be refreshed")
            await expect(acknowledged).to_contain_text("Retry refresh only")
            expected_posts = 2 if failed_post else 1
            assert state["post_count"] == expected_posts
            if kind == "import":
                await expect(page.locator("#import-button")).to_be_disabled()
                assert await page.locator("#import-file").evaluate("element => element.files.length") == 0
                await expect(acknowledged).to_contain_text(HASH)
            else:
                assert state["detail_reads_after_ack"] >= 1, "Summary failure must not prevent independent detail refresh"
                if failed_detail:
                    await expect(acknowledged).to_contain_text("Case detail could not be refreshed")
                    await expect(page.locator("#detail-content")).to_contain_text("SIMULATED detail unavailable")
                else:
                    await expect(action).to_be_disabled()
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True, animations="disabled")

            state["summary_failed"] = False
            state["detail_failed"] = False
            get_count = state["get_count"]
            await page.locator("#retry-import-refresh" if kind == "import" else "#retry-write-refresh").click()
            await expect(acknowledged).to_contain_text(notice)
            await expect(acknowledged).not_to_contain_text("could not be refreshed")
            assert state["get_count"] > get_count
            assert state["post_count"] == expected_posts, "Refresh retry must issue GETs only"
            if kind == "import":
                await expect(page.locator("#import-button")).to_be_disabled()
            else:
                await expect(action).to_be_disabled()
            assert not page_errors, page_errors
            report["scenarios"].append({"name": name, "post_count": state["post_count"],
                                        "get_count": state["get_count"], "all_posts_intercepted": True})
    except Exception:
        await page.screenshot(path=str(output / f"{name}-failure.png"), full_page=True, animations="disabled")
        raise
    finally:
        await page.close()


async def run_browser(base_url: str, output: Path, report: dict, channel: str | None) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**({"channel": channel} if channel else {}))
        report["browser"] = {"name": channel or "Chromium", "version": browser.version}
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            context.set_default_timeout(15_000)
            for kind in ("review", "waveform", "import"):
                await scenario(context, base_url, output, report, kind)
                await scenario(context, base_url, output, report, kind, failed_post=True)
                if kind != "import":
                    await scenario(context, base_url, output, report, kind, failed_detail=True)
            await context.close()
        finally:
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/browser-write-outcomes")
    parser.add_argument("--browser-channel", default=None)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "szl.seismic.browser-write-outcomes/v1", "evidence_class": "SIMULATED",
              "started_at": datetime.now(timezone.utc).isoformat(), "checks": [], "scenarios": [], "passed": False,
              "scope": "Intercepted write acknowledgements and failed GETs in a browser; not storage durability or deployment",
              "source_revision_declared": os.environ.get("GITHUB_SHA")}
    process = None
    exit_code = 1
    with tempfile.TemporaryDirectory(prefix="szl-write-browser-", dir=output) as directory, (output / "server.log").open("w", encoding="utf-8") as log:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            env = os.environ.copy()
            env.update({"SZL_REVIEW_DB_PATH": str(Path(directory) / "review.sqlite3"),
                        "SZL_REVIEW_WRITE_TOKEN": "", "SZL_REVIEWER_TOKENS_JSON": "{}"})
            env.pop("SZL_SOURCE_REVISION", None)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            report["health"] = wait_for_server(process, base_url)
            asyncio.run(run_browser(base_url, output, report, args.browser_channel))
            report["passed"] = True
            exit_code = 0
        except Exception:
            report["error"] = traceback.format_exc()
            print(report["error"], file=sys.stderr)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            (output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"SIMULATED write-outcome evidence: {output}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
