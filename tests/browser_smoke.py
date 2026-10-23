"""Read-only browser checks against local or exact-source live Seismic Review.

Run explicitly with ``python tests/browser_smoke.py``. This is not a pytest unit
test. Screenshots, the downloaded public export, JSON results and server logs are
retained even on failure. No custodian/reviewer credentials or POSTs are used.
Use --live --sha <merge-sha> for the canonical public Space without response mocks.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
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
from browser_catalogue_flows import catalogue_flows


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


async def select_published_scope(page, expected_mode):
    if expected_mode == "active":
        return await wait_query(page, lambda: page.locator("#catalogue-filter").select_option("__published__"),
                                origin="published", offset=0)
    return None


async def smoke(base_url: str, output: Path, report: dict, *, live: bool = False,
                browser_channel: str | None = None, expected_mode: str = "read-only") -> None:
    assert expected_mode in ("read-only", "active")
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
        browser = await playwright.chromium.launch(**({"channel": browser_channel} if browser_channel else {}))
        report["browser"] = {"name": browser_channel or "Chromium", "version": browser.version}
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        context.set_default_timeout(15_000)

        async def guard_request(route):
            if route.request.method not in ("GET", "HEAD", "OPTIONS"):
                write_requests.append({"url": route.request.url, "method": route.request.method})
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await context.route("**/*", guard_request)
        page = await context.new_page()
        page.on("pageerror", lambda error: browser_errors.append(str(error)))

        def record_response(response):
            if response.status >= 400:
                item = {"url": response.url, "status": response.status}
                if not live and response.headers.get("x-szl-test-fixture") == "simulated":
                    report.setdefault("simulated_http_errors", []).append(item)
                else:
                    http_errors.append(item)

        page.on("response", record_response)
        page.on("request", lambda request: write_requests.append({"url": request.url, "method": request.method}) if request.method not in ("GET", "HEAD", "OPTIONS") else None)
        try:
            with check(report, "Dashboard loads the pinned panel and expected write configuration"):
                response = await page.goto(base_url, wait_until="domcontentloaded")
                assert response is not None and response.ok
                await ready(page)
                await expect(page.locator("#count-panel")).to_have_text(str(len(fixture)))
                if expected_mode == "read-only":
                    await expect(page.locator("#write-state")).to_have_text("Read only")
                    await expect(page.locator("#import-button")).to_be_disabled()
                    await expect(page.locator("#detection-rows [data-record]")).to_have_count(20)
                    await expect(page.locator("#page-status")).to_contain_text(f"of {len(fixture)} matching records")
                else:
                    await expect(page.locator("#write-state")).to_have_text("Custody + review enabled")
                    await expect(page.locator("#storage-state")).to_have_text("Remote saved state · DECLARED")
                    await expect(page.locator("#storage-explanation")).to_contain_text("receipts remain unsigned")

            with check(report, "Skip link transfers keyboard focus to the main landmark"):
                await page.keyboard.press("Tab")
                await expect(page.locator(".skip-link")).to_be_focused()
                await page.keyboard.press("Enter")
                await expect(page.locator("#overview")).to_be_focused()

            with check(report, "Published panel scope excludes mutable imported cases"):
                result = await wait_query(page, lambda: page.locator("#catalogue-filter").select_option("__published__"),
                                          origin="published", offset=0)
                assert result["total"] == len(fixture)
                assert all(item["origin"] == "PUBLISHED_PANEL" for item in result["items"])
                await expect(page.locator("#detection-rows [data-record]")).to_have_count(20)
                if expected_mode == "read-only":
                    await wait_query(page, lambda: page.locator("#clear-filters").click(), offset=0)

            with check(report, "Canonical primary action remains readable at rest, hover and keyboard focus"):
                action = page.locator(".hero-actions .btn-primary")
                ratios = {}
                for state in ("rest", "hover", "focus"):
                    if state == "hover":
                        await action.hover()
                    elif state == "focus":
                        await page.mouse.move(0, 0)
                        await action.focus()
                    ratios[state] = await action.evaluate("""element => {
                        const context = document.createElement('canvas').getContext('2d');
                        const luminance = color => {
                            context.clearRect(0, 0, 1, 1); context.fillStyle = color; context.fillRect(0, 0, 1, 1);
                            const data = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3).map(v => v / 255);
                            const linear = data.map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
                            return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
                        };
                        const style = getComputedStyle(element), a = luminance(style.color), b = luminance(style.backgroundColor);
                        return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                    }""")
                    assert ratios[state] >= 4.5, (state, ratios)
                report["primary_action_contrast"] = ratios

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
                await select_published_scope(page, expected_mode)
                await expect(page.locator("#page-status")).to_contain_text(f"of {len(fixture)} matching records")

            if not live:
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
                        await (await old_response.value).body()
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
                await page.screenshot(path=str(output / "desktop-detail.png"), full_page=True, animations="disabled", timeout=60000)
                await page.keyboard.press("Escape")
                await expect(page.locator("#detail-drawer")).to_have_attribute("aria-hidden", "true")
                await expect(opener).to_be_focused()
                assert await page.locator(".shell").evaluate("element => element.inert") is False

            with check(report, "Desktop and mobile catalogue have no document overflow"):
                reset = page.locator("#clear-filters") if await page.locator("#clear-filters").is_enabled() else page.locator("#refresh-button")
                await wait_query(page, reset.click, offset=0)
                await select_published_scope(page, expected_mode)
                for name, width, height in (("desktop", 1440, 1000), ("wide", 1920, 1080), ("tablet", 768, 1024), ("narrow", 320, 800), ("mobile", 390, 844)):
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
                    presentation = await page.evaluate("""() => {
                        const hero = getComputedStyle(document.querySelector('.hero'));
                        return {image: hero.backgroundImage, fill: hero.backgroundColor,
                            controls: [...document.querySelectorAll('.btn,.row-button,.pager,.brand')].filter(e => e.getClientRects().length).map(e => ({text: e.textContent.trim(), height: e.getBoundingClientRect().height}))};
                    }""")
                    assert "radial-gradient(" in presentation["image"] and presentation["fill"] != "rgba(0, 0, 0, 0)", presentation
                    assert all(item["height"] >= 43.99 for item in presentation["controls"]), presentation
                    report.setdefault("presentation", []).append({"viewport": name, **presentation})
                    await expect(page.locator("#search-query")).to_be_visible()
                    await expect(page.locator("#verdict-filter")).to_be_visible()
                    await expect(page.locator("#export-button")).to_be_visible()
                    await page.screenshot(path=str(output / f"{name}.png"), full_page=True, animations="disabled", timeout=60000)
                await page.locator("#detection-rows [data-record]").first.click()
                await expect(page.get_by_role("dialog", name="Detection detail")).to_be_visible()
                await expect(page.locator("#detail-content")).to_have_attribute("aria-busy", "false")
                box = await page.locator("#detail-drawer").bounding_box()
                assert box and box["width"] <= 391
                await page.locator("#close-drawer").click()

            with check(report, "Evidence scroller is named, focusable and keyboard-scrollable on mobile"):
                scroller = page.get_by_role("region", name="Detection catalogue")
                await scroller.focus()
                await expect(scroller).to_be_focused()
                await page.keyboard.press("ArrowRight")
                await page.wait_for_function("document.querySelector('#catalogue-results').scrollLeft > 0")

            with check(report, "Forced colors provide a solid hero and reduced motion stops smooth scrolling"):
                await page.emulate_media(forced_colors="active", reduced_motion="reduce")
                paint = await page.locator(".hero").evaluate("element => ({image: getComputedStyle(element).backgroundImage, fill: getComputedStyle(element).backgroundColor})")
                assert paint["image"] == "none" and paint["fill"] != "rgba(0, 0, 0, 0)", paint
                assert await page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto"
                await page.emulate_media(forced_colors="none")

            await catalogue_flows(page, context, base_url, output, report, fixture, model,
                                  check=check, ready=ready, wait_query=wait_query,
                                  simulate_failures=not live, expected_mode=expected_mode)

            with check(report, "Browser completed without page errors, unexpected HTTP errors or writes"):
                assert not browser_errors, browser_errors
                assert not http_errors, http_errors
                assert not write_requests, write_requests
        except Exception:
            try:
                await page.screenshot(path=str(output / "failure.png"), full_page=True, animations="disabled", timeout=60000)
            except Exception as error:
                report["failure_screenshot_error"] = str(error)
            raise
        finally:
            await context.close()
            await browser.close()


def runtime_observation(source: str, report: dict, expected_mode: str = "read-only") -> None:
    spec = importlib.util.spec_from_file_location("live_runtime", ROOT / "scripts/verify_deployment.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    report.update(expected_source=source, checks=[])
    probe.verify(source, probe.PublicReader(), report, expected_mode=expected_mode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/browser")
    parser.add_argument("--browser-channel", default=None)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--mode", choices=("read-only", "active"), default="read-only")
    parser.add_argument("--sha", help="Exact checked-out source deployed to the canonical Space")
    args = parser.parse_args()
    if args.live != bool(args.sha):
        parser.error("--live and --sha must be supplied together")
    if args.mode == "active" and not args.live:
        parser.error("--mode active requires --live and the exact --sha")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "szl.seismic.browser-smoke/v1", "evidence_class": "UNKNOWN",
              "started_at": datetime.now(timezone.utc).isoformat(), "checks": [], "passed": False,
              "scope": "Local read-only browser behavior against pinned fixtures; not deployment or scientific validation",
              "source_revision_declared": args.sha or os.environ.get("GITHUB_SHA"), "python": sys.version,
              "expected_mode": args.mode,
              "frontend_sha256_basis": "Local checkout bytes; live served hashes are recorded in runtime_before and runtime_after",
              "frontend_sha256": {name: file_sha256(ROOT / name) for name in ("frontend/index.html", "frontend/app.js", "frontend/app.css", "frontend/szl/SOURCE.json", "frontend/szl/szl-design-system.css", "frontend/szl/szl-console.css")}}
    if args.live:
        report["scope"] = "Public GET-only UI behavior with no response mocks; source and running revision verified before and after. Same operator, no independent scientific replication."
        report["application_url"] = "https://szlholdings-szl-seismic-review.hf.space"
        try:
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            if head != args.sha:
                raise RuntimeError("Live verification requires the exact source checkout")
            subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "app.py", "review_store.py", "frontend", "data", "models", "Dockerfile", "requirements.txt"], cwd=ROOT, check=True, capture_output=True)
            report["runtime_before"] = {}
            runtime_observation(args.sha, report["runtime_before"], args.mode)
            asyncio.run(smoke(report["application_url"], output, report, live=True,
                              browser_channel=args.browser_channel, expected_mode=args.mode))
            report["runtime_after"] = {}
            runtime_observation(args.sha, report["runtime_after"], args.mode)
            if report["runtime_before"] != report["runtime_after"]:
                raise RuntimeError("Runtime identities changed during the browser checks")
            report.update(passed=True, evidence_class="MEASURED")
        except Exception:
            report.update(evidence_class="BLOCKED", error=traceback.format_exc())
            print(report["error"], file=sys.stderr)
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            (output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Live browser evidence: {output}", flush=True)
        return 0 if report["passed"] else 1
    process = None
    exit_code = 1
    with tempfile.TemporaryDirectory(prefix="szl-browser-", dir=output) as directory, (output / "server.log").open("w", encoding="utf-8") as log:
        try:
            with socket.socket() as port_probe:
                port_probe.bind(("127.0.0.1", 0))
                port = port_probe.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("SZL_REVIEW") and key not in ("SPACE_ID", "SZL_SOURCE_REVISION")}
            env.update({"SZL_REVIEW_DB_PATH": str(Path(directory) / "review.sqlite3"),
                        "SZL_REVIEW_WRITE_TOKEN": "", "SZL_REVIEWER_TOKENS_JSON": "{}"})
            command = [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)]
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            report["health"] = wait_for_server(process, base_url)
            report["evidence_class"] = "MEASURED"
            asyncio.run(smoke(base_url, output, report, browser_channel=args.browser_channel))
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
