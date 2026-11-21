# Moonshot 03: prove edits equivalent with value profiles

## Verdict: NO-GO. The gain is about 1–2 s per 40 practice commits, with 2–4 MB of store and a new hole in completeness.

| per 40 commits (simulated s; local selector = main without slowverify) | P (600) | Q (1000) |
|---|---|---|
| current selection cost | 326.6 | 318.4 |
| **(iii) site evaluated, every evaluation provably the same value** | **1.1** (sync pool: 2.1) | **0.9** (2.3) |
| (iii) + (a) site never evaluated because an enclosing `and`/`or` short-circuited it (positive evidence) | 1.7 (3.8) | 1.9 (4.2) |
| (iii) + (a) + (b) site simply never evaluated (statement-level coverage, no evidence) | 4.5 (12.5) | 4.6 (12.4) |

- **Run 1** uses the real spawn pools. Their children are not recorded, so `test_optimizer.py` and the three `_mp_execute` TPC tests are kept. This is the sound-ish number.
- **The bracketed numbers** come from run 2, which uses an in-process `SyncPool` (see below). They are optimistic and are **not sound**.

**Soundness on the labeled sets** (private264, private41, sample, practice_like, adversarial, adversarial2):
- Buckets (iii) and (iii)+(a) dropped **0 failing tests** in both runs.
- Bucket (b) missed failing tests in both runs:
  - Run 1, private: 6 tests, all `test_execute_tpcds`. Its work runs in a spawn pool that was not recorded.
  - Run 2, sample `3utz`: 23 tests. The edit is in `serde.dump`. That function runs only when an Expression is **pickled** to be sent to the spawn pool. With an in-process pool nothing is pickled, so the site looks unreached.
- Bucket (b) is therefore the same "SyncPool vs spawn" hole as diffexec #3, now shown to cause misses. It must not ship without recording inside the real children.

**Labeled ceiling.** On the sample, commits with no failing test cost 21.6 s of our 118.7 s. Value profiling can only ever reach the part of that 21.6 s that sits in a Compare or BoolOp edit; the sample measures 4.1 s.

## Why the gain is small (the mechanism, measured)

1. **Most practice edits are not expression-local.**
   - On P, 650 of the 687 s spent on `False->True`, and 465 of the 498 s on `True->False`, are keyword arguments, defaults and returns. They have no Compare/BoolOp site, so the value differs on every evaluation.
   - Whether that difference matters is decided downstream. That is the territory of diffexec (function-level replay), not of expression equivalence.
   - `0->1` and `1->2` are mostly subscripts or arguments: 116 of 168 s have no site.
   - `not ->` removal always flips its value when evaluated.
2. **Where a site exists, the old and new values usually disagree, or cannot be decided.** Non-equivalence reasons on P (sync run), per (test, site) check:
   - `new-needs-unevaluated` 2812: e.g. `a and b` → `a or b` with `a` falsy. The new code evaluates `b`, which the old code never evaluated, so its value is unknown.
   - `skips-impure` 1237: with `a` truthy, the new code skips `b`, but `b` is a call such as `self._match(...)`, which advances the parser.
   - `C:differs` 728: `==`↔`!=` and `is not`→`is` always flip.
   - `truth-differs` 48.
   - Q shows the same pattern.
3. **The proofs that do succeed are already cheap.**
   - Most of the (iii) gain is `is`→`==` on enums or None: 19 s of the 31.5 s on P.
   - diffexec already took those commits down to one cheap test (2haz, 66ks, h2yq: 2.0–2.2 s → 0). The proof saves only the 2 s worker charge.
   - For `and`/`or` commits that cost 25 s, the cost is a slow test that does evaluate the site with differing values, or that is kept as incomplete.
4. **Labels agree.** In private264, `code:and->or` costs 227.7 s against an oracle of about 209 s. Those mutants really do break most of the selected tests, so there is little left to prove away.

## What was built (prototype, `research/moonshots/03-vprof/`)

**`vprof_plugin.py`** (pytest `-p`):
- An import hook compiles sqlglot from source with every `BoolOp` operand and every single-operator `Compare` wrapped in a recording call.
  - It is a `get_code` override with `dont_write_bytecode`, so no instrumented .pyc is written.
  - The `_vp_*` builtins have no double underscore, which avoids class-scope name mangling.
