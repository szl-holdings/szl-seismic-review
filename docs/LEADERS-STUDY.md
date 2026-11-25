# Leaders Study: szl-seismic-review

**STATUS: PROPOSE-ONLY DOCUMENT.** This file adds information only. It changes no code, policy, configuration, credential, or receipt. All absorb candidates (A1-A4) are UNAPPROVED and each needs its own gated lap. A5 and A6 are OPEN questions; this study takes no side. Zero merges. The repo's own doctrine is unchanged and preserved verbatim.

## What this repo is (from its own receipts, not asserted here)

szl-seismic-review is an evidence-bound research pilot for studying human confirmability of machine-catalogue earthquake detections. Its own README bounds it precisely: it does NOT determine whether an earthquake physically occurred, does not predict earthquakes, does not issue a hazard forecast, and does not validate a model for another region. Measured surface: a checksum-verified 200-event NE Japan forearc panel with 800 published reviewer verdicts (Meng et al. EarthArXiv preprint, Zenodo dataset v1.1.0, CC BY 4.0 attribution); a seven-feature logistic model on the 164 resolved cases (139 confirmed, 25 rejected, 36 unresolved) with five-fold out-of-fold AUC 0.850 (bootstrap 0.771-0.912) and Brier 0.102 on the SAME panel; a three-feature baseline AUC 0.839, Brier 0.101, with no clear gain claimed for the seven-feature fit; reviewer-scoped blind workflow withUNSIGNED hash-chained receipts (administrator resistance UNVERIFIED, not Khipu, not independent authorization proofs); same-panel evaluation REPORTED, independent replay UNAVAILABLE; operator-supplied station traces NOT independently authenticated; input contract v1 with exact unit tokens (refuses conversion); Mahalanobis joint-support proxy advisory-only, never changing score status. This study repeats only what the repo already records.

## Domain leaders studied (2026-11-24, web-verified)

### L1. Meng et al. - the pinned source study (EarthArXiv repository/view/14864, Zenodo 10.5281/zenodo.22059219)

The leader this pilot is built from: "Can human seismologists verify machine-learning-detected earthquakes?" and its published reviewer-verdict dataset. The repo is already leader-grade here: the source is pinned, checksummed, attributed, and its authors' performance figures are explicitly "not SZL validation results"; published verdicts are shown "as source data, never as a new SZL adjudication." Gap: the EarthArXiv object is a preprint with version bumps (multiple download objects observed); the repo pins version 1.1.0 of the Zenodo dataset but the preprint's own revision drift is not receipted. Absorb candidate **A1 (UNAPPROVED): a docs-only source-version note recording the pinned preprint object IDs and checking whether the seven-feature choice still matches the preprint's current version - documentation only, no re-derivation, no new claim.**

### L2. Fleiss (1971) - nominal-scale agreement among many raters (Psychological Bulletin 76(5), doi 10.1037/h0031619)

The leader in chance-corrected inter-rater agreement for nominal (three-state) verdicts by MANY distinct raters, which is exactly this repo's shape: 800 published reviewer verdicts across a reviewer panel, three states (confirmed / rejected / unresolved). Gap: the repo reports consensus counts (164 resolved of 200) but no chance-corrected agreement statistic for verdict agreement, so "how much reviewers agree beyond chance" is unmeasured in this pilot's receipts. Absorb candidate **A2 (UNAPPROVED): a docs-only note on whether the published verdict panel admits a Fleiss-kappa-style statistic (it requires the same set of raters per item - which the published panel may not satisfy), including an honest UNAVAILABLE conclusion if the data shape does not support it. No computation, no number asserted.**

### L3. Guo et al. - on calibration of modern neural networks (arXiv 1706.04599)

The leader in calibration measurement: reliability diagrams and expected calibration error (ECE) as standard instruments for whether a model's confidence means what it says. Gap: this repo reports Brier 0.102 (a proper score) but no reliability diagram or ECE-class statistic for the logistic scorer on the 164-case panel. Absorb candidate **A3 (UNAPPROVED, docs-only): a study note defining what a reliability diagram on the same-panel predictions would and would NOT show (n=164 caution, same-panel not independent, REPORTED not validated) with no recomputation and no new metric claim.**

### L4. Wilkinson et al. - FAIR guiding principles (Scientific Data 3:160018, 2016)

The leader in data stewardship: Findable, Accessible, Interoperable, Reusable. This repo is unusually strong: SHA-256 source manifest as production reads, MD5-checked archive rebuild path, pinned Zenodo DOI, per-file SOURCE_LICENSE.md, exported JSON with attribution and evidence class, machine-readable metadata. Gap: no self-assessed FAIR crosswalk exists in-repo. Absorb candidate **A4 (UNAPPROVED): a docs-only FAIR crosswalk of EXISTING artifacts (source_manifest.json, SOURCE_LICENSE.md, /api/evidence, /api/export) with each cell pointing to an already-committed artifact and honestly marking what is absent - a crosswalk, not a certification, not a badge claim.**

## Open questions (no sides taken)

- **A5 (OPEN):** Hosted custody and writes require the private Hub store, named access credentials, and explicit activation (OPERATIONS.md). Whether and when to provision and activate that store is owner terrain; this study only notes the condition. No recommendation is implied.
- **A6 (OPEN):** The relationship between this catalogue-quality pilot and the a11oy seismic module (aftershock-rate forecasting from USGS data) is declared separate by the README; whether the estate ever wants them reconciled, cross-referenced, or kept permanently separate is an estate-level question a leaders study cannot adjudicate.

## Doctrine preservation (verbatim obligations)

Research pilot, not a physical-occurrence adjudicator; no prediction, no hazard forecast, no other-region validation; published verdicts are source data, never new SZL adjudication; receipts UNSIGNED, administrator resistance UNVERIFIED; same-panel results REPORTED, independent replay UNAVAILABLE; operator-supplied traces NOT independently authenticated; input contract never converts units; Mahalanobis proxy advisory-only, never causes abstention and never certifies joint support; contract conformance is not scientific qualification; unvalidated imports abstain or are labelled unvalidated. This document changes none of these.

## Sources

- Pinned source study: https://eartharxiv.org/repository/view/14864/ and https://doi.org/10.5281/zenodo.22059219 (v1.1.0)
- Fleiss 1971: doi 10.1037/h0031619, Psychological Bulletin 76(5), 378-382
- Guo et al. 2017: https://arxiv.org/abs/1706.04599
- Wilkinson et al. 2016: https://www.nature.com/articles/sdata201618

*Part of the estate's OUROBOROS audit program; spaces-audit slice. Propose-only; the author lane makes no activation, publication, or merge decision.*
