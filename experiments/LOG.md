# Experiment log

One row per experiment. Scores from `python3 eval/evaluate.py sample private`. CPU = selector CPU.

| # | Time (PST) | Version / branch | Ideas | Sample s | Sample misses | Private s | Private misses | Kept? | Note |
|---|---|---|---|---|---|---|---|---|---|
| E001 | 11-20 19:30 | v0 | I01 I02 | 994.1 | 0/30 | – | – | yes | Baseline: no-op filter, else ALL. |
| E002 | 11-20 21:30 | v1 | I03 I04 I05 I07 | 198.8 | 0/30 | 625.8 (41 robust-val+held patches) | 0/41 | yes | PLAN §3 on interim prototype maps; CPU 0.7 s sample, 2.2 s private. |