- Records are kept per test phase (setup, call, teardown, and `<out>` for collection and import), keyed by site (file, line, col, end line, end col, kind):
  - `BoolOp`: the vector of operand states. Codes: not evaluated, falsy, truthy, None, False, True, `bool()` raised, plus an "operand raised, evaluation unfinished" marker that a stack flush adds at the end of each phase.
  - `Compare`: abstract operand values (exact for None, bool, |int| ≤ 1000, float, str of 24 characters or fewer, and enum members; otherwise the type), whether the result is a bool, `a is b`, and `a == b`. The last is computed under a re-entrancy guard; without the guard the run was 60x slower.
- `VPROF_SYNCPOOL=1` replaces `ProcessPoolExecutor` with a synchronous executor.

**`vcheck.py`:**
- Takes a minimal AST diff of old against new.
- Climbs to the innermost Compare or BoolOp whose new counterpart can be written over the old operands plus constants.
- Evaluates the new expression symbolically on each recorded evaluation. Short-circuiting is modeled exactly:
  - The new code may evaluate only operands the old code evaluated.
  - Operands it skips must be pure (Name, Constant, Attribute, Compare or Not of those).
  - In a test context the two results must have the same truthiness; elsewhere they must be the same operand or the same singleton.
- A test is dropped if every evaluation in its phases, plus its class's setup phases, is equivalent.
- If an evaluation in `<out>` is not equivalent, the whole commit is kept.
- Pack and cost use `selector/pack.py`.

**Cost:**
- **Profiling:** the full suite runs about 9–10x slower. Run 1: 7 min 12 s wall on 8 laptop workers, 16 min CPU. Run 2: 5 min wall, 10.5 min CPU. The tail is `test_max_nodes`, at about 5 min on one worker.
- **In the Docker build** (4 cores, about 2x slower than the laptop): roughly 10–15 min. That does not fit the 10-minute build unless the store is recorded offline and committed.
- **Store:** 5,049 sites executed and 559k (test, site) pairs. That is 4.1 MB with LZMA, or 2.3 MB if a pair holding more than 16 distinct entries is capped to "unknown". The repo has about 2.4 MB of headroom, so the capped store only just fits.
- **Run-time check:** milliseconds per commit with a per-file shard. Loading the whole store takes about 15 s, so sharding is required.

## Soundness conditions (if anyone revives this)

1. **Complete per-test profiles.**
   - Every evaluation of the site in every process the test causes must be recorded. That includes spawned pool children *and* parent-side pickling.
   - Run 2 demonstrates the failure: SyncPool hides `serde.dump`.
   - The only acceptable recorder is the real pools with a sitecustomize-style hook in the children that tags records with the submitting test.
   - Tests that fail while being recorded, or that run a subprocess (`test_lazy_load`), must be kept.
2. **Order and cache warmth.** A test's evaluations depend on which earlier tests warmed caches. Cross-test leakage also applies: a non-equivalent evaluation in T1 can write a memo, a lazily imported module or a class table that a dropped T2 reads. These are the same holes as diffexec #1 and #2 and the lines map; this mechanism shrinks none of them.
   - A conservative rule: if the site's function runs at import time or writes memoized state, any non-equivalent evaluation anywhere keeps the whole commit.
3. **Side effects.** A skipped operand must be pure. Attribute loads are assumed pure, and a property with side effects would break that. The is/eq bits come from the profiler calling `__eq__` itself.
4. **Abstraction fidelity.**
   - Enum members are keyed by type name and member name. Run 1 used the bare qualname; it should be module plus qualname.
   - Identity of equal ints and strs is never assumed.
   - A Compare whose result is not a bool, or that is chained, is unknown.
5. **Bucket (b) is not value profiling.** It is statement-level coverage for cheap tests. The team deliberately limits that to slow tests. Measured here, it caused misses whenever recording was incomplete.

## Smallest experiment that could change the verdict

Extend the BoolOp evidence by eagerly recording the truthiness of pure, unevaluated `Name` operands; this targets the largest blocker, `new-needs-unevaluated`. The upper bound is roughly doubling (iii) to about 2–4 s per 40 commits. That is still far below the cost of a 2–4 MB store and a new recorder in the build. **Not recommended.** Spend the time on slow-test verification or on a finer diffexec instead, since the value-flow question that matters here is downstream of the edited expression.

Reproduce:
```
pytest -p vprof_plugin -n 8 tests          # PYTHONPATH=03-vprof, VPROF_OUT=dir, on a copy of sqlglot
python 03-vprof/vcheck.py PROFDIR SQLGLOT DATA SEL.jsonl META|- [answers.json] -v
```
