# Moonshot 15: per-fixture-case slicing for code edits (branch `moon-15`)

## Verdict and numbers first

**Sound and works, but small. It is not a moonshot.** Prototype: `selector/cases.py` + a 7-line hook in
`select.py`. It is a fourth run-time pruner after subtest / slowverify, with the same gate (clean
function-body edits, diffexec jobs, not `_mixed_py`).

| Measured (laptop, version c1 = all-or-nothing verdicts) | P (600 unlabeled) | labeled (566: sample, practice_like, private, private41, adv, adv2) |
|---|---|---|
| Commits where the pruner drops ≥ 1 test | 36 (6 %) | 31 (5.5 %) |
| Sim seconds saved | 60.5 s = **−4.0 s / 40** | 46.7 s = **−3.3 s / 40** (sample −1.4 s/30; practice_like −15.5 s/120 = −5.2/40) |
| Commits whose selection reaches `[]` | **0** | **0** |
| Misses | real pytest on every drop: 36 commits / 150 dropped tests: **all PASS** | **0 new misses**; 489 labeled-failing optimizer tests reached the pruner: all kept; 146 dropped, none failing |
| Verify child CPU (laptop) | 13.0 s → 1.7 s / 40 at 2x | about 1.6 s / 40 at 2x |
| Parent CPU (c1: eager 0.37 s store load + naive line scan) | +12.8 s / 600 → +1.7 s / 40 at 2x | noisy |

**Net per 40 practice-style commits, c1: about −4 s sim + 3.4 s CPU ≈ −0.5…−1 s.** That is close to zero.
The c2 fixes on the branch (lazy store, bisect lookup, one union per case set, per-test verdicts in grandchildren, warm-only guard) should cut the parent CPU to about
0.4 s (one store load) and add some mixed drops. Expected net c2: **−2…−3 s / 40**. See the EC2 section for the c2 measurement.

**Why it never reaches `[]`.** Every commit that keeps an optimizer fixture test also keeps a dialect
test, test_transpile, test_executor, test_scope or the like. The 2 s floor never disappears from this lever
alone. Of the 36 P drops, about 8 leave a residual of ≤ 2.5 s. Moonshot 10's V1 (run cheap residuals) would turn those into `[]`, so the two compose.
The 106 P commits ("ceiling 268 s") whose selection lies entirely inside the 23 target tests are mostly
**fixture edits** (`tests/fixtures/optimizer/*.sql` → readers) or definition-time edits. Every fixture
commit in the sample fails its test, so case-slicing fixture edits gains nothing (not built).

P(works on the board as measured) ≈ 70 %. P(moonshot) ≈ 0 %. Expected gain ≈ 2–3 s per 40 practice commits
(≈ 3–4 s on the 60-commit final), 0 misses.

## Mechanism

- **Targets.** The 22 `TestOptimizer` tests that call `check_file` and `test_annotate_funcs`: 4497 cases in all
  (annotate_funcs 2435, simplify 651, qualify_columns 397, merge_subqueries 147, unnest 129, tpcds 99, …).
- **Build** (`python -m selector.cases build ROOT index/cases.pkl`; 99 s on 8 laptop jobs, 0.76 MB):
  - **Non-case coverage.** For each test, in a cold fork: setUpClass (pool stubbed, real duckdb), setUp, and the test body with `check_file` replaced by a recorder.
    The recorder also yields the exact `check_file` calls. The cases come from them with check_file's own meta logic
    (schema / dialect / leave_tables_isolated / execute …). Functions and partials are pickled by reference. The
    test module's helpers resolve by name to an AST-extracted copy, so neither pandas nor duckdb is imported at run time.
  - **Per-case coverage.** Each case runs in a cold fork (`maxtasksperchild=1`) under sys.monitoring: submit pickle → worker
    unpickle → `parse_and_optimize` → result pickle round trip → `.sql(pretty, dialect)`. For execute cases it also
    runs the duckdb SQL of both expressions. For annotate_funcs it runs the type sql and `DataType.build(expected)`. Stored: the inverse index
    file → line → case-set id (3421 distinct sets), pinned outputs and CPU.
    All 4497 cases are ok and stable (warm re-run gives identical outputs).
  - **Warm pass (c2).** Every case runs twice, warm, in one process. Lines that only warm runs execute (cache-hit
    branches the cold forks never take) are stored as `warm_only`, and touching one keeps the test (see Risks).
- **Run.**
  1. Keep the test if a touched statement is in setUpClass / setUp / non-case lines, in `serde.py` (the kwargs were
     pickled at build) or on a warm-only line.
  2. Otherwise, re-run the cases whose cold coverage contains a touched statement, in a forked child with `__code__`
     swapped (subtest's `_swap`), cheapest first. Each case is held to the test's own assertion and to the pinned duckdb SQL.
  3. All pass → drop the test. No hit case → drop for free: the per-test lines map is coarse for pool tests. It credits every TestOptimizer pool test with
     lines it never runs, e.g. `tokenizer_core.py:794` and `parser.py:9342`. About a third of the drops are this kind.
  4. Gate as subtest: scorer CPU (2x) ≤ min(6 s, 0.6 × saving), plus 0.25 s fork/swap overhead.
- **Why it is sound.** A case that runs no touched statement (cold or warm-only) executes identical code from identical inputs.
  Cases are independent subTests, so the test fails iff some case or the non-case code fails. Every re-run case
  is checked against exactly what the test asserts.

## Where the candidates go (P, c1)

63 kept by the gate: mostly test_tpcds (8.7 CPU s for 99 cases) or many annotate_funcs cases hit.
23 had a failing case (presumed real mutant failures; not spot-checked with pytest).
15 kept because a touched statement is in non-case code (setUp's `DataType.build` = 424 tokenizer / parser lines;
test_qualify_columns 12k and test_simplify 7k non-case lines).
36 dropped: 24 after re-running 1–72 cases (0.1–0.6 s child), 12 with no hit case.

## Risks

- **Cache-hit paths (fixed in c2, not yet measured on P).** A cold fork misses branches a warm pool worker takes.
  A mutant on a memo-hit line could change a warm case without any cold case running it. The `warm_only` guard
  closes the same-order version of this. Pool scheduling order across tests is nondeterministic, so the
  guard is approximate. No observed miss either way.
- **Pool vs fork.** Cases run in spawned workers. The child is forked from the selector, where sqlglot is imported and nothing has run, and its
  recursion limit is set to 1000 as subtest does. Logger patches (`@patch("sqlglot.generator.logger")`) only touch logging.
- **Store fingerprint.** It covers sqlglot/**.py, fixtures/optimizer/**, tests/helpers.py and tests/test_optimizer.py.
  A test-file edit is never a diffexec job.

## Not built: dialect `validate_*` / transpile identity slicing

That needs per-statement slicing of test bodies, not fixture data. The same "never reaches `[]` alone" logic applies.
The real `[]` gain lives in the dialect tests: hundreds of `validate_identity` calls per method,
each ~1 ms. Unmeasured guess: if ~10 % of commits have a residual of only dialect / transpile tests (~2.5 s each) and they all pass, that is ≈ −10 s / 40. Effort is about 1 day, so it is out of scope.

## Reproduce

Laptop scratch `m15/`: `runsel.py` (sharded selector run that keeps the selections), `cmp.py` (A/B), `truth.py`
(apply patch + real pytest on dropped tests), `miss.py` / `lab2.py` (labeled misses), results `P_*.json`,
`L_*_{base,c1}.json`, `P_truth.json`.
