# Anonymized telemetry dataset

Anonymized copy of the OBD-II telemetry used in the article *An Explainable Multi-Agent Oracle Architecture for Vehicular Carbon Microcredit Tokenization Using Large Language Models and Permissioned Blockchain*.

## Contents

| Folder | Content |
|---|---|
| `data/` | Real dataset. Eight urban trips recorded with an OBD-II datalogger on the same reference route in Natal, Brazil, two per vehicle (Hyundai Creta, Fiat Fastback, Honda Fit and Volkswagen Polo). |
| `data_synthetic/` | Synthetic benchmark. 80 files derived from the real trips, combining 4 vehicles, 2 base trips and 10 scenarios. The 8 files of scenario 10 are intentionally empty. |

Scenarios of the synthetic benchmark:

| Id | Nature | Description |
|---|---|---|
| 01 | Legitimate | Standard driving session |
| 02 | Legitimate | Eco-driving with smooth acceleration |
| 03 | Legitimate | Ethanol-fueled operation |
| 04 | Legitimate | Mass air flow sensor failure (speed-density fallback) |
| 05 | Legitimate | Aggressive driving |
| 06 | Fraud | Implausible speed (above 200 km/h) |
| 07 | Fraud | Vehicle motion with zero engine rotation |
| 08 | Fraud | Machine-generated telemetry with near-zero RPM variance |
| 09 | Fraud | Out-of-range intake air temperature |
| 10 | Invalid | Empty file |

Readings are recorded approximately once per second. Columns that the pipeline does not use come from the data-collection application and are kept as recorded.

## Anonymization

Only the following changes were applied to the raw files. Every other value is copied verbatim, without reformatting.

- `VIN`: replaced by one pseudonym per vehicle (`PSEUDO-CRETA`, `PSEUDO-FASTBACK`, `PSEUDO-POLO`). The Honda Fit files carried no VIN and keep the recorded `null` or empty value.
- `time_sensor`: the absolute timestamps were replaced by `elapsed_time_s`, the time in seconds since the first reading of the file.

## Reproducibility

The columns used by the pipeline are unchanged (`mass_air_flow`, `rpm`, `intake_air_temperature`, `intake_manifold_absolut_pressure`, `speed`, `fuel_model_prediction` and `fuel_type`). For every file, the CO2 total, fuel type, traveled distance, physical-plausibility checks and CSV validation computed by the repository code are identical for the raw and the anonymized versions, and the CO2 totals and anomaly labels match the results reported in the article.

Because the files were modified, their SHA-256 digests differ from those of the raw files anchored on-chain during the experiments. The digests of the published files are listed in `SHA256SUMS.txt`.
