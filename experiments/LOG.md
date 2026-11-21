# Experiment log

One row per experiment. Scores from `python3 eval/evaluate.py sample private`. CPU = selector CPU.

| # | Time (PST) | Version / branch | Ideas | Sample s | Sample misses | Private s | Private misses | Kept? | Note |
|---|---|---|---|---|---|---|---|---|---|
| E001 | 11-20 19:30 | v0 | I01 I02 | 994.1 | 0/30 | – | – | yes | Baseline: no-op filter, else ALL. |
| E002 | 11-20 21:30 | v1 | I03 I04 I05 I07 | 198.8 | 0/30 | 625.8 (41 robust-val+held patches) | 0/41 | yes | PLAN §3 on interim prototype maps; CPU 0.7 s sample, 2.2 s private. |
| E003 | 11-20 | r1-attrnarrow | I08 | 185.0 | 0/30 | 583.2 (private41) | 0/41 | pending review | Class/module assignment edits → tests that ran a line reading the changed name ∩ v1 set; changed lambda in a table → its call lines only. 535z 11.2→4.6, 6ze8 9.1→2.0, baseclass_attr 12.7→11.4, h_gtab0/1/2, op10/12 cut. CPU 3.2 s sample (+2.5), 5.9 s private (+3.7): whole-source parse at run time. |
