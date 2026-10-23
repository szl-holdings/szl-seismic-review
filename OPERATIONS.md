# Retained review activation

This is a low-volume research pilot. Configuration is **DECLARED**. Runtime observations are **MEASURED** only when their harness actually runs. Acceptance cases and waveforms are **SAMPLE** data. Named automation principals do not establish independent human review or scientific qualification.

## Store and acknowledgment

`review_store.py` uses the fixed private dataset `SZLHOLDINGS/szl-seismic-review-state`. SQLite runs in process memory. Each transaction loads an immutable snapshot, validates schema, constraints, byte hash and receipt chain, then commits the changed database and manifest together with the observed parent revision. A successful write requires one new receipt and exact immutable commit readback before acknowledgment. A stale parent produces a conflict; an ambiguous provider outcome is **UNKNOWN**, reconciled once by complete snapshot identity, never automatically resubmitted. There is no local fallback.

The database is capped at 8 MiB. Snapshot history accumulates on the provider and is not bounded by that cap; monitor quota and traffic before increasing use. This design does not establish a provider availability SLA, administrator resistance, independent signatures, or disaster recovery after provider/account loss. No claim is made that a mounted object store safely hosts SQLite. Hugging Face documents [mount consistency limits](https://github.com/huggingface/hf-mount#consistency-model) and the SDK's [parent-commit guard](https://huggingface.co/docs/huggingface_hub/en/package_reference/hf_api#huggingface_hub.HfApi.create_commit).

Bootstrap and qualify only the dedicated private target:

```bash
python scripts/provision_review_store.py --output artifacts/store-provision.json
```

The command uses the operator's local provider session. It initializes only a repository created by that invocation, refuses incomplete existing state, and qualifies stale-parent rejection plus immutable byte reads on a unique retained branch. It never installs that operator credential into the runtime. Provider qualification is separate from an application restart test.

## Access configuration

Use a fine-grained provider credential restricted to read/write this state dataset for Space secret `SZL_REVIEW_STATE_TOKEN`. Do not install the publishing credential or administrator session as the state token.

| Space setting | Kind | Value or role |
|---|---|---|
| `SZL_REVIEW_STATE_TOKEN` | Secret | Dedicated private dataset credential |
| `SZL_REVIEW_WRITE_TOKEN` | Secret | Distinct cryptographically generated custodian bearer token |
| `SZL_REVIEWER_TOKENS_JSON` | Secret | Object mapping named reviewer IDs to distinct bearer tokens |
| `SZL_REVIEW_CUSTODIAN_ID` | Variable | Explicit custodian identifier, distinct from reviewer IDs |
| `SZL_REVIEW_STORE` | Variable | `private-hub-cas-v1` |
| `SZL_REVIEW_ACTIVATION` | Variable | `read-only` for recovery reads, `active` to admit writes |

Tokens stay in a secret manager and current UI memory only. `/api/access` verifies the selected role. Custodians can import and attach source traces; reviewers can lock only their own verdict. A locked verdict reveals source information only to that reviewer. Public search/export retains blind imported records. Receipts are **UNSIGNED** and GET never creates one. The acceptance principals are service identities; assign distinct human-owned credentials before a formal independent reviewer study.

## Release and live qualification

1. Pass CI and CodeQL at the exact merged source. Use the existing sole `hf-sync.yml` publisher on `main`, with explicit `mode=active` after configuring the store and access. Retain the publish manifest, source SHA, Hub revision, and runtime/browser artifacts.
2. From a clean checkout of that same source, run the public runtime, browser, and write-guard checks with `--mode active`.
3. Load the custodian and two reviewer credentials into process environment without printing them, then run:

```bash
python scripts/verify_live_operator.py --sha <exact-source-SHA> --output-dir artifacts/operator-live --phase write
```

The fixed-host harness creates two labelled SAMPLE cases, attaches distinct five-station waveforms, records two separate reviewers' verdicts on one case, and keeps the second case blind. It checks wrong-role denial, duplicate prevention, complete payload/receipt hashes, public blindness, and exactly five new receipts relative to the pre-run baseline. Lost acknowledgments are UNKNOWN; inspect retained progress and current receipts before deciding what happened. Never rerun the write phase blindly.

4. Request an actual Hugging Face Space restart. Retain provider observations, wait for the same deployed Hub revision, then run the GET-only phase using the same output directory:

```bash
python scripts/verify_live_operator.py --sha <exact-source-SHA> --output-dir artifacts/operator-live --phase readback
```

The readback requires a different process boot ID, unchanged source and Hub revisions, identical case/waveform/verdict state, and unchanged receipt count/head. It sends no writes. Save the before/after reports; an HTTP 200 or RUNNING state alone is insufficient. SAMPLE records remain identifiable in the private snapshot history.

## Recovery

Set `SZL_REVIEW_ACTIVATION=read-only` to stop new writes while retaining authenticated recovery reads. On a 503 with UNKNOWN outcome, compare custodian receipt and case reads before retrying an action. Do not rewrite the main dataset to clear an error or initialize an empty local database. Investigate the exact immutable snapshot and preserve failed evidence. Credential rotation, restoring an older snapshot, or changing the data schema requires an explicit migration/recovery procedure; automatic history pruning and rollback are not implemented.
