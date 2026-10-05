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

## Rebuild the research fixture and model

The committed small CSV fixture and model artifact are enough to run the app. To independently rebuild the fixture, download the three named archives from [Zenodo record 22059219](https://zenodo.org/records/22059219) into a local archive directory, then run:

```bash
python scripts/build_reference_data.py --archive-dir path/to/archives
python scripts/train_model.py
```

The extraction script checks each archive's MD5 against the pinned Zenodo record. `data/source_manifest.json` holds the resulting SHA-256 checksums. The model script verifies those checksums before fitting and emits a chained, **UNSIGNED** training receipt in `models/receipts/` with dataset, model, harness, seed, observed CPU, training loss, and same-panel evaluation. The service checks the committed receipt's hashes at startup. Re-running the script emits another receipt, so do it only when intentionally recording a new run. Archive files are not committed or uploaded to the Space. See [RESEARCH.md](RESEARCH.md) for method, limitations, and primary-source comparisons.

## API and deployment

`/healthz`, `/api/meta`, `/api/summary`, `/api/detections`, `/api/detections/{id}` and `/api/score` provide read access. Imported case detail requires either the source-custodian token or that reviewer's token. `/api/catalogues/import` and `/api/detections/{id}/waveform` require the custodian token; `/api/reviews` requires a reviewer token and derives the reviewer ID from it. `/api/receipts` lets the custodian check local receipt-chain self-consistency without writing on GET. `/api/docs` is the interactive API reference. The public Space is deliberately read-only.

`Dockerfile` serves port 7860 for Hugging Face Spaces. No deployment secret is required for the public read-only viewer. The local operator database is excluded from Git and the Docker build. This project has its own CI and source history in GitHub `szl-holdings`; the Hugging Face Space is a published projection of an exact source commit, with verification described in the release record.

## Attribution and licenses

Project code and original UI are Apache-2.0 under [LICENSE](LICENSE). The derived panel and verdict CSVs in `data/` are attributed to Lingsen Meng, Hui Huang, Jinzhi Ma, and Yang Ma under the source deposition's **CC BY 4.0** terms; see [data/SOURCE_LICENSE.md](data/SOURCE_LICENSE.md) and the source manifest. The study is a preprint and its authors' performance figures are not SZL validation results.
