# Moonshot 11: assertion-level slicing

**Verdict: sound and cheap per commit, but small. Do not build it for the final.** On the eligible
commits it gives the same verdicts as running the whole tests. It does so at a few percent of the
CPU. The payoff is still small, because it only applies to clean function-body edits.

| Set | Net change per 40 commits (gated, warm-up included) |
|---|---|
| sample | about +5 s |
| private264 | about -2 s |
| P (practice-style) | -2.6 (net); +0.4 without the warm-up charge |
| Q300 | -2.6 (net); +0.4 without the warm-up charge |

- After two soundness fixes: **0 false drops in 2,811 labeled verdicts** (sample and private).
- The prototype found **two false drops** first. Both are fixed (§Soundness). One of the two is a
  coverage hole that other line maps built with `sys.monitoring` LINE events alone may share.
- Chance it helps the final measurably (more than 2 s per 40): **about 10%**.
- Expected gain: **about 0 s per 40.** It ranges from -2.6 to +0.5 on P, and is +5 on sample.

## Mechanism (prototype: `11-assertions/asrv.py`, built on moon-02 `forksrv.py`)

**Unit.** A unit is either of two things:
- a call to `Validator.validate_identity`, `validate_all` or `validate_transpile`, keyed by
  (caller file, call position, occurrence within the test);
- one fixture case of `TestOptimizer.check_file`, keyed by (call position, occurrence, case index).

**Skippable.** A unit is skippable only when both hold:
- its return value is discarded: the call is a bare `ast.Expr` statement;
- it is not inside a `with assertRaises / assertLogs / assertWarns`.

**Build** (pinned code). For each test:
1. Fork once and enumerate the units. Record each unit's argument signature (md5 of the repr) and
   its CPU.
2. For every skippable unit, take a **cold fork** of the warm parent (imports only) and run the test
   with every other skippable unit skipped. Record the sqlglot lines that *this* unit runs, using
   `sys.monitoring`. Then abort the test.

Cold recording matters for caches:
- A lazily built cache (a dialect instance, the generator dispatch, the typing tables) shows up in
  the coverage of *every* unit that needs it, not only in the first unit that fills it.
- So if a skipped unit reads a cache that changed code would fill, its own cold coverage contains
  the code that fills it, and the unit counts as affected.

Size and cost:
- 1,187 tests (1,020 dialect, 167 optimizer). 13,116 units, of which 11,821 are skippable.
- On average 1,517 lines per dialect unit and 2,273 per optimizer unit.
- **Store: 79 MB pickle, 1.1 MB lzma.** It fits in the repo.
- **Build CPU: 1,185 s on the laptop** (724 s dialect, 462 s optimizer; 75% of it is cold forks).
  With 4 cores that is roughly 5-10 min, too much for the image build. So record it offline and
  commit it, like the diffexec store. The cost could also be cut a lot: setUpClass is now repeated
  in every unit fork.

**Run.** A commit is eligible only when every edit is a `sqlglot/*.py` change inside function bodies:
- the same code tree, with no changed module or class body, even in modules not loaded yet;
- no changed code object that ran during warm-up;
- the line is not the first line of a code object (see §Soundness).

For an eligible commit:
1. Fork the warm parent and swap `f.__code__` (as in moon-02).
2. Expand each changed line to its statement. For a simple statement that is all of its lines; for
   a compound statement, its header.
3. In one fork per commit (setUpClass once per class), run each candidate test **for real**: setUp,
   all residual test code, and every non-skippable unit run as normal. Skip a skippable unit only if
   both hold:
   - its cold lines miss the changed statements;
   - its argument signature at run time equals the recorded one.
4. If the test passes, drop it.

Code outside the units never needs a coverage check, because it really runs on the new code.

**Gate** (ex ante). For each test, predicted CPU = the test's recorded residual CPU plus the
recorded CPU of the units that must run. Attempt a test when predicted CPU ≤ θ. Attempt the commit
when the most it could save exceeds 2 × predicted CPU.

## Results (sliced runs on EC2 / rwx-big containers; selections = main 4826380 from moon-02)

Upper bound with a perfect cheap-test oracle (`ceil*.py`), dropping every passing test under 3 s:

| | sample | private264 | P |
|---|---|---|---|
| any commit | 44.4 | 46.1 | ≥ 27.4 (381 of 600 oracled) |
| only commits with function-body edits (diffexec ran) | 16.7 | 17.5 | ≥ 12.9 |
| only dialect tests, function-body commits | 9.3 | 3.6 | ≥ 4.0 |

Slicing as measured. CPU is charged at 2× the measured CPU, plus 2 × 1.5 s of warm-up per 40
commits:

