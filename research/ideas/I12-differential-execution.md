# I12 — Differential execution (record call inputs at the pinned commit, replay old vs new f)

Branch `r1-diffexec-main` is built on main 9b775a4 (c8b4927 plus log commits). The code is in
`diffexec/` and `selector/diffexec.py`. Without a store in the image, the selector output is
**byte-identical to main** on sample, private264, private41 and adversarial46. The 61 unit tests
pass.

## Round 8 (branch `r8-diffexec2`, merged with main 3acd5e1): wider coverage

**EC2, pinned image, scorer flags, store recorded in the pinned image, 3 interleaved runs
(main, branch, main, …), median container CPU. 0 misses on every set, identical outputs across runs.**

| Set | Simulated s, main → branch | Container CPU s, main → branch | Net s |
|---|---|---|---|
| sample | 149.047 → 118.177 | 2.23 → 4.36 | −28.7 |
| private264 | 2713.822 → 2678.447 | 25.01 → 30.10 | −30.3 |
| private41 | 434.504 → 428.855 | 5.31 → 5.98 | −5.0 |
| adversarial46 | 1168.639 → 1168.639 | 7.71 → 7.55 | −0.2 |
| adversarial2 (65) | 994.061 → 993.783 | 15.33 → 15.39 | −0.2 |

Store: `maps/diffexec.zip` 13.1 MB (was 9.4 MB), git tree 17.6 MB. 2,039 functions kept (with
46.5k (function, test) pairs); 4,134 left out by the budget, 285 generators, 220 over 50k calls.

**What changed:**
1. **Elided stand-ins (`serial.Elided`).** An input over 16 kB is pickled again with every
   exact list / dict / set of more than 32 items written as a length-only stand-in (persistent
   ids; aliasing kept). A stand-in answers `len`, `bool` and identity like the original. Any
   other use — item access, iteration, comparison, hashing, attribute lookup (incl.
   `__class__`), operators, pickling, and any builtin / class / slot wrapper called with it as
   first argument (`type`, `id`, `isinstance`, `list`, `list.__len__`, …, watched through
   `sys.monitoring` CALL events during the replay) — sets a flag and raises a `BaseException`.
   A flagged replay is not ok even if the code swallowed the exception. The effect digest
   writes the same containers as their stand-in key on both sides (record: by id). So an
   accepted call saw the elided data only through len / bool / identity, which the stand-in
   reproduces. Executor `Context` inputs (row tables, `env` with modules, now pickled by
   name) record this way: **66ks / h2yq (`Context.table` `is None` → `== None`) 24.8 → 2.1 s**.
   The tpcds blobs replay ok; `test_sql_boolean_logic_with_numpy_scalars` (numpy is not in the
   run image) and 4 tests whose replays disagree with the recording are kept.
2. **Completeness per (function, test phase), not per function.** An input still too big,
   unpicklable, or over the function's per-file byte budget (2 MB) marks only that phase
   incomplete, and later calls in that phase are not pickled. Tests whose own calls were all
   recorded stay droppable. (Before: one big input killed the function for every test.)
3. **Generators' `_dispatch`.** A Generator whose `_dispatch` is the shared per-class table is
   pickled without it and rebuilt like `__init__` does (34 kB → 1 kB per `self`).
4. **Effect digests with the C pickler** (`FastCmpPickler`, ~3x faster): str / tuple / bytes /
   frozenset go through persistent ids that carry their value, so the bytes do not depend on
   interning; mutable sharing is still in the bytes. Digests are stored as 8 bytes.
