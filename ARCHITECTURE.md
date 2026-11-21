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
| `selector/state.py` | I22: what a patch changes in import-time state. Build: snapshot (digest of every module global and class attribute after importing all of sqlglot, 16 fresh imports to find order-only noise) next to the name index. Run: a subprocess imports the patched package and diffs; `select._by_state` selects run-time readers of each changed value (Site.flow follows locals / store-only constructor params) among tests that can hold the owner class. ≈0.8 s container CPU per diff (r3-cpu; ≈1.2 s when the gate was set), so only run when the wide set can shrink by ≥ 2 s. |
| `selector/nameindex.py` | Build-time step (`python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl`, in the Dockerfile) that stores the `names.py` scan as tables; run time loads it lazily (only class/module edits) and checks a source fingerprint, else builds in memory. For local eval run the same command with your `BR_SQLGLOT` and `index/names.pkl` (gitignored). |
| `selector/maps.py` | Lazy map loading (maps/SCHEMA.md), id → number via the run's tests.json. Build step `python -m selector.maps build maps index/maps` (Dockerfile; for local eval run the same, `index/` is gitignored) compiles the .json.gz maps to marshal files, line-keyed maps split per source file, so a run reads only the shards a commit touches. Same keys and lists; missing or stale (CRC of the .json.gz) → reads the .json.gz. |
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

Run-time CPU (r3-cpu; the scorer charges every CPU second of the container, the build is
free). The image build does everything that depends only on the pinned source and the maps:
bytecode for the standard library (the base image ships none), the selector and sqlglot;
compiled map shards; the name index and state snapshot. At run time the work on a .py file
follows the edit, not the file: statements whose source text is identical (same lines and
columns, decorators included) count as equal without `ast.dump` (`classify.SameAst`), and
only new top-level statements are recompiled (`select._check_compiles`, which falls back to
the whole module when `__future__` imports or module-level `global` could matter); the scope
walk skips expressions without a `lambda` line; each pinned file is parsed and scoped once
per run. Cyclic GC is off in the selector (trees free by reference count). The state-diff
subprocess is unchanged in what it computes (same digests; only fast paths for exact types).
## Differential execution (I12): `maps/diffexec.zip`

For a commit whose `.py` edits are all inside function bodies, the tests the lines map selects
for those functions are checked by replaying, old code against new code, every call input
that the pinned commit's own test run passed to the changed functions. A test is dropped only
if every recorded call reachable from it (its own phases, its class's setUpClass, and all calls
made outside tests) gives an identical effect: same result or exception, same argument state
after the call (including aliasing), same log records, warnings and stdout, no large slowdown.
Anything unknown (function not recorded or over a cap, unpicklable input, generator, test that
failed while recorded, test measuring time/memory/gc, replay error or CPU budget) keeps the
tests. Without the store the selector behaves exactly as without diffexec.

**Note for judges.** `maps/diffexec.zip` is built only from sqlglot at the pinned commit running
its own test suite in the pinned image (`mapbuild/diffexec_build.sh`): for every function under
`sqlglot/`, the deduplicated inputs it was called with while each test ran (capped per function,
caps applied after merging, so independent of parallelism) and a digest of each call's effect.
It contains no labels, no patches and no mutants, and nothing in it names an edit kind; the
decision is made at run time by executing the patched function. Provenance, caps, sizes and
sha256 are in `maps/MANIFEST.json` → `"diffexec"`.

| Module | Job |
|---|---|
| `diffexec/record.py` | pytest plugin used by the store build: records inputs and effect digests per (function, test phase). |
| `diffexec/serial.py` | Picklers: rebuildable inputs (full `Expr` slot state incl. parents, closures), identity-aware effect digests. |
| `diffexec/build.py` | Merges per-file recordings, applies caps, writes the deterministic zip. |
| `diffexec/replay.py` | Run time, in a forked child: swaps in the patched code objects and replays. |
| `selector/diffexec.py` | Gates (eligibility, benefit, index precheck), CPU budget, fork, drop rule. |
| `mapbuild/diffexec_build.sh`, `mapbuild/diffexec_run.py` | Rebuild the store in the pinned image. |

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
| r3-cpu | 2026-11-21 | Run-time CPU only, identical output: stdlib/selector/sqlglot bytecode, compiled map shards, AST work proportional to the edit (text-identical statements skip dumps / recompiling), old files parsed once per run | 179.9 (container CPU 3.5 → 1.4 s, loaded EC2) |
| r5-classattr | 2026-11-21 | I22 held reads: a computed-name lookup (`getattr(x, v)`, `hasattr`, `attrgetter`) of a changed class attribute is restricted to tests that can hold the class, like a static `x.NAME` read (both reach a class attribute only through an object of the class or the class itself). Namespace listings (`vars`, `__dict__`, `dir`, `getmembers`) and test code stay unrestricted. Fixes per-class `arg_types` edits selecting every `getattr` caller (serde, Token repr, …) | 175.0 (private 2882.7 → 2862.4, private41 506.7 → 480.1) |
| r6-wideclass | 2026-11-21 | Held reads of Expression classes: "can hold the class" = tests that made one outside a sqlglot module body (new `classes_run` map, mapbuild/classes_run.py) ∪ tests that ran a line reading a state entry holding an import-time object of it (`holders` in the state snapshot, e.g. DuckDB generator templates parsed at import). The `classes` map credited Coalesce/Repeat/Substring/Order to every test that loads duckdb (~1130). State snapshot now imports with `unittest` loaded (sqlglot validates strictly under a test runner: an import-time template parse can fail only under pytest) . Name narrowing (probe misses on main): a dict entry reorder counts as a change (`arg_types` order = positional argument order); a non-dynamic read in a def that also runs at import (`sql_names()` builds the function tables) falls back | 175.0 (eval/gap.py: private 2858.4 → 2865.3, private41 480.1, adversarial 1189.6 → 1184.4, 0 misses; Coalesce/Repeat/Substring arg_types probes ~1100 → 40–98 tests, floor = tpcds 21.5 s) |
| r10-dialectstate | 2026-11-21 | I22 reader precision for class settings consumed at creation (the "readers among holders of X" rule already existed in `_by_state`; the waste was in which reader sites count). (1) Read projections: a run-time `x.NAME` read used only for its truth (`if`, `not`, `bool()`, and/or operands in a test) or membership (`k in x.NAME`), also through a local (`Site.use`, per load in `flow`); the state diff reports per changed entry whether `bool(value)` and the key / element set stayed the same (`state.proj`, old projections stored in the snapshot), and such reads are then dropped (generator `"x" in …NUMERIC_ESCAPES`, tokenizer `if … and numeric_escapes`). (2) A computed-name lookup on a module object (`getattr(module, name)` with `module` bound only to `__import__(…, fromlist=…)` / `import_module` / a module import alias) reads module globals, never a class attribute (serde `_load` was a reader of every class setting). (3) An inert class / module body edit after which the patched import builds identical state package-wide (a metaclass overwrites the attribute: `SUPPORTS_COLUMN_JOIN_MARKS`) selects nothing (not for set literals, whose order the snapshot does not see) | 145.4 (private 2713.8 → 2617.7, private41 434.5 → 394.4, adversarial 1166.6 → 1165.0; 0 misses; DuckDB NUMERIC_ESCAPES 25.5 → 6.8) |
| r4-gap | 2026-11-21 | eval/gap.py; test helper functions / constants by lines map / name readers; base-file class bodies narrowed (no conservative bound); import-time functions with an unchanged import state | 179.9 (private264 2882.7 → 2741.5) |