| | sample | private264 | P | Q300 |
|---|---|---|---|---|
| eligible (sliceable) commits | 8 / 30 | 47 / 264 | 165 / 465 | 83 / about 250 |
| verdicts vs labels | 70 / 70 exact | 2,740 exact; 1 extra fail; **0 false drops** | – | – |
| gain, θ = 0.05 | 9.3 | 0.7 | 0.9 | 0.9 |
| scorer CPU, θ = 0.05 (+3.0 warm-up) | 1.0 | 0.3 | 0.6 | 0.5 |
| **net per 40, θ = 0.05** | **+5.3** | **-2.5** | **-2.6** | **-2.6** |
| net per 40, θ = ∞ | +6.1 | -8.6 | -2.0 | -3.0 |
| commits → [] per 40 | 4.0 | 0.3 | 0.3 | 0.3 |

Where the sample gain comes from: 2qn5, 8y7j and fryt each go from about 2 s to 0, and bj7g gains a
little. Each is a small set of dialect candidates that all pass.

**Why the measured gain is so far below the ceiling** (private264, 195 commits with cheap
candidates in the store):

| Reason the commit was not sliced | Commits |
|---|---|
| module or class body edit (dialect settings, tables) | 43 |
| fixture-only edit | 37 |
| test-file edit | 17 |
| pure insertion | 12 |
| other (tree shape, warm-up, syntax error, first-line edit) | 26 |
| **sliced** | **47** |

- **Commits that were sliced** gain little:
  - most of their candidates fail anyway;
  - or the gain sits in tests with no units (`test_annotate_funcs`, `test_scope`,
    `test_optimize_in_nulls`, executor tests);
  - or a hot changed line affects most units. Then the "slice" is the whole test, and with
    θ = ∞ the CPU loses.
- **Fixture-case slicing** (a changed optimizer fixture case runs alone; `fix.py`) has a ceiling of
  only 0.0 / 1.9 / 0.8 s per 40 on sample / private / P. Nearly every fixture edit fails its test.
- P (465 commits with cheap store candidates): 165 sliced; 108 module or class body edits, 101
  fixture-only edits, 43 pure insertions, 24 with changed code that ran during warm-up, 7 first-line
  edits. Best commits there: 3dwo, 94ik, ymum, 75de, orfb and 89l3, each about +2 s. Only about 3 per
  40 commits are attempted at all.
- **Module and class body edits** are the large ineligible group. Slicing them needs the
  state-diff machinery (which attributes changed) together with attribute-read coverage per unit.
  That is a separate project.

## Soundness

Every case points toward keeping the test.

1. **Coverage hole in `sys.monitoring` LINE events (private rf8w, false drop).**
   - With only LINE enabled, CPython 3.13 emits **no LINE event for a code object's first line**.
     That makes one-line lambdas (`"TRY_TO_BOOLEAN": lambda args: ...` in parser FUNCTIONS tables)
     and one-line defs invisible.
   - Reproduced in isolation: `f = lambda a: a + 1` gives no event for its line, and `def h(a):
     return a + 1` gives none.
   - Fix: also enable PY_START and record `co_firstlineno` (now in the recorder). The prototype
     store was recorded before the fix, so it runs with a `--line-only-store` rule instead: an edit
     on a code object's first line is not eligible.
   - **Check for main:** `mapbuild/import_time.py` uses LINE alone, so it may have the same hole.
     `trace_one.py` also enables PY_START, so it is fine.
2. **Class-attribute edit in a module not loaded at warm-up (private 22yd, `SUPPORTS_ORDER_BY_ALL`,
   false drop).**
   - The swap path skipped `plan_swap` for lazily loaded modules.
   - Fix: refuse any changed code object that is not a function (CO_OPTIMIZED clear), whether or
     not the module is loaded.
3. **Remaining assumptions:**
   - Caches are transparent memoisation: a key filled by unchanged code holds the same value either
     way.
   - The test is deterministic and does not depend on order. This is the same exposure as the
     existing line maps.
   - Skipped units do not mutate locals. Their arguments are literals, and the signature check
     catches a different call sequence.
   - Units inside `assertRaises` / `assertLogs` are never skipped. Skipping them would only cause
     a spurious failure, never a miss.
   - Any timeout, crash, signature mismatch or untrusted process is treated as a failure, so the
     test is kept.

## Recommendation

Not worth integrating 15 h before cutoff. On practice-style commits it is roughly neutral, and the
warm-up of the test modules (about 3 s scorer per run, pandas and duckdb included) eats most of
the gain. If anything is taken from this work:
- **(a)** Check `import_time.py` for the first-line LINE hole. This is a possible miss source in
  main and costs nothing to check.
- **(b)** A lighter variant: dialect units only, no `tests.test_optimizer` import, warm-up about
  0.4 s scorer. It keeps the sample gain (+8 s per 40) and drops the private and P losses to about
  0. Still under +2 s per 40 on practice-style commits.

Files: `11-assertions/asrv.py` (recorder and runner), `econ2.py` (gated economics), `ceil*.py`
(ceilings), `fix.py` (fixture ceiling), `bigjob.sh` / `ec2job.sh` (runs). Raw outputs are in the
scratchpad `m11/` (not committed).
