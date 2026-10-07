---
title: SZL Seismic Review
emoji: 🌐
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
license: apache-2.0
short_description: Evidence-bound earthquake catalogue review research pilot
---

# SZL Seismic Review

An original SZL Holdings Python service and browser dashboard for studying **human confirmability of machine-catalogue earthquake detections**. The layout takes visual cues from the user-provided [BOb dashboard](https://bob.bzzzbx.com/dashboard.html); no BOb code or assets are included. The model and data path are based on a pinned, attributed [Meng et al. EarthArXiv preprint](https://eartharxiv.org/repository/view/14864/) and its [Zenodo dataset, version 1.1.0](https://doi.org/10.5281/zenodo.22059219).

This is a **research pilot**. It does not determine whether an earthquake physically occurred, predict earthquakes, issue a hazard forecast, or validate a model for another region. The existing [a11oy seismic module](https://github.com/szl-holdings/a11oy/blob/main/a11oy_seismic.py) forecasts aftershock rates from USGS data; this repository handles a separate catalogue-quality question. `szl-lambda-gate` is not a scientific scoring dependency.

## What works

- A checksum-verified, 200-event NE Japan forearc panel with 800 published reviewer verdicts, displayed with attribution and a three-state consensus.
- A seven-feature logistic model trained on the 164 panel cases with a resolved plurality (139 confirmed, 25 rejected). Thirty-six cases stay unresolved. Five-fold out-of-fold AUC is **0.850** (bootstrap interval **0.771–0.912**) and Brier score **0.102** on the *same panel*. A three-feature baseline yields AUC **0.839** and Brier **0.101**; the seven-feature fit does not show a clear gain in this pilot.
- A FastAPI backend, responsive dashboard, read-only model-scoring endpoint, source manifest, and reproducible training script.
- A source-custodian CSV import and reviewer-scoped blind workflow. Each reviewer has a distinct bearer credential; their identity comes from that credential, never a request field. Event metadata and the waveform source stay hidden from each reviewer until their own verdict is locked. The custodian can attach five station traces in the browser, and duplicate reviewer submissions are blocked. These traces are **operator-supplied and not independently authenticated by the service**.
- An atomic, locally hash-chained receipt for every import, waveform attachment, and verdict. These receipts are **UNSIGNED**, and local SQLite durability and administrator resistance are **UNVERIFIED**. They are not Khipu or independent authorization proofs.
- A public read-only mode by default. The Hugging Face Space does not enable writes or store new review data.

The published archive does **not** contain continuous waveform traces, so the 200 source-panel cases cannot be freshly reviewed here. Their published verdicts are shown as source data, never as a new SZL adjudication.

## Run locally

Python 3.12 or newer is required (the pinned numpy 2.5.3 and scipy 1.18.1 in `requirements-dev.txt` need it). From this directory:

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
python -m uvicorn app:app --host 127.0.0.1 --port 7860
```

Open `http://127.0.0.1:7860`. The service is read-only unless credentials are configured. Set `SZL_REVIEW_WRITE_TOKEN` for the source custodian's import and waveform attachment actions, and `SZL_REVIEWER_TOKENS_JSON` to a JSON object mapping each reviewer ID to a **distinct** bearer token. The service refuses duplicate reviewer tokens or a reviewer token equal to the custodian token. Set `SZL_REVIEW_DB_PATH` to controlled, durable storage before retaining real reviews. Keep credentials in a secret manager; do not put them in this repository, a URL, or browser storage. The UI holds entered tokens only in current page memory. A custodian who sees the source CSV is not an independent blind reviewer; use separate people and stronger identity and storage controls before a formal multi-person study.

The import CSV header is:

```text
event_id,n_pha_total,n_sta,moveout_rms,az_gap,sec_az_gap,dist_nearest_sta_km,mag
```

Optional `time,lat,lon,dep` fields are hidden during blind review. Azimuth gaps are in degrees, nearest-station distance in kilometres, and move-out RMS in seconds. Imports from outside the fitted Japan region abstain; values beyond the training feature ranges also abstain. Any score on a new import is labelled **unvalidated import** until a suitable independent panel and local calibration exist.

### Input contract v1 (opt-in)

`/api/score` accepts an optional `input_contract` object. Without it, requests and responses are unchanged. With it, the service checks the features against `INPUT_CONTRACT_V1` in `app.py` before scoring:

```json
{
  "region": "japan_forearc",
  "features": {"n_pha_total": 27, "n_sta": 20, "moveout_rms": 0.5594, "az_gap": 214.0,
               "sec_az_gap": 50.8, "dist_nearest_sta_km": 39.455, "mag": 0.87},
  "input_contract": {
    "version": "szl.seismic-input/v1",
    "units": {"n_pha_total": "count", "n_sta": "count", "moveout_rms": "s", "az_gap": "deg",
              "sec_az_gap": "deg", "dist_nearest_sta_km": "km", "mag": "magnitude"}
  }
}
```

| Feature | Unit token | Accepted values |
|---|---|---|
| `n_pha_total`, `n_sta` | `count` | integer, at least 0 |
| `moveout_rms` | `s` | real, at least 0 |
| `az_gap`, `sec_az_gap` | `deg` | real, 0 to 360 |
| `dist_nearest_sta_km` | `km` | real, at least 0 |
| `mag` | `magnitude` | real; the magnitude scale is undeclared in the source fixture |

- Each declared unit must equal its token exactly. A missing unit is `UNIT_UNDECLARED`; any other value, such as `ms`, `mi`, `rad`, `seconds` or `Mw`, is `UNIT_MISMATCH`. The service refuses mismatches and never converts units.
- Values must be finite JSON numbers (not booleans) or plain decimal strings: an optional leading minus, ASCII digits and an optional fraction, with no exponent, whitespace, underscore, `inf` or `nan`. Other forms are `NON_NUMERIC`, a fractional count is `NON_INTEGER_COUNT`, and a value outside the range above is `DOMAIN`.
- Any violation returns `score_status` `INPUT_CONTRACT_VIOLATION` with `confirmability_score: null` and the list of violations. A conforming request is scored exactly as it would be without the key, and the response gains an `input_contract` block. An unknown `version` or a non-object `input_contract` is rejected with HTTP 422.
- Three relations hold in all 200 published rows: `n_pha_total <= 2 * n_sta`, `sec_az_gap <= az_gap`, and `az_gap + sec_az_gap <= 360`. A request that breaks one gets an advisory (`PHASES_EXCEED_TWO_PER_STATION`, `SECONDARY_GAP_EXCEEDS_PRIMARY` or `GAPS_SUM_EXCEEDS_360`) that never changes the status. These relations are data-observed, not physical law. The `sec_az_gap` relations match a "second-largest gap" reading, which is **UNVERIFIED** against the source pipeline's code; the common secondary-gap definition (the largest gap after removing one station) is never smaller than the primary gap.
- Contract conformance is not scientific qualification. A conforming input can still fall outside the training support (three published rows conform and still abstain), and the per-feature training box is not joint support. `/api/catalogues/import` and requests without the key do not use the contract.

## Rebuild the research fixture and model

The committed small CSV fixture and model artifact are enough to run the app. To independently rebuild the fixture, download the three named archives from [Zenodo record 22059219](https://zenodo.org/records/22059219) into a local archive directory, then run:

```bash
python scripts/build_reference_data.py --archive-dir path/to/archives
python scripts/train_model.py
```

The extraction script checks each archive's MD5 against the pinned Zenodo record. `data/source_manifest.json` holds the resulting SHA-256 checksums. The model script verifies those checksums before fitting and emits a chained, **UNSIGNED** training receipt in `models/receipts/` with dataset, model, harness, seed, observed CPU, training loss, and same-panel evaluation. The service checks the committed receipt's hashes at startup. Re-running the script emits another receipt, so do it only when intentionally recording a new run. Archive files are not committed or uploaded to the Space. See [RESEARCH.md](RESEARCH.md) for method, limitations, and primary-source comparisons.

## API and deployment

`/healthz`, `/api/meta`, `/api/summary`, `/api/detections`, `/api/detections/{id}` and `/api/score` provide read access. Imported case detail requires either the source-custodian token or that reviewer's token. `/api/catalogues/import` and `/api/detections/{id}/waveform` require the custodian token; `/api/reviews` requires a reviewer token and derives the reviewer ID from it. `/api/receipts` lets the custodian check local receipt-chain self-consistency without writing on GET. `/api/docs` is the interactive API reference. The public Space is deliberately read-only.

`Dockerfile` serves port 7860 for Hugging Face Spaces. No application credential is required to use the public read-only viewer. The local operator database is excluded from Git and the Docker build. Publication requires a separate Hugging Face publisher credential.

### Source-bound Space publication

[`.github/workflows/hf-sync.yml`](.github/workflows/hf-sync.yml) is the sole application-file writer for `SZLHOLDINGS/szl-seismic-review`. It runs only by manual dispatch on this repository's `main` branch and calls the SHA-pinned central Dockerfile publisher. The publisher receives the exact dispatch commit, refuses a commit that is no longer the current default-branch tip, derives the payload from Dockerfile `COPY` sources, and retains the deployment manifest and verification reports as run artifacts. Pruning and forced restarts are disabled. The workflow does not select or upgrade hardware.

Before the first dispatch, provision a **public Docker Space on CPU Basic** named `SZLHOLDINGS/szl-seismic-review` and make a target-scoped `HF_TOKEN` available to this repository's Actions through a repository or selected-organization secret. The central publisher requires an existing Space; it does not create one. Local CLI login does not establish Actions access. Leave `SZL_REVIEW_WRITE_TOKEN` and `SZL_REVIEWER_TOKENS_JSON` unset on this public Space.

After the exact merged source has passed both CI and the CodeQL result gate, dispatch **Publish Seismic Review to Hugging Face** on `main`. The publisher generates an untracked `SOURCE_REVISION` file, mirrors the exact source bytes, sets and reads back `SZL_SOURCE_REVISION`, and checks the immutable Hub revision, running revision, payload hashes, and the declared application routes. `/api/build-info` reports a source match only when the baked file and runtime variable agree; it does not mint a receipt on GET. Keep the resulting GitHub run, source SHA, Hub SHA, deployment manifest, and runtime probe together in the release record. A workflow definition or a successful source test alone does not establish publication or runtime verification.

For a local Docker build from a clean checkout, create the same untracked ASCII source file explicitly before building:

```bash
python -c "import pathlib,subprocess; pathlib.Path('SOURCE_REVISION').write_bytes(subprocess.check_output(['git','rev-parse','HEAD']).strip()+b'\n')"
docker build -t szl-seismic-review .
```

The generated file is ignored by Git. A local build without a matching `SZL_SOURCE_REVISION` environment variable reports its source binding as **UNKNOWN**. Local development with `python -m uvicorn` does not require this file.

## Attribution and licenses

Project code and original UI are Apache-2.0 under [LICENSE](LICENSE). The derived panel and verdict CSVs in `data/` are attributed to Lingsen Meng, Hui Huang, Jinzhi Ma, and Yang Ma under the source deposition's **CC BY 4.0** terms; see [data/SOURCE_LICENSE.md](data/SOURCE_LICENSE.md) and the source manifest. The study is a preprint and its authors' performance figures are not SZL validation results.
