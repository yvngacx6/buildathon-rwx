# Architecture

## Current best: v0

```
patch ──► diff.parse ──► per-file check ──► test set ──► pack (LPT, k=1..8) ──► JSON line
                          │
                          ├─ .py file, AST unchanged → no tests
                          └─ anything else           → ALL tests
```

| Module | Job |
|---|---|
| `selector/__main__.py` | Read `/data`, loop over commits, print JSON, log reasons to stderr. |
| `selector/diff.py` | Parse unified diffs; apply hunks in pure Python (no git/patch in the image). |
| `selector/select.py` | Patch → test numbers. |
| `selector/pack.py` | Choose worker count and split tests (scorer cost model). |
| `eval/evaluate.py` | Score the selector on `sample/` and the private set, without Docker. |

## History

| Version | Date | Change | Sample s |
|---|---|---|---|
| v0 | 2026-11-20 | No-op filter + ALL + optimal packing | 994.1 |
