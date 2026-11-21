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
                                  │                     (nothing if only sqlglot/ modules run it and the
                                  │                     import-state diff is empty)
                                  │        class / module assignment → names.py: readers of the changed
                                  │                   names (line map) ∩ conservative set, else:
                                  │        class-creation consumers → state.py: import old + patched,
                                  │                   diff all class / module values; readers of each
                                  │                   changed value among tests that can hold its class, else:
                                  │        class    → classes map (class chain, MRO) ∪ tests that ran the file
                                  │        module   → module users ∪ tests that ran the file ∪ importers' users
                                  │        base files (BASE_FILES): name / state narrowing, unbounded, else ALL
                                  ├─ tests/fixtures/** → reads map (never AST-filtered)
                                  ├─ tests/*.py → test functions; other functions → lines map;
                                  │        module constants → name readers; else whole file ∪ test_imports
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
| `selector/state.py` | I22: what a patch changes in import-time state. Build: snapshot (digest of every module global and class attribute after importing all of sqlglot, 16 fresh imports to find order-only noise) next to the name index. Run: a subprocess imports the patched package and diffs; `select._by_state` selects run-time readers of each changed value (Site.flow follows locals / store-only constructor params) among tests that can hold the owner class. ≈1.2 s container CPU per diff, so only run when the wide set can shrink by ≥ 2 s. |
| `selector/nameindex.py` | Build-time step (`python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl`, in the Dockerfile) that stores the `names.py` scan as tables; run time loads it lazily (only class/module edits) and checks a source fingerprint, else builds in memory. For local eval run the same command with your `BR_SQLGLOT` and `index/names.pkl` (gitignored). |
| `selector/maps.py` | Lazy gz map loading (maps/SCHEMA.md), id → number via the run's tests.json. |
| `selector/pack.py` | Choose worker count and split tests (scorer cost model). |
| `mapbuild/import_time.py` | Build `maps/import_time.json.gz`: per line, the modules whose import runs it (fresh interpreter per module, sys.monitoring). |
| `eval/adversarial.py` | Write the adversarial private patch set (import-time code, removed names, hunkless diffs, …). Patches and labels stay out of git. |
| `mapbuild/import_map.py` | Builds `maps/imports.json.gz`: runs every test alone in a fresh pytest process with `mapbuild/importtrace/sitecustomize.py` on PYTHONPATH (audit "import" hook, also in subprocesses / spawned pool workers) and records each module it loads. Selector use: the import bound (below). |
| `mapbuild/convert_prototype.py` | INTERIM: convert the laptop prototype maps (research reports 02 and 05) into the SCHEMA format. |
| `maps/` | `lines`, `classes`, `reads`, `test_imports`, `import_time` (.json.gz) + `MANIFEST.json`. |
| `eval/evaluate.py` | Score the selector on `sample/` and the private set, without Docker. |
| `eval/gap.py` | Oracle-gap report (I14): per commit our cost, the oracle cost (failing tests packed alone), the gap, the reason, and which mechanism (function map, statement rule, name / state narrowing, conservative set, import bound, ALL fallback, fixture readers, test-file rules, untraced, near-ALL) added the extra tests; aggregated by mechanism and kind. |
| `tests/test_select.py` | One unit test per rule, on a tiny fake repo (`python -m pytest tests/`). |
| `tests/test_names.py` | The I08 narrowing and each of its fallbacks. |
| `tests/test_state.py` | The I22 import-state narrowing on a fake package with a metaclass. |

Import bound (r2-importclosure): an edit confined to one module can only reach tests that load
that module (imports map) or read it as a file (reads map); if the module stops importing,
exactly those tests error. Used for: new text that does not parse, deleted / renamed .py
files (old path; a rename onto conftest, __init__ or a non-module stays ALL), removed
bindings, and every rule below that would otherwise answer ALL – but only for modules loaded
lazily at run time (dialects by name, imports inside functions). A module loaded while
pytest collects the test files (160 of them, incl. the `import sqlglot` set, everything
tests/dialects/test_dialect.py and test files import) → ALL: a collection error may
interrupt the whole session (pytest default). Deleting a test file nothing imports → its tests.

Safety nets: hunk does not apply → ALL; does not parse → import bound; definition-time change
with no known users → import bound; module-level non-assignment change → import bound;
base-class bodies (`BASE_FILES`) that the name / state narrowing cannot take → import bound
(≈ ALL: 1617 tests load them);
a function no test ran is treated as definition-time code (it may run only at import);
Import-time: a function whose body runs while modules are imported (trie builders, decorator
factories, `X = build()` tables, module `__getattr__`) is a change to every importer: ALL if
`import sqlglot` runs it, else the dependents of each module whose import runs it. Module-level
name changes follow derived names (`B = f(A)`) and same-module functions that read them;
deleted / renamed top-level defs and classes join the importer search. `diff --git` entries
without ---/+++ (pure renames, empty files added / deleted) are kept as hunkless file patches.
tests that no map knows join every non-empty selection. Without the imports map, every
"import bound" above is ALL.

## History

| Version | Date | Change | Sample s |
|---|---|---|---|
| v0 | 2026-11-20 | No-op filter + ALL + optimal packing | 994.1 |
| v1 | 2026-11-20 | PLAN §3 table on interim prototype maps (function / class / module regions, reads, test imports) | 198.8 |
| r1-attrnarrow | 2026-11-20 | I08: name-reader narrowing of class / module assignments; lambda call lines | 185.0 |
| r1-robust | 2026-11-20 | Import-time map; removed/renamed/derived module names; hunkless diff entries (adversarial round 1) | 198.8 |
| r2-slowtests | 2026-11-20 | Slow tests (≥ 5 s) in function bodies need a touched statement (private264: 3453.9 → 3269.2) | 185.0 |
| r2-importclosure | 2026-11-20 | I22: import bound (traced per-test import closure) instead of ALL, run-time-only modules | 183.9 (private264 3263.9 → 2923.7) |
| r2-classcreate | 2026-11-20 | I22: import-state diff follows class-creation consumers to derived values; readers among owner-class holders | 179.9 |
| r4-gap | 2026-11-21 | eval/gap.py; test helper functions / constants by lines map / name readers; base-file class bodies narrowed (no conservative bound); import-time functions with an unchanged import state | 179.9 (private264 2882.7 → 2741.5) |
