# Experiment log

One row per experiment. Scores from `python3 eval/evaluate.py sample private`. CPU = selector CPU.

| # | Time (PST) | Version / branch | Ideas | Sample s | Sample misses | Private s | Private misses | Kept? | Note |
|---|---|---|---|---|---|---|---|---|---|
| E001 | 11-20 19:30 | v0 | I01 I02 | 994.1 | 0/30 | – | – | yes | Baseline: no-op filter, else ALL. |
| E002 | 11-20 21:30 | v1 | I03 I04 I05 I07 | 198.8 | 0/30 | 625.8 (41 robust-val+held patches) | 0/41 | yes | PLAN §3 on interim prototype maps; CPU 0.7 s sample, 2.2 s private. |
| E003 | 11-20 | r1-slowtests | I09 I11 | 198.8 | 0/30 | 596.1 (private41) | 0/41 | proposed | Slow tests (≥5 s) in function bodies only if they ran a touched statement (AST in-place statement diff). Saves op04, op06 (−29.7 s private). Sample unchanged: in 2haz and 66ks test_execute_tpcds ran the mutated line itself (dialect.py:1280, context.py:53), so only equivalence reasoning could drop it. Labeled slow failures (2jrq, 3utz, exec1) all inside their touched statements; on every labeled function region, every failing test (cheap or slow) ran a touched statement. Map stability (the two independent coverage runs behind the interim map, per slow test, lines inside functions both runs credit): tpcds 2 of 7076 differ (generator.py:1007-08, the _DISPATCH_CACHE fill), max_nodes 0 of 865, generator_memory 24 of 272 (dialect loading). Hence a guard: defs that write module/class-level state (16 of 3565 defs, incl. Generator.__init__, Dialect._try_load) keep the whole-function rule. CPU 0.68 / 2.17 s. |
