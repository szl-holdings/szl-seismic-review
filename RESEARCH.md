# Earthquake catalogue verification: source study and build decisions

## Research question

The [Meng, Huang, Ma and Ma EarthArXiv preprint](https://eartharxiv.org/repository/view/14864/) asks how much of a machine-learning earthquake catalogue a human seismologist can verify from blind waveform evidence. Its [current PDF](https://eartharxiv.org/repository/object/14864/download/26312/) has DOI [10.31223/X5MZ2D](https://doi.org/10.31223/X5MZ2D). It is a preprint, not peer-reviewed certification. Titles vary across revisions; this project pins the data and code deposit [Zenodo 22059219, version 1.1.0](https://zenodo.org/records/22059219). The [concept DOI](https://doi.org/10.5281/zenodo.22059218) can resolve to a newer version, so it is not used as a reproducibility pin.

The study has four author-reviewers give 4,800 blind verdicts on 1,200 sampled detections from six catalogues. Their options are confirmed, uncertain, and rejected. The reviewers see plots from five nearby stations with phase guides while event identity, origin, location, depth, magnitude, and station identity are hidden. It reports 60.1–97.6% confirmation among resolvable detections by catalogue, after accounting for the study's stratified design. Uncertain/tied verdicts must stay visible as unresolved bounds. A human's inability to verify a weak signal is not proof that the earthquake did not occur.

The paper uses seven association features: total phase count (`n_pha_total`), station count (`n_sta`), travel-time move-out RMS (`moveout_rms`), primary and secondary azimuth gaps (`az_gap`, `sec_az_gap`), nearest station distance in kilometres (`dist_nearest_sta_km`), and magnitude (`mag`). Its Ridgecrest binary logistic fit gives a reported five-fold AUC of 0.946, with 151 resolved panel cases. A separate ridge model ranks reviewer-adjusted latent quality across compatible catalogues. The paper reports ranking transfer to northeast Japan at reviewer-level AUC 0.73–0.79. Those are **paper results**, not results of this code. Its absolute Ridgecrest probability threshold does not transfer as a calibrated threshold elsewhere.

## What SZL built and measured here

The pinned deposit contains panel, feature, verdict, code, catalogue and result archives. This project extracts only the 200-case Japan panel, 800 published verdicts, and seven association features, with archive MD5 and derived SHA-256 checks. We wrote an independent, small logistic fit rather than copying the paper's scripts. Four published votes are reduced to a plurality: a unique `real` winner is confirmed, a unique `false` winner is rejected, and ties or `uncertain` winners are unresolved. This yields 139 confirmed, 25 rejected, and 36 unresolved cases. AUC/Brier use five stratified cross-validation folds with preprocessing fit inside each training fold. The final fit scores the published panel for inspection; its per-case display is **in-sample**. See the model JSON for exact coefficients, training ranges, source hashes, seed, and metrics.

| Japan pilot, same panel | Seven features | Three-feature baseline |
|---|---:|---:|
| Resolved cases | 164 | 164 |
| Five-fold AUC | 0.850 | 0.839 |
| Brier score | 0.102 | 0.101 |
| Five-bin ECE, unweighted | 0.031 | 0.012 |

The seven-feature AUC interval is 0.771–0.912 from bootstrap resampling of out-of-fold predictions. These figures do not show a clear advantage over the smaller baseline and do not validate deployment outside the published Japan panel. The bootstrap does not make the same-panel evaluation an independent test.

## Contribution-led prior art

The following people and open projects define useful comparison points, not a ranking of people or an endorsement of any one package:

| Contribution | Primary research and source |
|---|---|
| Lingsen Meng and collaborators: blind catalogue confirmability audit | [EarthArXiv preprint](https://eartharxiv.org/repository/view/14864/), [Meng group](https://lsmeng.github.io/group.html) |
| Weiqiang Zhu, Gregory Beroza and collaborators: phase picking and catalogue pipelines | [PhaseNet paper](https://doi.org/10.1093/gji/ggy423), [PhaseNet code](https://github.com/AI4EPS/PhaseNet), [GaMMA](https://github.com/AI4EPS/GaMMA), [QuakeFlow](https://github.com/AI4EPS/QuakeFlow) |
| S. Mostafa Mousavi and collaborators: independent detector/picker baseline | [EQTransformer paper](https://www.nature.com/articles/s41467-020-17591-w), [source](https://github.com/smousavi05/EQTransformer) |
| Jannes Münchmeyer and collaborators: shared picker evaluation and high-throughput association | [SeisBench paper](https://doi.org/10.1029/2021JB023499), [toolbox](https://github.com/seisbench/seisbench), [PyOcto paper](https://seismica.library.mcgill.ca/article/view/1130), [PyOcto source](https://github.com/yetinam/pyocto) |
| Ian McBrearty and collaborators: graph association | [GENIE source](https://github.com/imcbrearty/GENIE), [associator comparison](https://seismica.library.mcgill.ca/article/view/1559) |
| Yiyu Ni, Marine Denolle and collaborators: global-scale pick processing | [Seismica study](https://seismica.library.mcgill.ca/article/view/1738) |

PhaseNet, EQTransformer, GaMMA, PyOcto, GENIE and QuakeFlow code repositories are reported as MIT licensed by their maintainers. [SeisBench](https://github.com/seisbench/seisbench) uses GPL-3.0; model weights and waveform datasets may have separate terms. No external implementation code or weights are bundled here. The [2025 associator benchmark](https://doi.org/10.26443/seismica.v4i2.1559) varies event rate, false picks, station density and geometry; its synthetic comparisons do not establish truth for this Japan panel. [USGS ComCat](https://earthquake.usgs.gov/data/comcat/) also distinguishes automatic and analyst-reviewed events, which should remain separate statuses in future integration.

## Next validation gates

1. Admit real waveform sets under their network licenses, including timing, five-station selection, phase marks and source checksums. The current Zenodo archive does not ship continuous waveforms. The service only validates the shape of operator-submitted traces; it cannot prove their authenticity.
2. Recruit independent reviewers, lock blind verdicts before revealing event metadata, and record disagreement and the unresolved category. A shared operator token is suitable only for a controlled local pilot; a multi-user service needs individual identity and durable storage.
3. Sample per catalogue and per region with documented strata and inclusion weights. Fit local calibration after a representative panel rather than using a Ridgecrest or Japan threshold in an untested domain.
4. Compare full seven-feature, three-feature and waveform-augmented models on sequence, pipeline, time-window and reviewer holdouts. Report both discrimination and calibration, plus coverage after abstention.
5. Preserve all detections and raw scores when publishing tiers. The paper shows that one global quality threshold can distort early aftershock-rate estimates; assess tiering within time windows before any forecast integration.
6. Only after these gates, study whether catalogue-quality uncertainty improves the separate [a11oy aftershock forecast](https://github.com/szl-holdings/a11oy/blob/main/a11oy_seismic.py). Keep forecast outputs and confirmability scores distinct, and retain source/model/reviewer provenance.

The SZL Lambda aggregator remains an advisory governance component in its own repository. It is not a calibrated earthquake classifier and adds no demonstrated scientific value to this pilot model.
