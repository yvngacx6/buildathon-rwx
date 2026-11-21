# Moonshot 18 — red-team of the run-time pruners

**Verdict: one real soundness hole found and fixed (diffexec ignores exception attributes);
no end-to-end miss on current main from the 15 mutants labeled. The hash-seed, statement-rule,
r13 default-replay and r19 import-fn arguments held under attack.** Fix on branch `moon-18`
(`diffexec/replay.py` `exception_detail` + unit test). Labeled-set check: see "Regression" below.

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

## Regression

REGRESSION_PLACEHOLDER
