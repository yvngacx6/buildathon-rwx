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
| `select._removed_bindings` | I26 (r9-removed): a removed / renamed module- or class-level def or assignment in sqlglot/ → tests that ran the old def's body (callers) ∪ `from … import name` inside defs (lines map) ∪ the readers of every value the import-state diff shows changed (the removed name, the new name of a rename, tables that held the old function, class-creation products; `_state_readers`). ALL (via the import bound) when a test module imports or reads the name at collection, when the patched package does not import (some module imports the name), for removed classes / imports, special names, names the snapshot does not see, and reads in code cached across tests (`_build_dispatch`'s `dir()` over `*_sql` methods → every `*_sql` removal). The file's class / module regions are covered by those readers; its other function regions go through the usual rules; diffexec is off for it. |
| `Site.recv` (names.py) | `self.NAME` / `cls.NAME` in a plain / property / class method of class K (receiver never rebound, method never read off the class object): the object is a K or a subclass, so `_state_readers` skips the site for an owner class unrelated to K (no class of the owner's family is K or derives from it; metaclasses and unknown bases count as related). `Tokenizer.size` readers no longer include every `TokenizerCore.size` line. |
| `_on_module` (names.py) | A computed-name lookup on a module object (a top-level alias of a repo module bound once in the file, or a local bound only to such aliases / `import_module` / `__import__`) cannot read a class attribute (`serde._load`'s `getattr(module, class_name)`). |
| `selector/testimport.py` | I25: a test module whose own import-time code runs no sqlglot code (imports, defs, classes without sqlglot bases, literals, unittest.skip* / mock.patch / os.path…, test helpers made of builtins and str methods; no operators / hashing / subscripts on sqlglot objects, no metaclass-defined names) is *explained* by the import-state diff: with an empty diff, an import-time function run by its import changed nothing. Exception: a package's module `__getattr__` (`from sqlglot.dialects import BigQuery`) is sqlglot code the snapshot never calls, so an edit inside one is never explained. |
| `selector/nameindex.py` | Build-time step (`python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl`, in the Dockerfile) that stores the `names.py` scan as tables; run time loads it lazily (only class/module edits) and checks a source fingerprint, else builds in memory. For local eval run the same command with your `BR_SQLGLOT` and `index/names.pkl` (gitignored). |
| `selector/maps.py` | Lazy map loading (maps/SCHEMA.md), id → number via the run's tests.json. Build step `python -m selector.maps build maps index/maps` (Dockerfile; for local eval run the same, `index/` is gitignored) compiles the .json.gz maps to marshal files, line-keyed maps split per source file, so a run reads only the shards a commit touches. Same keys and lists; missing or stale (CRC of the .json.gz) → reads the .json.gz. |
| `selector/subtest.py` | r17: sub-test verification of `test_execute_tpcds` / `test_execute_tpch` / `test_optimized_tpch`. Build (`python -m selector.subtest build ROOT index/subtest.zip`, in the Dockerfile; ~30 s): per TPC query, the sqlglot lines its parse+pickle, execute, duckdb transpile (and, TPC-H, the optimized transpile) run (fresh fork each, sys.monitoring), its pinned outputs and CPU; plus set-up lines (Table construction / pickling). Run: for diffexec-style commits (clean function-body edits, not `_mixed_py`), the queries running a touched statement are re-run in a forked child with the changed functions' `__code__` swapped; identical outputs (type-strict digest, same duckdb SQL) → drop the test. Set-up touched, any difference / exception / CPU cap (3x pinned + 0.8 s per query) → keep. Spend ≤ min(12 s, 0.6 × saving) at 2x scorer slowdown. **r29 stage checkpoints:** an executed tpcds query whose touched statements run only in parse / optimize (+ the eager debug `.sql()`) / duckdb transpile is verified from the stored post-parse pickle: re-run just those windows and compare a type-strict tree digest (`strict_digest`) with the pinned one; equal → Plan / PythonExecutor get the same input and run untouched code. Divergence → whole-query re-run while ≤ 1.5 s pinned CPU, else keep. Guard (`stage_guard`): no shortcut when a changed function (old or new; closures via the outermost def) uses global / nonlocal / setattr / exec, or stores / mutates / deletes through module-level names, imports, `cls`, a Dialect's `self`, or local aliases of those. Store CPU is normalised by a calibration workload (optimize + tuple hash join) timed after each run, so a busy build box does not inflate it (`CALIB_REF`; idle and loaded builds agree within 4 %). Mutants: `research/moonshots/05/stage_mutants.py`. |
| `selector/slowverify.py` | r18: phase verification of `test_max_nodes` (28.8 s). Build (`python -m selector.slowverify build ROOT index/slowverify.pkl`, Dockerfile): the sqlglot lines its two expensive parses (P3 / P4: 2M tokens, max_nodes 1e6 / 5e4) run outside `sqlglot/errors.py` (cold fork each, sys.monitoring), and every frontier call into errors.py (`highlight_sql`, `ParseError.new`: one each) with pickled arguments and the old code's canonical result. Run (same gate as subtest: clean function-body edits): a touched statement on a P3/P4 line → keep; else a forked child with the new `__code__` swapped in runs the cheap phases (P1, P2, P5, P6) exactly as the test, and replays the frontier calls, which must return the pinned value without mutating their arguments → drop. Soundness: P3/P4 under new code run only untouched statements from the same state until a frontier call, and each frontier call returns the same value, so the phase is identical. Store keyed by a fingerprint of sqlglot/*.py + tests/test_parser.py and an exact match of the test body. `BR_SLOWVERIFY=0` disables. |
| `selector/pack.py` | Choose worker count and split tests (scorer cost model). |
| `mapbuild/import_time.py` | Build `maps/import_time.json.gz`: per line, the modules whose import runs it (fresh interpreter per module, sys.monitoring). |
| `eval/adversarial.py` | Write the adversarial private patch set (import-time code, removed names, hunkless diffs, …). Patches and labels stay out of git. |
| `eval/adversarial2.py` | Round-2 probes (65) aimed at the newer narrowing: name narrowing (aliases, f-strings, partial, defaults, lambdas, shadowing, `__getattr__`), import-state diff (metaclass-derived values, held reads), shared-state aliasing, slow-test statement rule, import bound, multi-file mixes. Labeled with the full suite in the pinned image; set in `eval/data/adversarial2/` (gitignored). |
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
shared state (r7-robust2): a class / module body that mutates objects (`KEYWORDS.pop(...)`,
`COERCES_TO[k] |= {...}`, loops) or whose edit binds an existing object by reference
(`SINGLE_TOKENS = Tokenizer.SINGLE_TOKENS`) gets the import-state diff first; a changed entry
outside the patched modules (not a subclass of one of their classes, not a copy of one of their
changed values) means the edit mutates another module's state → ALL, never the import bound:
once the module is imported (lazily, by any earlier test in the worker) every later test sees it;
a def that writes a class-level table at run time (`self.NO_PAREN_FUNCTION_PARSERS["PRIOR"] = …`
… `.pop("PRIOR")`) adds the tests that ran a line naming the table; a class the classes map does
not record (enums, namespaces) adds, to its conservative set, the tests that ran a line naming it
(`ErrorLevel.RAISE` is read in parser.py by tests that never run errors.py);
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

Round 8 (r8-diffexec2): an input over 16 kB is recorded with every list / dict / set of more
than 32 items replaced by a length-only stand-in (`serial.Elided`); any use of a stand-in other
than `len` / `bool` / identity (incl. builtins applied to it, watched through `sys.monitoring`
CALL events) makes that replay not ok. This covers executor `Context` inputs (row tables). An
input that is still too big, unpicklable or over budget marks only that (function, test phase)
incomplete instead of killing the function, so tests whose own calls were all recorded stay
droppable. Effect digests use the C pickler (identity-independent for immutables). The store
(LZMA) keeps, per function, all its droppable tests or only the expensive ones, chosen greedily
under a global byte budget by lines(f) × (seconds a change of f could save)^1.5 per byte
(tests.json seconds; no labels).

Round 19 (r19-importfn): **import-time runs no longer block run-time pruning.** Core
Parser / Tokenizer functions (`_parse_table`, `_parse_join`, `_scan_number`, …) also run while
`sqlglot.generators.duckdb` (templates parsed at import) and the dialect test modules import.
Their body edits were `_mixed_py` (diffexec / subtest / slowverify off) whenever the import-time
dependents were not cheap to add (STATE_MIN_GAIN counted only the added tests, not the slow
callers the pruners could drop), so test_max_nodes / test_generator_memory stayed selected.
Now (1) the gain also counts `slack`: slow prunable callers of the def, so the state diff runs;
(2) the edited def's own state entry (a def with a closure, e.g. `super()`, is digested with
its code) is ignored: it is the edit, seen by its callers; (3) diff empty (and every importing
test module explained, as before) → plain body edit; diff changed and every changed value has a
seen reader → those readers are kept (`fixed`, never pruned) and the commit stays prunable
("readers"; dependents of unexplained test modules are kept the same way), even when the
readers set costs more raw seconds than the dependents if it drops the slow tests; a changed
value with no seen reader (callers stand in for it) or an import failure → `_mixed_py` as before.

Round 34 (r34-gap): **R-DEF default edits of defs run at import.** The r19 rule skipped the
edited def's own import-state entry only for body edits. A literal-default edit (R-DEF: every
change in the file is a literal default and nothing reads `__defaults__` & co.) changes that
entry only in its defaults, which only a call leaving the parameter out can observe, and such a
call runs the body (lines map / default-observing calls). So the entry is skipped there too:
diff otherwise empty → no import-time dependents and the commit stays prunable; other changed
values → their readers (`hook=own`). Before, the entry's own readers (every holder of a
Parser) made the readers set as wide as the dependents (`_build_list_sort` 25.5 → 2.8 s).