5. **Store budget.** LZMA zip; phases as ints into a test table. The build reads the
   recordings twice, in buckets of functions (memory ~2.6 GB for 4 GB of recordings). Per
   function, "all droppable tests" or "only tests ≥ 0.5 s" is an option worth
   lines(f) × saving^1.5, where saving is what a commit changing only f would save at the
   scorer's packing cost (tests.json seconds, never labels); options are taken greedily per
   packed byte up to 13.5 MB (estimate). The power 1.5 and the budget were picked on the
   labeled sets' totals — **this knob is tuned in-sample**. Savings over the four sets (sample,
   private264, private41, adversarial; laptop recordings, `diffexec/measure.py`): power 1 at
   budget 11.5e6 ≈ 63 s, power 2 at 11.5e6 ≈ 90 s, power 1.5 at 11.5e6 ≈ 106 s; power 1.25 at
   13.5e6 ≈ 114 s, power 1.5 at 13.5e6 ≈ 115 s (shipped). The deciding difference: with power 1
   (a uniform-over-functions prior) `Context.table` (~105 kB packed) does not fit — about 4,000
   cheaper functions rank above it — so 66ks / h2yq (≈ 45 s of the gain) are lost. The choice
   of functions and tests never looks at labels, but the power was picked knowing that.
6. **Parent-side gate.** Before forking the replay, the selector reads the changed functions'
   phase table and computes the benefit over the candidates that could be dropped at best;
   nothing possible → no replay.
7. **Recorder memory.** Recordings are a pickle stream per function, inputs zlib-compressed in
   memory. (The first EC2 run of test_optimizer / test_executor was killed by host memory
   pressure from other jobs; they were re-recorded with `RESUME=1`.)

