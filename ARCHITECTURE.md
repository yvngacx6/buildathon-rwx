# Architecture

## Current best: v1 (branch `v1`)

```
patch ──► diff.parse/apply ──► per file ──► rule (select.py) ──► ∪ ──► +untraced ──► pack (LPT, k=1..8) ──► JSON
                                  │
                                  ├─ .py: classify.regions → function / class / module regions
                                  │        function → lines map over the function span
                                  │                   (slow tests: only if they ran a touched statement)
                                  │                   ∪ import-time map: run while `import sqlglot` → ALL,
                                  │                     run while importing module M → M's dependents
                                  │        class / module assignment → names.py: readers of the changed
                                  │                   names (line map) ∩ conservative set, else:
                                  │        class    → classes map (class chain, MRO) ∪ tests that ran the file
                                  │        module   → module users ∪ tests that ran the file ∪ importers' users
                                  ├─ tests/fixtures/** → reads map (never AST-filtered)
                                  ├─ tests/*.py → test functions, or whole file ∪ test_imports
                                  └─ docs/config/unknown → nothing / pytest-section check / ALL
```

The full rule table is the docstring of `selector/select.py` (it implements research/PLAN.md §3).
Every rule is about code structure; no rule names an edit kind as harmless or maps edits to failures.

| Module | Job |
|---|---|
| `selector/__main__.py` | Read `/data`, loop over commits, print JSON, log reasons to stderr. Never crashes: any error → ALL. A selection that costs ≥ 85 % of ALL becomes ALL. |
| `selector/diff.py` | Parse unified diffs; apply hunks in pure Python (no git/patch in the image); report changed line numbers. |
| `selector/classify.py` | Place each changed line in the old (removed) or new (added) syntax tree → regions. Drops regions whose scope's AST is unchanged (comments, docstrings). Metaclass methods and `__init_subclass__` count as class body. For an in-place function edit, also gives the old-file spans of the touched statements (`touched_statements`). |
| `selector/select.py` | Region / file → test numbers (the PLAN §3 table). |
| `selector/names.py` | I08: narrow class-attribute / module-constant edits to tests that ran a line reading the changed name. Static scan of the pinned source at run time; falls back on reads at import / class creation / in memoized code, dynamic attribute access that could name it, non-literal new values, and definition-time calls handed the class. |
| `selector/nameindex.py` | Build-time step (`python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl`, in the Dockerfile) that stores the `names.py` scan as tables; run time loads it lazily (only class/module edits) and checks a source fingerprint, else builds in memory. For local eval run the same command with your `BR_SQLGLOT` and `index/names.pkl` (gitignored). |
| `selector/maps.py` | Lazy gz map loading (maps/SCHEMA.md), id → number via the run's tests.json. |
| `selector/pack.py` | Choose worker count and split tests (scorer cost model). |
| `mapbuild/import_time.py` | Build `maps/import_time.json.gz`: per line, the modules whose import runs it (fresh interpreter per module, sys.monitoring). |
| `eval/adversarial.py` | Write the adversarial private patch set (import-time code, removed names, hunkless diffs, …). Patches and labels stay out of git. |
| `mapbuild/convert_prototype.py` | INTERIM: convert the laptop prototype maps (research reports 02 and 05) into the SCHEMA format. |
| `maps/` | `lines`, `classes`, `reads`, `test_imports`, `import_time` (.json.gz) + `MANIFEST.json`. |
| `eval/evaluate.py` | Score the selector on `sample/` and the private set, without Docker. |
| `tests/test_select.py` | One unit test per rule, on a tiny fake repo (`python -m pytest tests/`). |
| `tests/test_names.py` | The I08 narrowing and each of its fallbacks. |

Safety nets: hunk does not apply / does not parse → ALL; definition-time change with no known
users → ALL; module-level non-assignment change → ALL; base-class bodies (`BASE_FILES`) → ALL;
a function no test ran is treated as definition-time code (it may run only at import);
tests that no map knows join every non-empty selection.
Import-time: a function whose body runs while modules are imported (trie builders, decorator
factories, `X = build()` tables, module `__getattr__`) is a change to every importer: ALL if
`import sqlglot` runs it, else the dependents of each module whose import runs it. Module-level
name changes follow derived names (`B = f(A)`) and same-module functions that read them;
deleted / renamed top-level defs and classes join the importer search. `diff --git` entries
without ---/+++ (pure renames, empty files added / deleted) are kept as hunkless file patches.

## History

| Version | Date | Change | Sample s |
|---|---|---|---|
| v0 | 2026-11-20 | No-op filter + ALL + optimal packing | 994.1 |
| v1 | 2026-11-20 | PLAN §3 table on interim prototype maps (function / class / module regions, reads, test imports) | 198.8 |
| r1-attrnarrow | 2026-11-20 | I08: name-reader narrowing of class / module assignments; lambda call lines | 185.0 |
| r1-robust | 2026-11-20 | Import-time map; removed/renamed/derived module names; hunkless diff entries (adversarial round 1) | 198.8 |
| r2-slowtests | 2026-11-20 | Slow tests (≥ 5 s) in function bodies need a touched statement (private264: 3453.9 → 3269.2) | 185.0 |
