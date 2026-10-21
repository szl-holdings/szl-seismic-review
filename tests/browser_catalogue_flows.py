"""Read-only catalogue interactions shared by local and deployed browser checks."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import expect


ROOT = Path(__file__).resolve().parents[1]


def fixture_verdicts() -> dict[str, str]:
    """Derive expected public verdicts from the retained panel votes."""
    votes = defaultdict(Counter)
    with (ROOT / "data/japan_verdicts.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            votes[row["panel_id"]][row["verdict"]] += 1
    result = {}
    for identifier, counts in votes.items():
        leaders = [name for name, count in counts.items() if count == max(counts.values())]
        result[identifier] = ("unresolved" if len(leaders) != 1 or leaders[0] == "uncertain"
                              else "confirmed" if leaders[0] == "real" else "rejected")
    return result


async def save_export(download, output: Path, report: dict, name: str) -> dict:
    path = output / name
    await download.save_as(path)
    raw = path.read_bytes()
    exported = json.loads(raw)
    report.setdefault("catalogue_exports", []).append({
        "file": name, "sha256": hashlib.sha256(raw).hexdigest(),
        "count": exported["count"], "query": exported["query"],
    })
    return exported


async def download_export(page, output: Path, report: dict, name: str) -> dict:
    async with page.expect_download() as pending:
        await page.locator("#export-button").click()
    return await save_export(await pending.value, output, report, name)


async def catalogue_flows(page, context, base_url, output, report, fixture, model,
                          *, check, ready, wait_query, simulate_failures=True):
    """Exercise pagination, filter/export parity and bounded read-failure recovery.

    The injected failures and delayed response run only against the local fixture
    service. Deployed checks use the same real interactions without interception.
    """
    verdicts = fixture_verdicts()
    assert Counter(verdicts.values()) == model["class_counts"]
    published_ids = [f"published:{row['panel_id']}" for row in fixture]
    rejected_ids = [f"published:{row['panel_id']}" for row in fixture
                    if verdicts[row["panel_id"]] == "rejected"]

    async def visible_ids():
        return await page.locator("#detection-rows [data-record]").evaluate_all(
            "elements => elements.map(element => element.dataset.record)")

    async def clear():
        if await page.locator("#clear-filters").is_enabled():
            return await wait_query(page, lambda: page.locator("#clear-filters").click(),
                                    offset=0, q="", catalogue="", consensus="", origin="")
        while await page.locator("#prev-button").is_enabled():
            await wait_query(page, lambda: page.locator("#prev-button").click())
        return await wait_query(page, lambda: page.locator("#refresh-button").click(), offset=0)

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await clear()
    with check(report, "Next and Previous preserve page order and the last page disables Next"):
        first = await wait_query(page, lambda: page.locator("#verdict-filter").select_option("rejected"),
                                 consensus="rejected", offset=0)
        page_size = first["limit"]
        assert first["total"] == len(rejected_ids)
        assert page_size < len(rejected_ids) <= 2 * page_size, "Expected the pinned two-page rejected subset"
        assert await visible_ids() == rejected_ids[:page_size]
        await expect(page.locator("#prev-button")).to_be_disabled()
        last = await wait_query(page, lambda: page.locator("#next-button").click(),
                                consensus="rejected", offset=page_size)
        assert [item["id"] for item in last["items"]] == rejected_ids[page_size:]
        assert await visible_ids() == rejected_ids[page_size:]
        await expect(page.locator("#next-button")).to_be_disabled()
        await expect(page.locator("#page-status")).to_have_text(
            f"Showing {page_size + 1}–{len(rejected_ids)} of {len(rejected_ids)} matching records")
        await wait_query(page, lambda: page.locator("#prev-button").click(), consensus="rejected", offset=0)
        assert await visible_ids() == rejected_ids[:page_size]
        await expect(page.locator("#prev-button")).to_be_disabled()

    with check(report, "Changing a filter from a later page resets the offset"):
        await wait_query(page, lambda: page.locator("#next-button").click(),
                         consensus="rejected", offset=page_size)
        result = await wait_query(page, lambda: page.locator("#verdict-filter").select_option("confirmed"),
                                  consensus="confirmed", offset=0)
        assert result["offset"] == 0 and result["total"] == model["class_counts"]["confirmed"]
        await expect(page.locator("#prev-button")).to_be_disabled()
        assert (await visible_ids())[0] == next(f"published:{row['panel_id']}" for row in fixture
                                               if verdicts[row["panel_id"]] == "confirmed")

    with check(report, "Combined catalogue, verdict and search filters agree with downloaded JSON"):
        await clear()
        chosen = next(row for row in fixture if verdicts[row["panel_id"]] == "confirmed")
        catalogue, query, verdict = chosen["source_catalog"], chosen["event_id"][-2:], "confirmed"
        expected_ids = [f"published:{row['panel_id']}" for row in fixture
                        if row["source_catalog"] == catalogue and verdicts[row["panel_id"]] == verdict
                        and any(query.casefold() in value.casefold() for value in
                                (f"published:{row['panel_id']}", row["event_id"], row["source_catalog"]))]
        assert expected_ids and len(expected_ids) <= page_size
        await wait_query(page, lambda: page.locator("#catalogue-filter").select_option(catalogue), catalogue=catalogue)
        await wait_query(page, lambda: page.locator("#verdict-filter").select_option(verdict),
                         catalogue=catalogue, consensus=verdict)
        await page.locator("#search-query").fill(query)
        result = await wait_query(page, lambda: page.locator("#search-query").press("Enter"),
                                  catalogue=catalogue, consensus=verdict, q=query, offset=0)
        assert [item["id"] for item in result["items"]] == expected_ids
        assert await visible_ids() == expected_ids
        exported = await download_export(page, output, report, "combined-filter-catalogue.json")
        assert exported["query"] == {"catalogue": catalogue, "origin": None, "q": query, "consensus": verdict}
        assert [item["id"] for item in exported["items"]] == expected_ids
        assert exported["count"] == exported["total"] == len(expected_ids)
        assert exported["truncated"] is False and exported["next_offset"] is None

    with check(report, "Empty-result exports are explicit and Imported cases can be cleared"):
        missing = "no-such-catalogue-flow-record"
        await page.locator("#search-query").fill(missing)
        await wait_query(page, lambda: page.locator("#search-query").press("Enter"), q=missing, offset=0)
        await expect(page.locator("#page-status")).to_have_text("0 matching records")
        exported = await download_export(page, output, report, "empty-catalogue.json")
        assert exported["query"]["q"] == missing
        assert exported["items"] == [] and exported["count"] == exported["total"] == 0
        assert exported["truncated"] is False and exported["next_offset"] is None
        await expect(page.locator("#export-feedback")).to_contain_text("Downloaded 0 matching visible cards")
        await clear()
        imported = await wait_query(page, lambda: page.locator("#catalogue-filter").select_option("__imports__"),
                                    origin="imported", offset=0)
        assert imported["total"] == 0 and imported["items"] == [], "Pinned read-only deployment unexpectedly contains imported cases"
        await expect(page.locator("#page-status")).to_have_text("0 matching records")
        await expect(page.locator("#prev-button")).to_be_disabled()
        await expect(page.locator("#next-button")).to_be_disabled()
        await clear()
        assert await visible_ids() == published_ids[:page_size]
        await expect(page.locator("#search-query")).to_have_value("")
        await expect(page.locator("#catalogue-filter")).to_have_value("")
        await expect(page.locator("#verdict-filter")).to_have_value("")

    if simulate_failures:
        with check(report, "Export failure creates no download and a retry succeeds", fixture="SIMULATED_READ_FAILURE"):
            downloads = []
            def record_download(download):
                downloads.append(download)

            page.on("download", record_download)

            async def fail_export(route):
                await route.fulfill(status=503, content_type="application/json",
                                    headers={"x-szl-test-fixture": "simulated"},
                                    body=json.dumps({"detail": "Simulated export unavailable"}))

            await page.route("**/api/export*", fail_export, times=1)
            try:
                await page.locator("#export-button").click()
                await expect(page.locator("#export-feedback")).to_have_text("Export failed: Simulated export unavailable")
                await expect(page.locator("#export-button")).to_be_enabled()
                assert not downloads, "A failed export must not create a downloaded file"
                exported = await download_export(page, output, report, "retry-catalogue.json")
                assert exported["count"] == exported["total"] == len(fixture)
                assert [item["id"] for item in exported["items"]] == published_ids
                assert len(downloads) == 1
            finally:
                await page.unroute("**/api/export*", fail_export)
                page.remove_listener("download", record_download)

        with check(report, "Evidence read failure reports UNKNOWN and reload restores artifact evidence", fixture="SIMULATED_READ_FAILURE"):
            response = await context.request.get(f"{base_url}/api/evidence")
            assert response.ok
            evidence = await response.json()

            async def fail_evidence(route):
                await route.fulfill(status=503, content_type="application/json",
                                    headers={"x-szl-test-fixture": "simulated"},
                                    body=json.dumps({"detail": "Simulated evidence unavailable"}))

            await page.route("**/api/evidence", fail_evidence, times=1)
            try:
                await page.locator("#evidence-refresh").click()
                await expect(page.locator("#evidence-status")).to_contain_text("Evidence unavailable: Simulated evidence unavailable")
                await expect(page.locator("#model-evidence")).to_contain_text("UNKNOWN")
                await expect(page.locator("#runtime-evidence")).to_contain_text("UNKNOWN")
                await expect(page.locator("#artifact-evidence")).to_have_text("Artifact hashes unavailable.")
                await expect(page.locator("#evidence-refresh")).to_be_enabled()
                await page.locator("#evidence-refresh").click()
                await expect(page.locator("#evidence-status")).to_contain_text("Evidence record loaded")
                await expect(page.locator("#model-evidence")).to_contain_text(evidence["model"]["evidence_class"])
                await expect(page.locator("#artifact-evidence")).to_contain_text(evidence["model"]["sha256"])
            finally:
                await page.unroute("**/api/evidence", fail_evidence)

        with check(report, "An in-flight export keeps its original filters while the visible view changes", fixture="SIMULATED_RESPONSE_ORDER_WITH_REAL_PUBLIC_API"):
            await wait_query(page, lambda: page.locator("#verdict-filter").select_option("confirmed"),
                             consensus="confirmed", offset=0)
            captured, release = asyncio.Event(), asyncio.Event()

            async def delayed_export(route):
                response = await route.fetch()
                captured.set()
                await asyncio.wait_for(release.wait(), timeout=15)
                await route.fulfill(response=response)

            await page.route("**/api/export*", delayed_export, times=1)
            try:
                async with page.expect_download() as pending:
                    await page.locator("#export-button").click()
                    await asyncio.wait_for(captured.wait(), timeout=15)
                    # Catalogue loading may finish while Export remains disabled
                    # for the captured request; do not wait on the export button.
                    async with page.expect_response(lambda response:
                            urlsplit(response.url).path == "/api/detections"
                            and parse_qs(urlsplit(response.url).query).get("consensus") == ["rejected"]
                            and parse_qs(urlsplit(response.url).query).get("offset") == ["0"]) as changed:
                        await page.locator("#verdict-filter").select_option("rejected")
                    assert (await changed.value).ok
                    await expect(page.locator("#catalogue-results")).to_have_attribute("aria-busy", "false")
                    assert await visible_ids() == rejected_ids[:page_size]
                    await expect(page.locator("#export-button")).to_be_disabled()
                    release.set()
                exported = await save_export(await pending.value, output, report, "in-flight-confirmed-catalogue.json")
                assert exported["query"]["consensus"] == "confirmed"
                assert exported["count"] == model["class_counts"]["confirmed"]
                assert all(item["published_consensus"] == "confirmed" for item in exported["items"])
                await expect(page.locator("#verdict-filter")).to_have_value("rejected")
                assert await visible_ids() == rejected_ids[:page_size]
                await expect(page.locator("#export-feedback")).to_contain_text("Requested view: consensus=confirmed")
                await ready(page)
            finally:
                release.set()
                await page.unroute("**/api/export*", delayed_export)

    await clear()