**Discrimination (laptop, this design's store):** failing candidates kept because a replay
*differed*: 15 on sample; failing tests dropped: **0** on all sets
(`diffexec/measure.py`, `bad_drops`). `ary6` (10 failing) now drops 91 non-failing candidates
and keeps exactly the failing ones that differ: 9.0 → 4.5 s.

**Not done / limits:** functions over 50k calls are still not recorded (pickling every call is
too slow); only the first 2 calls per distinct input are digested; tuple elision exists
(`DIFFEXEC_ELIDE_TUPLE_K`) but is off (it made Context inputs touch the stand-ins and did not
reduce distinct inputs). Container CPU grows on private264 (+5 s): more replays, larger blobs.
New soundness assumption: no code path inspects a stand-in from C without going through its
type slots or a Python-level call (e.g. a C extension doing `PyList_Check` and taking another
branch silently); sqlglot is pure Python. Two channels are handled or documented:
structural pattern matching (`MATCH_SEQUENCE` / `MATCH_MAPPING` test type flags directly) —
the pinned sqlglot has no `match` statement, and replay refuses stand-in inputs whenever a
changed code object uses one (`replay.uses_match`); and a C-to-C call such as `map(type, xs)`
or `sorted(xs, key=id)` applied to a stand-in emits no CALL event — contrived, not handled.

## Shipped (round 2): pinned-image store, on main d2e0730

Branch `r1-diffexec-main`. The store is `maps/diffexec.zip`, built by `mapbuild/diffexec_build.sh`
in the pinned image.

**How the store is built:**
- A census pass counts calls first, so functions that will go over the cap are never recorded.
- Then one pytest process runs per test file.
- Caps are applied after merging: 50k calls, 300 kB of distinct inputs, 16 kB per input. The
  store therefore does not depend on the worker count.
- The effect digest is taken for the first 2 calls of each distinct input.
- Pickling stops as soon as an input is over the input cap.
- Recording is skipped while tracemalloc traces, and those phases are marked incomplete.
- Tests that fail while being recorded are listed in the store and are never dropped. One test
  did: `test_dialect_attribute_access_is_cached`.

**Size and build time:** 3,488 of 6,678 functions are recorded. The store is 9.4 MB and the
checked-out tree is 13.9 MB. A no-cache Docker build takes 36 s.

**EC2, pinned image, Makefile flags, median of 3 runs, 0 misses on every set:**

| Set | Simulated s, without → with | Container CPU s | Net s |
|---|---|---|---|
| sample | 174.97 → 149.05 | 1.23 → 2.16 | −25.0 |
| private264 | 2865.29 → 2855.01 | 15.88 → 20.48 | −5.7 |
| adversarial46 | 1184.36 → 1178.00 | 3.86 → 4.84 | −5.4 |
| private41 | 480.14 → 479.96 | 2.71 → 3.40 | +0.5 |

`make sample` gives 149.047 s with 0 misses.

The interim laptop store (branch `diffexec-laptop`, 8df22cc) gives the same selections.

## Verdict

**Feasible, as a store recorded offline and committed to the repo. It is not feasible as a
recording inside the Docker build. It is worth about −26 s on the sample, −10 s on private264
and −6 s on adversarial46, for +0.8 to +4.3 s of container CPU (EC2), with 0 misses everywhere.**

| | sample | private264 | private41 | adversarial46 |
|---|---|---|---|---|
| main (no store) | 183.9 | 3263.9 | 546.2 | 1213.4 |
| **+ diffexec, store C300 (full suite, not label-derived, 8.7 MB gz)** | **158.0** | **3253.6** | **546.0** | **1207.0** |
| replay child CPU (laptop) | 0.50 | 2.42 | 0.28 | 0.33 |
| container CPU without → with store (EC2, pinned image, scorer flags) | 1.10 → 2.19 | 24.7 → 29.0 | 3.35 → 4.15 | 5.77 → 6.98 |
| **net (simulated + CPU) vs main** | **−24.8** | **−6.0** | **+0.6** | **−5.2** |
| misses | 0/30 | 0/264 | 0/41 | 0/46 |

The C300 rows come from `diffexec/measure.py`: simulated cost before and after pruning, summed
over commits. `eval/evaluate.py` with the uncut store C (21.5 MB) gives 157.9, 3252.5, 546.1 and
1207.0.

- **The comparison discriminates.** Across the four sets, store C300 kept 51 failing candidates
  because a replay gave a different effect. With the generous label-targeted store B, the count
  is 534. In every configuration, diffexec dropped **0 failing tests**.
- **Where the wins come from.**
  - 2haz, `Dialect.case_sensitive` `is`→`==`: 25.5 → 2.0 s. All 1,055 distinct
    (dialect, text) inputs replayed equal, so `test_execute_tpcds` and 98 others are dropped.
  - adversarial `imp_formatted_time_closure`: 6.4 → 0.
  - private264 gzzg, hgxu, 81c4, ohgt and hn7m: −1 to −4.6 s each.
  - 5y72, `copy=False`→`True`: 2.5 → 0.
- **Where it cannot help.**
  - **66ks** (`Context.table`, `is None`→`== None`): every executor `Context` input is
    unpicklable, so all 51 phases are incomplete.
  - **Functions whose recorded inputs are huge or too many** (`is_implicit_group_by_all`,
    `Resolver.get_source_columns`, parser and generator methods whose `self` is over 16 KB):
    their recording is dropped.
  - **No-failure commits whose function output really changes** (op04, h_typing*, 2qn5, 8y7j):
    a proof at function level is not enough, because the tests just do not look at that output.
- **Recording inside the Docker build is infeasible.**
  - Strict caps (16 KB per input, 0.5 s per function per worker) cost **5,483 CPU-s** on 16
    laptop workers (13.6 min).
  - Store C caps (16 KB, 1 MB per function, 30 s) cost **8,369 CPU-s** on 14 workers (25.9 min).
  - The build budget is about 2,400 CPU-s (4 cores × 10 min), before `pip install` and the suite.
  - So the store must be recorded offline, in the pinned image, and committed, like the maps.
    C300 is 8.7 MB gzipped (11.5 MB as a tar of 3,641 per-function files). main tracks 4.2 MB,
    so the total is about 13–16 MB against the 20 MB limit.
- **I13 (bytecode equivalence): 0 gain on every set.** Docstrings are code constants, so it is
  strictly weaker than the AST filter we already have. Every `is`→`==` mutant also compiles to
  different bytecode.

**Recommendation.** Adopt the method only together with these steps:

1. Re-record store C300 on EC2 in the pinned image. That is about 8.4k CPU-s, roughly 40 min on
   4 cores, offline. Re-check the gains: they may shrink at 4 workers (hole 9).
1a. Add `python -m compileall /opt/sqlglot/sqlglot` to the shipped Dockerfile.
    The EC2 CPU figures were measured with it. Without `.pyc` files, the one sqlglot import
    recompiles every module on every run, and that could erase the private41 and private264
    nets.
2. Pack the store into a single archive. Commit it with a MANIFEST, as the maps are committed.
3. Rerun the four sets.

The 2haz-type win (an equivalent edit inside a function that tpcds calls with small inputs) is
the one category where the oracle gap is large and nothing else gets at it. Until step 1 is
done, keep it off: without a store the selector is exactly main.

## How it works

**Build time** (pinned sqlglot only, `python -m pytest -p diffexec.record`, `PYTHONHASHSEED=0`,
spawn pools replaced by an in-process pool that still pickles):

- **Hooks.** `sys.monitoring` PY_START (global), PY_RETURN (local, per recorded code object)
  and PY_UNWIND fire on every function under `sqlglot/`.
- **Inputs at entry.** The recorder pickles the arguments and closure cells with `InPickler`:
  - sqlglot `Expr` nodes are written with their full slot state, including `parent` (so the
    whole tree), `_type`, `_meta`, `comments` and `_hash`. It does not go through
    `Expr.__reduce__`/serde, which drops the parent link.
  - Local functions and lambdas are rebuilt from (file, qualname, line) plus their cells.
  - Inputs are deduplicated by blake2b digest.
- **Effect at exit.** The recorder takes a digest of (arguments after the call, result or
  exception) with `CmpPickler`:
  - It is a pure-Python pickler that memoizes mutable objects only, so aliasing ("returns
    self" vs "returns an equal copy") is in the bytes. String interning is not.
  - Functions and code objects are written as a bytecode digest without line numbers, so a
    patched function object compares unequal.
- **Attribution.** Calls are attributed to phases: SETUP|T, CALL|T, TEARDOWN|T, IMPORT, OTHER.
- **Caps per function per worker.** A function over a cap is "dead", and its tests are never
  dropped:
  - more than `CALL_CAP` calls;
  - an input over `INPUT_CAP` bytes;
  - distinct inputs over `BYTE_CAP` bytes;
  - more than `TIME_CAP` s of pickling;
  - generators and coroutines.
  An unpicklable input marks that (function, phase) pair "incomplete". The same input with two
  different effects is marked "unstable".
- **Store.** `python -m diffexec.build` writes `index.pkl` plus one `c/<n>.pkl.gz` per
  function. `DIFFEXEC_BUILD_MAX_BYTES` sets the size budget per function (C300 = 300 kB raw).

**Run time** (`selector/diffexec.py` and `diffexec/replay.py`):

1. **Eligibility.** The commit qualifies only if every behaviour-changing `.py` region is a
   function body with no signature change, the function ran under some test and does not run
   at import, and there is no test-file edit. Fixture edits may be mixed in. Otherwise
   diffexec is skipped.
2. **Two gates.** Benefit: dropping every candidate must lower the cost. Index precheck: every
   changed function must be recorded and alive. Ineligible commits cost 0 CPU.
3. **Replay in a forked child.** The selector re-execs itself once with `PYTHONHASHSEED=0` and
   imports sqlglot once.
   - The child compiles the old and new module and matches code objects by
     (qualname, occurrence). It bails on added or removed functions, a closure-layout change,
     or a class or module body change. Set constants are compared order-free.
   - It swaps `__code__` on every live function object of each changed code object (found with
     `gc`), so recursive and nested calls also run new code. It clears lru caches.
   - For each recorded input, accept only if all of these hold:
     - recorded digest == old-code digest == new-code digest;
     - the old and new runs emit equal log records, warnings and stdout;
     - new CPU ≤ 5 × old + 50 ms.
   - Each call runs under a CPU itimer. The first overtime aborts that function, so a patched
     infinite loop costs at most the budget.
4. **Drop rule.** Test T is dropped iff all of the following hold:
   - every changed function is alive;
   - every global phase (IMPORT/OTHER) is ok;
   - T's own phases are ok, and so are the SETUP phases of T's classmates (setUpClass);
   - T has at least one recorded call (otherwise the map and the recording disagree);
   - T's class uses no resource module (`time`, `gc`, `tracemalloc`, `resource`, `threading`,
     `signal`, …) or probe (`setrecursionlimit`, `getsizeof`, `perf_counter`, …).
5. **Budget.** CPU per commit is at most min(3 s, 0.3 × benefit); per batch it is 60 s.

## Numbers

Laptop: 20 cores, Python 3.13.12, sqlglot @ eb11aec. "EC2" means the pinned image under the
Makefile's scorer flags.

### Recording cost and size

| Recording | Scope | Wall | CPU | Functions alive / 6,678 | Store (gz) |
|---|---|---|---|---|---|
| census (count only) | full suite, `-n 8` | 70 s | 227 s | — | — |
| rec1 strict (16 KB input, 0.5 s, 1 MB) | full suite, `-n 16` | 13.6 min | 5,483 s | 3,208 | 6.9 MB |
| **recC (16 KB, 30 s, 1 MB, 50k calls)** | **full suite, `-n 14`** | **25.9 min** | **8,369 s** | **3,641** | 21.5 MB (C300 budget: **8.7 MB**; C100: 5.2 MB) |
| A strict (16 KB, 0.5 s, 2 MB) | 350 label-changed functions, `-n 4` | 3.1 min | 340 s | — | 0.74 MB |
| B generous (4 MB, 120 s, 200 MB) | 350 label-changed functions, `-n 4` | 27.6 min | 4,235 s | — | 151 MB |

- The suite makes 199.7 M Python calls into sqlglot. `test_max_nodes` makes 70 M and
  `test_execute_tpcds` 66 M.
- Full-fidelity capture costs about 2.5 ms per call. A generator's `self` alone is 34 KB,
  mostly the per-instance `_dispatch` dict, so most generator methods die on the 16 KB input cap.
- **Stores A and B are measurement only.** `DIFFEXEC_ONLY` is derived from the labeled patches,
  so they must never ship. They are a ceiling study. Caps are per worker, so the decisions also
  depend on how tests are split across workers: `case_sensitive` died in A (`-n 4`) and lived in
  rec1 and C.
- Under recording, `test_generator_memory` fails 33 subtests because tracemalloc sees the
  recorder's memory. That test is excluded by the resource rule anyway.

### Gains by store, on main 9b775a4 (0 misses on every row)

| Store | sample | private264 | private41 | adversarial46 | child CPU s (s / p264 / p41 / adv) |
|---|---|---|---|---|---|
| none (= main) | 183.9 | 3263.9 | 546.2 | 1213.4 | — |
| C100 (5.2 MB) | 181.4 | 3263.9 | 546.2 | 1213.4 | 0.25 / 0.96 / 0.12 / 0.06 |
| **C300 (8.7 MB)** | **158.0** | **3253.6** | **546.0** | **1207.0** | 0.50 / 2.42 / 0.28 / 0.33 |
| C (21.5 MB) | 157.9 | 3252.5 | 546.1 | 1207.0 | 0.50 / 3.72 / 0.28 / 0.34 |
| A (label-targeted) | 181.4 | 3258.3 | 546.1 | 1213.4 | — |
| B ceiling (label-targeted) | 153.7 | 3239.7 | 538.5 | 1213.4 | 9.5 / 60 / 10.6 / — |

### Discrimination: failing tests that were in diffexec's candidate sets

| Store | Set | Failing candidates | Kept, replay **differed** | Kept, incomplete | Kept, no data (dead / no calls / precheck / bail) | **Dropped** |
|---|---|---|---|---|---|---|
| C | sample / p41 / p264 / adv | 60 / 44 / 1,490 / 1 | 2 / 4 / 145 / 1 | 0 / 14 / 72 / 0 | 58 / 26 / 1,273 / 0 | **0** |
| B | sample / p41 / p264 | 60 / 44 / 2,623 | 23 / 15 / 496 | 0 / 14 / 73 | 37 / 15 / 2,054 | **0** |

### Container CPU (EC2, pinned image, scorer flags)

- On the v1 base with store A, CPU without → with was:
  - sample 2.00 → 3.1;
  - private41 6.0 → 7.4;
  - private264 51.3 → 55.4.
- The first version spawned a subprocess per replay and re-imported sqlglot each time, about
  0.4 s per replay (sample 2.00 → 4.70). The fork version costs 0.3 s once plus about
  0.05–0.5 s per replay.
- C300 on main (2 runs each, same simulated seconds as the laptop):
  - sample 1.10 → 2.19 (+1.1);
  - private41 3.35 → 4.15 (+0.8);
  - adversarial 5.77 → 6.98 (+1.2);
  - private264 24.7 → 29.0 (+4.3).
- About 0.3 s of each increase is the one sqlglot import plus the index load; the rest is replay.

## Soundness holes (0 misses is necessary, not proof)

1. **Global and class state is not compared.** Mutations of module globals, class attributes or
   caches by f or its callees are outside the effect digest. Cache warmth can also differ
   between record and replay. The three-way check (recorded == old replay) catches state that
   *old* code reads, but not state that only *new* code reads.
2. **Order and grouping.** The recording follows one pytest order (xdist `loadfile`). The
   labelers' grouping can give f other inputs, for example a cache miss in a fresh worker. The
   maps share this hole.
3. **SyncPool vs spawn.** Pool jobs are recorded in-process, but the real run executes them in
   fresh processes.
4. **Fidelity of the inputs.** Anything pickled by reference (classes, module functions, enums)
   is assumed to be identical at replay. An unpicklable input makes the phase incomplete (safe).
5. **Resources are invisible.** Memory, recursion depth and wall time are not compared. The
   only guards are the resource rule and a 5× CPU check, and the labelers' timeout is unknown.
6. **Environment.** Stores C, A and B were recorded on the laptop (3.13.12), not in the pinned
   image. Hash seed 0 is assumed. A mismatch makes replays disagree, so all tests are kept
   (safe, gain lost).
7. **Identity against globals.** f returning a module singleton vs an equal copy looks the same.
8. **Tests that fail while recording have truncated recordings.** In recC, the only failures
   were the 33 `test_generator_memory` subtests: tracemalloc sees the recorder's memory. The
   resource rule excludes that test. Before adoption, `record.py` should record outcomes
   (`pytest_runtest_logreport`) and mark a failing test's phases incomplete.
9. **Caps are per worker.** C was recorded with 14 workers. Re-recording with `-n 4` in the
   pinned image hits the caps sooner, so fewer functions stay alive and the C300 gains may
   shrink.

## Cheaper variants

- **Run the cheap candidate tests at run time (report 03).** About −4 s on the sample for about
  7–9 CPU-s. Diffexec gets those commits too (5y72) at a fraction of the CPU, and it also gets
  2haz, which exec never can because tpcds costs 32 CPU-s.
- **I13 bytecode equivalence.** 0 commits beyond the AST filter.

## Files

| File | Job |
|---|---|
| `diffexec/record.py` | pytest plugin: per-phase call recording with caps |
| `diffexec/serial.py` | `InPickler` (rebuildable inputs) and `CmpPickler` (effect digests), code digests |
| `diffexec/build.py` | merge worker recordings into the store (size budget per function) |
| `diffexec/replay.py` | old-vs-new replay with code swapping, CPU itimer, and log/warning/stdout capture |
| `selector/diffexec.py` | gates, fork server, drop rule, resource rule |
| `diffexec/measure.py`, `report_store.py`, `changed.py`, `try_patch.py`, `i13_bytecode.py` | measurement tools |
| `diffexec/Dockerfile` | measurement image (`STORE=` prebuilt, or `RECORD=1` for an in-build recording) |
