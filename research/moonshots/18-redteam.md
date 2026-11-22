# Moonshot 18 — red-team of the run-time pruners

**Verdict: two real holes, both fixed on `moon-18` (870fb6b); 0 misses and byte-identical
selections on all 566 labeled commits.**
1. **Import-time one-line lambdas (end-to-end MISSES on main: 3 of 5 probes).** A one-line lambda
   called while modules import (Parser table lambdas parsing the duckdb templates; test modules
   parsing at collection) emits PY_START but no LINE event of its own, and the selector skips a
   lambda's own line (defining it runs that line). The class-table path (`names lambda bodies`)
   never checked import time at all. Fixed: import_time map gets PY_START keys `<path>:-<line>`;
   lambda regions / class-table lambda spans called at import go to the import-state diff / fallback.
2. **diffexec ignores exception attributes** (latent: replay-level proof, no end-to-end miss today).
The hash-seed, statement-rule, r13 default-replay and r19 import-fn arguments held under attack.

## Method

Mutants: `/tmp/.../scratchpad/m18/spec{1,2}.json` → patches (`mkmut.py`), selector run in the
pinned image under the scorer flags (EC2 rwx / rwx-big, `bsel.sh`), forced replays
(`python -m diffexec.replay` on a request), labels with `requirements.lock`, SKIP_INTEGRATION=0,
`--continue-on-collection-errors`, explicit PYTHONHASHSEED where the attack is about seeds.

## Results per mechanism

| Mechanism | Soundness argument | Attack | Result |
|---|---|---|---|
| diffexec effect digest | result/exception + args after call, logs, warnings, stdout identical ⇒ caller sees the same | **exception attributes**: digest keeps only `(type, str(e))`. `Parser.parse_into`: `e.errors[0]["into_expression"] = expression_type` → `expression_types` / `None` (str unchanged) | **HOLE.** Forced replay on main: all 928 blobs ok, `test_parse_into_error(s)` phases "ok" → droppable. Labels: both tests FAIL. Not an end-to-end miss today only because the selector never runs diffexec on parse_into (no benefit; 477 cheap tests). A function on a slow path that raises ParseError / TokenError (`.start/.end`) / an exception whose `__cause__` type a test checks (`test_executor` asserts `ctx.exception.__cause__`) would be dropped. **Fixed.** |
| diffexec | same | `Parser.raise_error` col/highlight swaps; `TokenizerCore.tokenize` TokenError `end` | not recorded (budget) → kept; labels: raise_col 3 failing, all kept |
| diffexec + subtest under PYTHONHASHSEED=0 | old code deterministic ⇒ seed-0 replay ≡ any seed | remove `sorted(` / `dict.fromkeys`→`set` on optimizer paths (pushdown_predicates ×2, simplify, tsort, resolver, pushdown_projections, qualify_columns using, SetOperation.named_selects) | subtest **dropped test_execute_tpcds** for `pushdown_sorted1`; diffexec dropped 83/90 for `tsort`. Labels under seeds 1–6 / 1–2: pushdown_sorted1 never fails; tsort's failing tests (optimizer test_tpcds, test_tpch) were kept (replay saw the difference at seed 0). resolver_set fails 2 vs 3 tests by seed (real seed dependence) but diffexec had no recording → all kept. **No miss**; hole remains theoretical (a seed-0 coincidental match needs every replayed input to match). |
| statement rule (slow tests need a touched statement) | an untouched statement path is identical | `nonlocal` deletion, `while True`, `else:` / `try:` header lines (no LINE event) | `touched_statements` returns None for global/nonlocal, length changes, header-only changes of compound statements map to header lines that do emit LINE events (checked: `while True`/`while 1`/`try` emit; `else:` has no header fields). `n_nonlocal_replaced`: statement rule declined, 226 kept. No hole. |
| r13 default replay | a recorded call binds every parameter; equal-to-old-default ⇒ substitute | nested defs with literal defaults (recursion through a closure-held object built after the gc swap) | nested defaults change the outer code's constants and have no live function object ⇒ replay bails ("no live function object"). Only 5 nested literal-default defs in sqlglot, none recursive. No hole. |
| r19 import-fn / state diff | import-time effects are in the snapshot digest | state outside module globals / class dicts | snapshot covers closures, defaults, `__dict__`, slots, partials, loggers; lru_cache contents not, but sqlglot has no lru_cache. No attack found. |
| subtest / slowverify / genmem | re-run / frontier replay ≡ test | cold-fork coverage vs warm worker; caches written by touched code | guarded by `_writes_shared_state` (defs that write module/class state never pruned); instance caches (`MappingSchema._*_cache`) are per object and pickled with `self`. Not exploited. |
| duckdb template `.copy()` removal (shared-object aliasing) | — | `exp.replace_placeholders(self.T.copy(), …)` → `self.T` | not a mutant that changes behaviour: `transform` copies by default. |