Round 16 (r16-allcut): **import-time edits by state readers.** (1) A function body run while
modules are imported (`import sqlglot` → was ALL; a module's import → its dependents) now
takes, when the patched package imports and it is cheaper, the readers of every value the
import-state diff shows changed (`_import_state_users`, `_state_readers` unbounded as in
`_removed_bindings`) ∪ its run-time runners ∪ the dependents of every test module whose own
import runs sqlglot code (testimport: not explained; all test modules when the map collapsed
the lines to `import sqlglot`). A changed value nobody is seen reading is accepted only when
the lines also run under tests. (2) Class-creation hooks (`__init_subclass__`, metaclass
methods; base files were ALL) use the same rule over the hook's lines (`_class_hook`); the
hook's own entry is skipped; a class table it mutates in place adds the tests naming it,
unless the hook first binds it to a fresh object (`_fresh_tables`). (3) A class / module body
with "no known users" tries the unbounded import-state narrowing before ALL; TypedDict key sets
(`__total__`, `__required_keys__`, …) are data: their readers are the tests that ran a line
naming the class (subclassing at run time), Fallback if any code spells the dunder or holds
the class at import.

Round 13 (r13-last): **default-value edits.** When every change in a file is a literal default
of a def (bool / int / finite float / str / None → another such literal; `select._default_changes`
compares the two ASTs with all defaults blanked) and no code in sqlglot/ or tests/ reads
defaults (`__defaults__`, `__kwdefaults__`, `.default`, `.defaults`, getcallargs, … via the
name index), the def's runners are prunable like a body edit (class users of the signature
edit stay fixed). The child compiles the *old* source and replays each recorded call with the
new default substituted for every parameter whose recorded value is the old default (same
type, equal); the live function objects get the new `__defaults__` / `__kwdefaults__` in the
new run (recursive and nested calls). A recorded call binds every parameter, so a value other
than the old default was passed by the caller and is unaffected; one equal to it may have been
passed explicitly, in which case the substitution can only make the replay keep more.

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
| r10-dialectstate | 2026-11-21 | I22 reader precision for class settings consumed at creation (the "readers among holders of X" rule already existed in `_by_state`; the waste was in which reader sites count). (1) Read projections: a run-time `x.NAME` read used only for its truth (`if`, `not`, `bool()`, and/or operands in a test) or membership (`k in x.NAME`), also through a local (`Site.use`, per load in `flow`); the state diff reports per changed entry whether `bool(value)` and the key / element set stayed the same (`state.proj`, old projections stored in the snapshot), and such reads are then dropped (generator `"x" in …NUMERIC_ESCAPES`, tokenizer `if … and numeric_escapes`). (2) A computed-name lookup on a module object (`getattr(module, name)` with `module` bound only to `__import__(…, fromlist=…)` / `import_module` / a module import alias) reads module globals, never a class attribute (serde `_load` was a reader of every class setting). (3) An inert class / module body edit after which the patched import builds identical state package-wide (a metaclass overwrites the attribute: `SUPPORTS_COLUMN_JOIN_MARKS`) selects nothing (not for set literals, whose order the snapshot does not see) | 145.4 (vs main 286a308: sample 149.0 → 145.4, private 2713.8 → 2617.7, private41 434.5 → 394.4, adversarial 1168.6 → 1167.0, adversarial2 994.1 → 945.3; 0 misses on all; 37 EC2-labeled dialect-setting probes: 0 misses, 321.9 → 255.6 s) |
| r9-removed | 2026-11-21 | I26 removed / renamed bindings → callers ∪ readers ∪ import-state diff readers (was ALL / import bound); `Site.recv` receiver-class filter and module-object `getattr` in the state readers; I25 test-module roots explained by the import-state diff (selector/testimport.py) | 147.9 (gap.py: private 2713.8 → 2576.6, private41 434.5 → 394.8, adversarial 1166.6 → 1153.6, 0 misses; E016) |
| r4-gap | 2026-11-21 | eval/gap.py; test helper functions / constants by lines map / name readers; base-file class bodies narrowed (no conservative bound); import-time functions with an unchanged import state | 179.9 (private264 2882.7 → 2741.5) |
| r7-robust2 | 2026-11-21 | Probe round 2 (eval/adversarial2.py, 65 probes, eval/data/adversarial2). Misses on main: a class body that aliases another class's dict instead of copying it and then mutates it (`SINGLE_TOKENS = Tokenizer.SINGLE_TOKENS; SINGLE_TOKENS.pop("%")` in lazily loaded Teradata removes `%` from every later tokenizer in the worker; Redshift, Hive likewise) → bodies that mutate objects or newly alias one get the import-state diff, and a change outside the patched modules is ALL (`_foreign_state`, also when another module stops importing); diffexec kept a def that leaks a class-level table across calls (`_parse_connect_with_prior` without its `.pop("PRIOR")`) → defs that write module / class state (also through local aliases or mutating calls) are never pruned and add the readers of a class table they write. Preventive: classes outside the classes map (enums) add the tests naming them to the conservative set; deleting a one-line `KEY: lambda …` table entry is also a class-body change | 149.0 (private 2713.8, private41 434.5, adversarial 1166.6 → 1168.6, adversarial2 2546.7 s / 6 misses → 994.1 s / 0; d2e0730 had 5 of the 6) |
| r8-diffexec2 | 2026-11-21 | I12 round 8: elided stand-ins for big container inputs (executor Context), per-(function, test phase) completeness, C-pickler effect digests, LZMA store under a stakes-weighted global budget (13.1 MB), parent-side replay gate | 118.2 (private 2678.4, private41 428.9, adversarial 1168.6, adversarial2 993.8; 0 misses) |
| r13-last | 2026-11-21 | eval/reasons.py (rank spend by mechanism on eval/ab.py runs); diffexec replays literal default-value edits (old code, new default where a call may have used it). Measured negatives: resolving dialect.py:332's `Tokenizer` hand-off to sqlglot.tokens.Tokenizer saves 0 s (the next blocker is the Tokenizer metaclass's real import-time read, already handled by the state diff); a 37 % larger diffexec store saves 25 s over 1,600 A/B patches | sample/private/private41/adversarial/adversarial2 unchanged (0 misses); A/B −43.6 s over 1,600 patches |
