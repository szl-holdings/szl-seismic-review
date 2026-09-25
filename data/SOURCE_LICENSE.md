# Source data attribution

`japan_panel.csv` and `japan_verdicts.csv` are small, format-normalized extracts from **Meng, Lingsen; Huang, Hui; Ma, Jinzhi; Ma, Yang**, *Data and code for: Can human experts verify machine-learning detected earthquakes?*, Zenodo version 1.1.0, DOI [10.5281/zenodo.22059219](https://doi.org/10.5281/zenodo.22059219). The Zenodo record declares **CC BY 4.0**. Changes: selected the 200 NE Japan forearc panel rows, joined seven association feature columns by `event_id`, normalized headers, and sorted rows. Published verdict labels remain `real`, `uncertain`, and `false` in the derived CSV.

`source_manifest.json` records the exact archive MD5 values and derived CSV SHA-256 values. The full archives and continuous seismic waveforms are not distributed in this repository. The project's Apache-2.0 code license does not replace the source data's CC BY 4.0 terms.
