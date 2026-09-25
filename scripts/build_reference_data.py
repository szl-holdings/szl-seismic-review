"""Extract the pinned Meng et al. Japan panel without copying their code.

Run after downloading the three named Zenodo archives into --archive-dir.
The output is a small attributed research fixture, never a waveform substitute.
"""

import argparse
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path


RECORD = "https://doi.org/10.5281/zenodo.22059219"
ARCHIVES = {
    "seisforge_catalogues.zip": "5f4850c4e313876ce74016f07d21a49b",
    "seisforge_panel.zip": "0665486a527779c0256b98740e2cfc6a",
    "seisforge_verdicts.zip": "b476d5396c39361febc632c63364f3a1",
}
FEATURES = (
    "n_pha_total",
    "n_sta",
    "moveout_rms",
    "az_gap",
    "sec_az_gap",
    "dist_nearest_sta_km",
    "mag",
)
PANEL_MEMBER = "panel/bench/japan_forearc/panel/sample_200.csv"
FEATURE_MEMBER = "catalogues/bench/japan_forearc/quality/association_features_all.csv"
VERDICT_MEMBER = "verdicts/verdicts_japan_forearc.csv"


def checksum(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_rows(path: Path, member: str):
    with zipfile.ZipFile(path) as archive:
        with io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="") as handle:
            yield from csv.DictReader(handle)


def build(archive_dir: Path, output_dir: Path) -> None:
    for name, expected in ARCHIVES.items():
        path = archive_dir / name
        observed = checksum(path, "md5")
        if observed != expected:
            raise ValueError(f"checksum mismatch for {name}: {observed}")

    panel = list(archive_rows(archive_dir / "seisforge_panel.zip", PANEL_MEMBER))
    if len(panel) != 200 or len({row["panel_id"] for row in panel}) != 200:
        raise ValueError("expected 200 unique Japan panel IDs")
    ids = {row["event_id"] for row in panel}
    quality = {
        row["event_id"]: row
        for row in archive_rows(archive_dir / "seisforge_catalogues.zip", FEATURE_MEMBER)
        if row["event_id"] in ids
    }
    if len(quality) != 200:
        raise ValueError(f"expected 200 joined feature rows, got {len(quality)}")
    verdicts = list(archive_rows(archive_dir / "seisforge_verdicts.zip", VERDICT_MEMBER))
    panel_ids = {row["panel_id"] for row in panel}
    if len(verdicts) != 800 or any(row["panel_id"] not in panel_ids for row in verdicts):
        raise ValueError("expected four published verdicts per Japan panel event")
    by_id = {panel_id: set() for panel_id in panel_ids}
    for verdict in verdicts:
        if verdict["verdict"] not in {"real", "uncertain", "false"}:
            raise ValueError("unknown published verdict")
        by_id[verdict["panel_id"]].add(verdict["reviewer"])
    if any(len(reviewers) != 4 for reviewers in by_id.values()):
        raise ValueError("expected four distinct reviewers per panel event")

    output_dir.mkdir(parents=True, exist_ok=True)
    fields = ["panel_id", "event_id", "source_catalog", "time", "lat", "lon", "dep", *FEATURES]
    with (output_dir / "japan_panel.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for case in sorted(panel, key=lambda row: row["panel_id"]):
            record = {key: case.get(key, "") for key in fields}
            feature_row = quality[case["event_id"]]
            for key in FEATURES:
                value = feature_row.get(key)
                if value is None or value == "":
                    raise ValueError(f"missing {key} for {case['panel_id']}")
                record[key] = value
            writer.writerow(record)

    with (output_dir / "japan_verdicts.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["panel_id", "reviewer", "verdict"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(verdicts, key=lambda row: (row["panel_id"], row["reviewer"])))

    manifest = {
        "source": RECORD,
        "source_version": "1.1.0",
        "source_license": "CC BY 4.0",
        "source_authors": ["Lingsen Meng", "Hui Huang", "Jinzhi Ma", "Yang Ma"],
        "archive_md5": ARCHIVES,
        "members": [PANEL_MEMBER, FEATURE_MEMBER, VERDICT_MEMBER],
        "derived_sha256": {
            name: checksum(output_dir / name, "sha256")
            for name in ("japan_panel.csv", "japan_verdicts.csv")
        },
        "waveforms_included": False,
    }
    (output_dir / "source_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    args = parser.parse_args()
    build(args.archive_dir, args.output_dir)