Remaining theoretical holes (documented, not exploited): set-order coincidence at seed 0
(diffexec/subtest/slowverify all run seed 0); identity of returned module singletons (I12 hole 7);
cross-test attribution with order-dependent caches if labelers' worker grouping differs from
xdist loadfile.

## The fix

`diffexec/replay.py`: when the replayed call raises, `exception_detail(e)` = C-pickler digest of
`(e.args, e.__dict__, (type, str) of e.__cause__)` joins the side channel that old and new runs
must agree on (logs / warnings / stdout). The recorded store is untouched (the recorded digest
still has only type + str; the old run matches it, and old ≡ new on the detail). An unpicklable
detail raises → the blob is not ok (keeps tests). Unit test:
`tests/test_diffexec_serial.py::test_exception_detail_sees_attributes_args_and_cause`.
Forced replay after the fix: x_into_list 926/928 ok, the two failing tests "bad"; x_into_none
910/928 ok, the three tests reading `into_expression` "bad"; the other 485 phases stay ok.

## Hole 1: one-line lambdas called at import (from moonshot 11's observation)

Pinned-image import of every sqlglot module: 41 one-line code objects are called at import
(~27 lambdas: Parser table lambdas at parser.py 419, 1162, 1228–1306, 1586–1644, nested lambdas in
`_parse_join` etc., `highlight_sql`, `Dot.build`, `subclasses`; the rest genexprs).
Probes (`spec3.json`, full suite labeled on rwx-big, selector in the pinned image):

| Probe | main: selected / failing / MISSED | fixed: selected / MISSED |
|---|---|---|
| l_cast_strict (`"CAST": … _parse_cast(not self.STRICT_CAST)`) | 194 / 142 / **3** (test_optimize, test_qualify_columns, test_ref_type) | 1281 / 0 |
| l_true_false (`TokenType.TRUE` → `Boolean(this=False)`) | 76 / 70 / **3** | ALL / 0 |
| l_datatype_schema (`exp.DataType` lambda `allow_identifiers=True`) | 342 / 28 / **16+** (incl. test_execute_tpcds, dialect test-file collection errors) | ALL / 0 |
| l_count_bigint (`"COUNT"` `big_int=False`) | 63 / 6 / 0 | 1271 / 0 |
| l_join_alias (nested lambda in `_parse_join`) | 1047 / 1 / 0 | 1047 / 0 |

Fix: `mapbuild/import_time.py` records PY_START as `<path>:-<first line>`; the map was rebuilt on
rwx-big (positive keys reproduce all 27,788 old keys exactly, so only the 1,971 start keys were
merged into the committed map; MANIFEST updated). `ImportTimeMap.modules(path, first, last, start)`;
lambda regions (`_function_at_import`, `_test_helper_function`) pass the lambda's line as `start`;
`names.BodyChange.lambda_lines` + `_by_names`: a table lambda called at import → `Fallback`.
Unit tests: `test_import_time_map_start_keys_cover_one_line_lambdas`,
`test_lambda_in_table_called_at_import_falls_back`.

## Regression (rwx-big, pinned image, scorer flags; main d48ac42/bd16852 vs moon-18)

| Set | commits | differing selections | main s / misses | moon-18 s / misses |
|---|---|---|---|---|
| sample | 30 | 0 | 115.1 / 0 | 115.1 / 0 |
| private | 264 | 0 | 2417.2 / 0 | 2417.2 / 0 |
| private41 | 41 | 0 | 388.9 / 0 | 388.9 / 0 |
| adversarial | 46 | 0 | 1063.2 / 0 | 1063.2 / 0 |
| adversarial2 | 65 | 0 | 937.4 / 0 | 937.4 / 0 |
| practice_like | 120 | 0 | 989.6 / 0 | 989.6 / 0 |

Recommendation: merge moon-18 (no cost on any labeled set; closes a class of real misses that
practice-like one-token edits of Parser table lambdas would hit). Add the 5 l_* probes and the
2 x_into_* probes to the adversarial set.
