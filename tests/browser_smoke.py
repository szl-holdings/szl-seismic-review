"""Read-only Chromium checks against a temporary, local Seismic Review service.

Run explicitly with ``python tests/browser_smoke.py``. This is not a pytest unit
test. Screenshots, the downloaded public export, JSON results and server logs are
retained even on failure. No custodian/reviewer credentials or POSTs are used.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def check(report: dict, name: str, *, fixture: str = "PINNED_PUBLIC_PANEL"):
    item = {"name": name, "fixture": fixture, "passed": False}
    report["checks"].append(item)
    try:
        yield
    except Exception as error:
        item["error"] = str(error)
        raise
    else:
        item["passed"] = True
        print(f"PASS: {name}", flush=True)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for_server(process: subprocess.Popen, base_url: str) -> dict:
    deadline = time.monotonic() + 30
    last_error = "Service did not respond"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Uvicorn exited during startup: {process.returncode}")
        try:
            with urlopen(f"{base_url}/healthz", timeout=1) as response:
                health = json.load(response)
            if health.get("fixture_verified") is True:
                return health
            last_error = "Fixture health check was not verified"
        except (URLError, TimeoutError, OSError, ValueError) as error:
            last_error = str(error)
        # Bounded startup polling only; UI tests use DOM/network conditions.
        time.sleep(0.1)
    raise RuntimeError(f"Local service startup timed out: {last_error}")


async def ready(page) -> None:
    await expect(page.locator("#catalogue-results")).to_have_attribute("aria-busy", "false")
    await expect(page.locator("#export-button")).to_be_enabled()


async def wait_query(page, action, **query) -> dict:
    def matches(response):
        url = urlsplit(response.url)
        params = parse_qs(url.query)
        return url.path == "/api/detections" and all(
            params.get(key, [""])[0] == str(value) for key, value in query.items()
        )

    async with page.expect_response(matches) as pending:
        await action()
    response = await pending.value
    assert response.ok, f"Catalogue request failed with {response.status}"
    payload = await response.json()
    await ready(page)
    return payload


async def smoke(base_url: str, output: Path, report: dict) -> None:
    with (ROOT / "data/japan_panel.csv").open(encoding="utf-8", newline="") as handle:
        fixture = list(csv.DictReader(handle))
    model = json.loads((ROOT / "models/japan_assoc_logistic_v1.json").read_text(encoding="utf-8"))
    published_id = f"published:{fixture[0]['panel_id']}"
    first_catalogue = fixture[0]["source_catalog"]
    browser_errors: list[str] = []
    http_errors: list[dict] = []
    write_requests: list[dict] = []
    report["browser_errors"] = browser_errors
    report["http_errors"] = http_errors
    report["write_requests"] = write_requests

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        report["browser"] = {"name": "Chromium", "version": browser.version}
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        context.set_default_timeout(15_000)
        page = await context.new_page()
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on("response", lambda response: http_errors.append({"url": response.url, "status": response.status}) if response.status >= 400 else None)
        page.on("request", lambda request: write_requests.append({"url": request.url, "method": request.method}) if request.method not in ("GET", "HEAD", "OPTIONS") else None)
        try:
            with check(report, "Read-only dashboard loads the pinned panel"):
                response = await page.goto(base_url, wait_until="domcontentloaded")
                assert response is not None and response.ok
                await ready(page)
                await expect(page.locator("#count-panel")).to_have_text(str(len(fixture)))
                await expect(page.locator("#write-state")).to_have_text("Read only")
                await expect(page.locator("#import-button")).to_be_disabled()
                await expect(page.locator("#detection-rows [data-record]")).to_have_count(20)
                await expect(page.locator("#page-status")).to_contain_text(f"of {len(fixture)} matching records")

            with check(report, "Published verdict filters select the reported panel counts"):
                for verdict in ("confirmed", "rejected", "unresolved"):
                    result = await wait_query(page, lambda verdict=verdict: page.locator("#verdict-filter").select_option(verdict), consensus=verdict)
                    expected = model["class_counts"][verdict]
                    assert result["total"] == expected
                    assert all(item["published_consensus"] == verdict for item in result["items"])
                    await expect(page.locator("#page-status")).to_contain_text(f"of {expected} matching records")
                    assert set(await page.locator("#detection-rows tr td:nth-child(4)").all_text_contents()) == {verdict.capitalize()}

            with check(report, "Export captures filtered records across catalogue pages"):
                await wait_query(page, lambda: page.locator("#verdict-filter").select_option("confirmed"), consensus="confirmed")
                async with page.expect_download() as pending_download:
                    await page.locator("#export-button").click()
                download = await pending_download.value
                export_path = output / "confirmed-catalogue.json"
                await download.save_as(export_path)
                exported = json.loads(export_path.read_text(encoding="utf-8"))
                assert exported["schema"] == "szl.seismic.catalogue-export/v1"
                assert exported["evidence_class"] == "DECLARED"
                assert exported["query"]["consensus"] == "confirmed"
                assert exported["count"] == exported["total"] == model["class_counts"]["confirmed"]
                assert exported["count"] > await page.locator("#detection-rows [data-record]").count()
                assert exported["truncated"] is False and exported["next_offset"] is None
                assert all(item["origin"] == "PUBLISHED_PANEL" and item["published_consensus"] == "confirmed" for item in exported["items"])
                assert not any("waveform" in item or "reviews" in item or "metadata" in item for item in exported["items"])
                await expect(page.locator("#export-feedback")).to_contain_text(f"Downloaded {exported['count']} matching visible cards")

            with check(report, "Catalogue filter and explicit search return fixture records"):
                await wait_query(page, lambda: page.locator("#clear-filters").click(), offset=0)
                result = await wait_query(page, lambda: page.locator("#catalogue-filter").select_option(first_catalogue), catalogue=first_catalogue)
                assert result["total"] == sum(row["source_catalog"] == first_catalogue for row in fixture)
                await expect(page.locator("#detection-rows tr td:nth-child(3) .sub")).to_have_text([first_catalogue] * len(result["items"]))
                await page.locator("#search-query").fill(published_id)
                result = await wait_query(page, lambda: page.locator("#search-query").press("Enter"), q=published_id)
                assert [item["id"] for item in result["items"]] == [published_id]
                await expect(page.locator("#detection-rows [data-record]")).to_have_count(1)
                await expect(page.locator("#detection-rows [data-record]")).to_have_attribute("data-record", published_id)

            with check(report, "Empty query state clears back to the full panel"):
                missing_query = "no-such-public-seismic-record"
                await page.locator("#search-query").fill(missing_query)
                await wait_query(page, lambda: page.locator("#search-query").press("Enter"), q=missing_query)
                await expect(page.locator("#page-status")).to_have_text("0 matching records")
                await expect(page.locator("#empty-clear-filters")).to_be_visible()
                await wait_query(page, lambda: page.locator("#empty-clear-filters").click(), offset=0)
                await expect(page.locator("#search-query")).to_be_focused()
                await expect(page.locator("#search-query")).to_have_value("")
                await expect(page.locator("#catalogue-filter")).to_have_value("")
                await expect(page.locator("#page-status")).to_contain_text(f"of {len(fixture)} matching records")

            with check(report, "Older search responses cannot replace the current view", fixture="SIMULATED_RESPONSE_ORDER_WITH_REAL_PUBLIC_API"):
                stale_query = "no-such-slow-public-record"
                captured = asyncio.Event()
                release = asyncio.Event()

                async def delayed_response(route):
                    query = parse_qs(urlsplit(route.request.url).query)
                    if query.get("q") == [stale_query]:
                        response = await route.fetch()
                        captured.set()
                        await asyncio.wait_for(release.wait(), timeout=15)
                        await route.fulfill(response=response)
                    else:
                        await route.continue_()

                await page.route("**/api/detections?*", delayed_response)
                try:
                    await page.locator("#search-query").fill(stale_query)
                    await page.locator("#search-query").press("Enter")
                    await asyncio.wait_for(captured.wait(), timeout=15)
                    await page.locator("#search-query").fill(published_id)
                    await wait_query(page, lambda: page.locator("#search-query").press("Enter"), q=published_id)
                    async with page.expect_response(lambda response: parse_qs(urlsplit(response.url).query).get("q") == [stale_query]) as old_response:
                        release.set()
                    await (await old_response.value).finished()
                    # Yield to the browser rendering loop, not a timing-based sleep.
                    await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                    await expect(page.locator("#detection-rows [data-record]")).to_have_count(1)
                    await expect(page.locator("#detection-rows [data-record]")).to_have_attribute("data-record", published_id)
                    await expect(page.locator("#page-status")).to_contain_text("of 1 matching records")
                finally:
                    release.set()
                    await page.unroute("**/api/detections?*", delayed_response)

            with check(report, "Evidence panel binds to API values and retained artifact bytes"):
                response = await context.request.get(f"{base_url}/api/evidence")
                assert response.ok
                evidence = await response.json()
                assert evidence["schema"] == "szl.seismic.evidence/v1"
                assert evidence["model"]["sha256"] == file_sha256(ROOT / "models/japan_assoc_logistic_v1.json")
                for name, digest in evidence["source"]["files_sha256"].items():
                    assert digest == file_sha256(ROOT / "data" / name)
                await page.locator("#evidence-refresh").click()
                await expect(page.locator("#evidence-status")).to_contain_text("Evidence record loaded")
                await expect(page.locator("#model-evidence")).to_contain_text("REPORTED")
                await expect(page.locator("#runtime-evidence")).to_contain_text(evidence["runtime"]["evidence_class"])
                await expect(page.locator("#paper-link")).to_have_attribute("href", evidence["source"]["paper_url"])
                await expect(page.locator("#source-link")).to_have_attribute("href", evidence["source"]["doi"])
                await page.locator(".hash-details summary").click()
                await expect(page.locator("#artifact-evidence")).to_contain_text(evidence["model"]["sha256"])
                await expect(page.locator("#artifact-evidence")).to_contain_text(evidence["training_receipt"]["chain_head_sha256"])
                await expect(page.locator("#evidence-limits")).to_contain_text("UNSIGNED")
                assert await page.locator("#evidence-limits .badge").all_text_contents() == ["UNAVAILABLE", "UNSIGNED", "UNAVAILABLE", "UNAVAILABLE"]
                (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

            with check(report, "Detection drawer supports keyboard focus, Escape and restoration"):
                opener = page.locator(f'[data-record="{published_id}"]')
                await opener.click()
                drawer = page.get_by_role("dialog", name="Detection detail")
                await expect(drawer).to_be_visible()
                await expect(drawer).to_have_attribute("aria-modal", "true")
                await expect(page.locator("#detail-content")).to_have_attribute("aria-busy", "false")
                await expect(page.locator("#close-drawer")).to_be_focused()
                assert await page.locator(".shell").evaluate("element => element.inert") is True
                await page.keyboard.press("Shift+Tab")
                await expect(page.locator("#detail-content a").last).to_be_focused()
                await page.keyboard.press("Tab")
                await expect(page.locator("#close-drawer")).to_be_focused()
                await expect(page.locator("#detail-content")).to_contain_text(fixture[0]["event_id"])
                await page.screenshot(path=str(output / "desktop-detail.png"), full_page=True, animations="disabled")
                await page.keyboard.press("Escape")
                await expect(page.locator("#detail-drawer")).to_have_attribute("aria-hidden", "true")
                await expect(opener).to_be_focused()
                assert await page.locator(".shell").evaluate("element => element.inert") is False

            with check(report, "Desktop and mobile catalogue have no document overflow"):
                await wait_query(page, lambda: page.locator("#clear-filters").click(), offset=0)
                for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                    await page.set_viewport_size({"width": width, "height": height})
                    await page.locator("#catalogue-heading").scroll_into_view_if_needed()
                    dimensions = await page.evaluate("() => ({scroll: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth})")
                    if dimensions["scroll"] > dimensions["viewport"] + 1:
                        report["overflow_elements"] = await page.evaluate("""() => [...document.querySelectorAll('body *')].map(element => {
                            const box = element.getBoundingClientRect(), style = getComputedStyle(element);
                            return {tag: element.tagName, id: element.id, classes: element.className,
                                    left: box.left, right: box.right, width: box.width,
                                    overflow: style.overflowX, visibility: style.visibility, minWidth: style.minWidth};
                        }).filter(box => box.right > document.documentElement.clientWidth + 1).slice(0, 40)""")
                        print(json.dumps({"viewport": name, "overflow_elements": report["overflow_elements"]}), flush=True)
                    assert dimensions["scroll"] <= dimensions["viewport"] + 1, f"{name} document overflow: {dimensions}"
                    await expect(page.locator("#search-query")).to_be_visible()
                    await expect(page.locator("#verdict-filter")).to_be_visible()
                    await expect(page.locator("#export-button")).to_be_visible()
                    await page.screenshot(path=str(output / f"{name}.png"), full_page=True, animations="disabled")
                await page.locator("#detection-rows [data-record]").first.click()
                await expect(page.get_by_role("dialog", name="Detection detail")).to_be_visible()
                await expect(page.locator("#detail-content")).to_have_attribute("aria-busy", "false")
                box = await page.locator("#detail-drawer").bounding_box()
                assert box and box["width"] <= 391
                await page.locator("#close-drawer").click()

            with check(report, "Browser completed without page errors, HTTP errors or writes"):
                assert not browser_errors, browser_errors
                assert not http_errors, http_errors
                assert not write_requests, write_requests
        except Exception:
            try:
                await page.screenshot(path=str(output / "failure.png"), full_page=True, animations="disabled")
            except Exception as error:
                report["failure_screenshot_error"] = str(error)
            raise
        finally:
            await context.close()
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/browser")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "szl.seismic.browser-smoke/v1", "evidence_class": "UNKNOWN",
              "started_at": datetime.now(timezone.utc).isoformat(), "checks": [], "passed": False,
              "scope": "Local read-only browser behavior against pinned fixtures; not deployment or scientific validation",
              "source_revision_declared": os.environ.get("GITHUB_SHA"), "python": sys.version}
    process = None
    exit_code = 1
    with tempfile.TemporaryDirectory(prefix="szl-browser-") as directory, (output / "server.log").open("w", encoding="utf-8") as log:
        try:
            with socket.socket() as port_probe:
                port_probe.bind(("127.0.0.1", 0))
                port = port_probe.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            env = os.environ.copy()
            env.update({"SZL_REVIEW_DB_PATH": str(Path(directory) / "review.sqlite3"),
                        "SZL_REVIEW_WRITE_TOKEN": "", "SZL_REVIEWER_TOKENS_JSON": "{}"})
            env.pop("SZL_SOURCE_REVISION", None)
            command = [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)]
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            report["health"] = wait_for_server(process, base_url)
            report["evidence_class"] = "MEASURED"
            asyncio.run(smoke(base_url, output, report))
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
    print(f"Browser evidence: {output}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
