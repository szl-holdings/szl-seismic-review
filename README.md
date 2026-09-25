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
- An operator-only CSV import and blinded review workflow. Event metadata and the waveform source are hidden until that reviewer submits a verdict. Five station traces are required before a fresh review can be recorded; duplicate reviewer submissions are locked. These traces are **operator-supplied and not independently authenticated by the service**.
- A public read-only mode by default. The Hugging Face Space does not enable writes or store new review data.

The published archive does **not** contain continuous waveform traces, so the 200 source-panel cases cannot be freshly reviewed here. Their published verdicts are shown as source data, never as a new SZL adjudication.

## Run locally

Python 3.11 is required. From this directory:

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
python -m uvicorn app:app --host 127.0.0.1 --port 7860
```

Open `http://127.0.0.1:7860`. The service is read-only unless `SZL_REVIEW_WRITE_TOKEN` is set in the process environment. Operator writes also need an SQLite file path via `SZL_REVIEW_DB_PATH` on storage you control. Do not put the token in this repository, in a URL, or in browser storage. The UI keeps the token only in current page memory. For multi-user deployment, replace the shared operator token with individual authenticated identities and durable, backed-up storage before accepting real reviewer submissions.

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

The extraction script checks each archive's MD5 against the pinned Zenodo record. `data/source_manifest.json` holds the resulting SHA-256 checksums. The model script verifies those checksums before fitting. The archive files are not committed or uploaded to the Space. See [RESEARCH.md](RESEARCH.md) for method, limitations, and primary-source comparisons.

## API and deployment

`/healthz`, `/api/meta`, `/api/summary`, `/api/detections`, `/api/detections/{id}` and `/api/score` provide read access. `/api/catalogues/import`, `/api/detections/{id}/waveform` and `/api/reviews` require `Authorization: Bearer <operator token>` when operator mode is configured. `/api/docs` is the interactive API reference.

`Dockerfile` serves port 7860 for Hugging Face Spaces. No deployment secret is required for the public read-only viewer. The local operator database is excluded from Git and the Docker build. This project has its own CI and source history in GitHub `szl-holdings`; the Hugging Face Space is a published projection of an exact source commit, with verification described in the release record.

## Attribution and licenses

Project code and original UI are Apache-2.0 under [LICENSE](LICENSE). The derived panel and verdict CSVs in `data/` are attributed to Lingsen Meng, Hui Huang, Jinzhi Ma, and Yang Ma under the source deposition's **CC BY 4.0** terms; see [data/SOURCE_LICENSE.md](data/SOURCE_LICENSE.md) and the source manifest. The study is a preprint and its authors' performance figures are not SZL validation results.
