# Moonshot 15: per-fixture-case slicing for code edits (branch `moon-15`)

## Verdict and numbers first

**It is sound and it works, but the gain is small. It is not a moonshot.**
- The prototype is `selector/cases.py` plus an 8-line hook in `select.py`.
- It is a fourth run-time pruner after subtest / slowverify, with the same gate: clean function-body edits, diffexec jobs, not `_mixed_py`.
- c2 is the version on the branch: lazy store, bisect lookup, per-test verdicts in grandchildren, warm-only guard.
- It was measured on rwx-big in the pinned image (drun, 4 shards), A/B against `BR_CASES=0` on the same box.

| c2 on rwx-big | P (600 unlabeled) | labeled (566 = sample 30, practice_like 120, private 264, private41 41, adv 46, adv2 65) |
|---|---|---|
| Commits with ≥ 1 test dropped | 39 (6.5 %) | 34 (6 %) |
| Sim s saved | 68.5 s = **−4.6 s / 40** | 53.6 s = **−3.8 s / 40**: practice_like −17.6 s/120 = −5.9/40; private −24.4/264; private41 −8.6/41; adv2 −3.0/65; adv 0; **sample 0** (laptop c1: −1.4 on bj7g) |
| Reaches `[]` | **0** | **0** |
| Misses | P: real pytest of every dropped test in its patched tree (`truth.py`): c1 36/36 commits (150 tests) PASS; c2 (pinned image): **39/39 commits, 180 dropped tests, all PASS**. Negative control: the tests verification kept in 0hs5, 21wp and 73wr all FAIL in the same harness | **0 misses.** 490 labeled-failing optimizer tests reached the pruner and all were kept; 198 tests dropped, none labeled failing |
| Verify-child CPU | 14.9 s per 600 → ×2 scorer = **2.0 s / 40** | ≈ 1.0 s / 40 (practice_like) |
| Parent CPU (lazy store load 0.37 s + lookup) | +3.3 s per 600 → ×2 = **0.4 s / 40** | +0.4…0.8 s per set |

**Net per 40: P ≈ −2.2 s, practice_like ≈ −4.5 s, sample ≈ +0.5 s** (CPU only, nothing dropped).
The sample is closest to the practice board. Expected practice gain: **≈ −1…−2 s / 40**, about −2…−3 s on the
60-commit final. Most of the CPU is fixed cost per verification: a fork plus `_swap`'s `gc.get_objects()` walk plus the
helper namespace, about 0.2 s even for one case. Forking the verifier from the diffexec child, which has already swapped, would cut that roughly in half.

**Why it never reaches `[]`.** None of the 39 + 34 commits with drops becomes empty. Something else always stays: dialect tests,
test_transpile, the TPC tests, test_scope, or optimizer tests whose non-case code is touched. The residual after the drop is ≤ 2.5 s in about 8 P commits.
Moonshot 10's V1 (run cheap residuals in-process) is what would turn those into `[]`, and the two compose.
The 106 P commits whose selection lies entirely inside the 23 target tests ("ceiling 268 s") are mostly fixture
edits (`tests/fixtures/optimizer/*.sql` → readers) and definition-time edits. All 8 fixture commits in the sample fail their
test, so slicing fixture edits by case gains nothing (not built).

**P(works on the board as measured) ≈ 65 %. P(moonshot) ≈ 0 %. Expected gain ≈ 1–2 s per 40 practice commits, 0 misses.**
Ship it only if a round has spare capacity. Landing it needs a Dockerfile line, `PYTHONHASHSEED=0 python -m selector.cases build /opt/sqlglot /app/index/cases.pkl 4`. That line is NOT on the branch. Measured build: 3m15s wall with 8 jobs in the pinned image, about 5.6 CPU-min, mostly fork syscalls, plus a ~1 min single-threaded warm pass. Whoever lands it must check the total image build against the 10-min limit (
The store is 0.76 MB in the image and nothing goes in the repo. The CPU figures were measured on rwx-big, and ×2 is the c7i-calibrated scorer factor, so treat it as approximate.

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

## Where the candidates go (P)

63 kept by the gate: mostly test_tpcds (8.7 CPU s for 99 cases) or many annotate_funcs cases hit.
23 had a failing case (presumed real mutant failures; not spot-checked with pytest).
15 kept because a touched statement is in non-case code (setUp's `DataType.build` = 424 tokenizer / parser lines;
test_qualify_columns 12k and test_simplify 7k non-case lines).
36 dropped: 24 after re-running 1–72 cases (0.1–0.6 s child), 12 with no hit case.

The c2 counts on P are similar. 66 were kept by the gate, 18 because non-case code was touched, 12 were dropped with no hit case and 41 were verified.
The 41 verified commits produced all of the c2 drops beyond c1: with per-test verdicts, a failing case in test A no longer keeps a passing test B.
Examples are 73wr (1 of 3 dropped), r007 (4 of 5) and dnc2 (3 of 4).

**Bigger levers inside this design (not built).**
- **The tpcds gate.** test_tpcds costs 8.7 CPU s for 99 cases, against a recorded 2.2 s, so it is gated out
  whenever many queries hit. Its average optimize case is ~90 ms.
- **Non-case code.** Re-running setUp's `DataType.build` calls and comparing the schema would free the 18 commits kept
  this way. Expect about 1 s / 40.
- **Dialect `validate_*` and transpile identity.** These need per-statement slicing of the test body. That is where `[]`
  would actually happen. It is out of scope here and would take about a day.

## Risks

- **Cache-hit paths: checked empirically, none found.** A cold fork could miss branches a warm pool worker takes, so
  a mutant on a memo-hit line might change a warm case without any cold case running it. The warm pass on EC2
  ran all 4497 cases twice in one process and found **0 warm-only lines**. A separate check ran 400 cases × 2 rounds
  and saw 1.9 M lines, 0 of them new. Each cold case already hits its own caches, because lookups repeat within one
  query. The guard stays in as a build-time tripwire. Cross-test order effects in the shared pool remain
  theoretically possible.
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
`L_*_{base,c1}.json`, `P_truth.json`. c2: rwx-big `~/work/m15/res/` (`P_{base,c2}.json`, `L_*_{base,c2}.json`, `P_truth_c2.json`), driver `ec2run.sh build|P|L`.
