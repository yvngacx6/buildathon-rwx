# I12 — Differential execution (record call inputs at the pinned commit, replay old vs new f)

Branch `r1-diffexec-main` (on main 9b775a4 = c8b4927 + logs). Code: `diffexec/` and `selector/diffexec.py`.
Without a store in the image, the selector output is byte-identical to main on all four sets.

## Verdict

**Not worth shipping as is. The method is sound in practice, but it cannot pay for itself.**

- **The comparison works.** With the most generous recording, 534 failing tests sat in candidate
  sets. Replay found a real difference for every one of them and kept it. Diffexec dropped
  **0 failing tests** on any set.
- **Recording during the Docker build is infeasible.** With strict caps, recording every function
  over the full suite costs **≥ 5,483 CPU-s** (13.6 min on 16 laptop workers). The budget is
  about 2,400 CPU-s (4 cores × 10 min), before `pip install` and the suite itself.
- **A committed, offline store fits on size, but the gain is small.** The strict-cap full store
  is 6.9 MB gzipped (9.5 MB as a tar of the per-function files). The repo is 1.6 MB plus maps.
  The functions that hold the big wins, however, are exactly the ones strict caps lose
  (`Dialect.case_sensitive` dies by the time cap, `is_implicit_group_by_all` and
  `get_source_columns` die by time or size).
- **The ceiling (record everything, ignore cost) on main:** sample 183.9 → **153.7 s**, of which
  2haz alone is −23.5 s. Private264 −24.2 s and private41 −7.7 s. All with 0 misses. Replay CPU
  is +7 s, +55 s and +9 s respectively. Only the sample nets a gain, and that rests on one commit.
- **Shippable-today numbers** come from the strict per-function caps (storeA). On main the sample
  gains −2.5 s, private264 −5.6 s, private41 −0.1 s and adversarial 0. On EC2, in the pinned
  image under scorer flags, the CPU cost is +1.1 s, +4.1 s and +1.4 s. The net is about −1 s on
  sample and private264 and +1 s on private41. **That is noise.**
- **I13 (bytecode equivalence): 0 gain.** It is strictly weaker than the AST filter we already
  have, because docstrings are code constants. Every `is`→`==` mutant compiles to different
  bytecode.
- **Differential execution cannot reach 66ks** (`Context.table`). Its `self` is an executor
  Context, and the inputs could not be pickled in any of the 51 tests. It also cannot reach
  op04, op06, h_typing*, or any other no-failure commit where the function's output really
  changes and the tests just do not look. A proof at function level cannot drop those tests.

Recommendation: **park I12.** If we revisit it, do the "case_sensitive" special case properly.
Record offline (hours of CPU are fine) in the pinned image, with **byte caps only and no time
caps**, then commit the store. Measure first whether such a store stays under about 15 MB. The
generous recording of just the 350 functions changed in the labeled sets was already 151 MB
gzipped, so it probably does not.

## How it works

Build time (pinned sqlglot only, `python -m pytest -p diffexec.record`, `PYTHONHASHSEED=0`,
spawn pools replaced by an in-process pool that still pickles):

- **Hooks.** `sys.monitoring` PY_START (global), PY_RETURN (local, per recorded code object)
  and PY_UNWIND fire on every function under `sqlglot/`.
- **Inputs at entry.** The recorder pickles the arguments and closure cells with `InPickler`:
  - sqlglot `Expr` nodes are written with their full slot state, including `parent` (so the
    whole tree), `_type`, `_meta`, `comments` and `_hash`. It does not go through
    `Expr.__reduce__`/serde, which drops the parent link.
  - Local functions and lambdas are rebuilt from their code key plus their cells.
  - Inputs are deduplicated by blake2b digest.
- **Effect at exit.** The recorder takes a digest of `(arguments after the call, result or
  exception)` with `CmpPickler`:
  - It is a pure-Python pickler that memoizes mutable objects only, so aliasing ("returns
    self" vs "returns a copy") is in the bytes. String interning is not.
  - Functions are written as a bytecode digest without line numbers, so a patched function
    object compares unequal.
- **Attribution.** Calls are attributed to phases: SETUP|T, CALL|T, TEARDOWN|T, IMPORT, OTHER.
- **Caps per function per worker.** A function over a cap is "dead", and its tests are never
  dropped:
  - more than `CALL_CAP` calls;
  - an input over `INPUT_CAP` bytes;
  - distinct inputs over `BYTE_CAP` bytes;
  - more than `TIME_CAP` s of pickling;
  - generators and coroutines.
  An unpicklable input marks that (function, phase) pair "incomplete".
- `python -m diffexec.build` merges the workers into `index.pkl` plus one `c/<n>.pkl.gz` per
  function. Inputs that gave different effects on different calls are marked "unstable".

Run time (`selector/diffexec.py` and `diffexec/replay.py`):

1. **Eligibility.** The commit qualifies only if every behaviour-changing `.py` region is a
   function body with no signature change, the function runs under some test and does not run
   at import, and there is no test-file edit. Otherwise diffexec is skipped (`_mixed_py`).
2. **Two gates.** Benefit: dropping every candidate must lower the cost. Index precheck: every
   changed function must be recorded and alive. These keep the CPU at zero on ineligible commits.
3. **Replay in a forked child.** The selector re-execs itself once with `PYTHONHASHSEED=0` and
   pre-imports sqlglot once.
   - The child compiles the old and new module and matches code objects by
     (qualname, occurrence). It bails on added or removed functions, a closure-layout change,
     or a class or module body change.
   - It swaps `__code__` on every live function object of every changed code object (found
     with `gc`), so recursive and nested calls run new code too. It also clears the lru caches.
   - For each recorded input, accept only if all of these hold:
     - recorded digest == old-code digest == new-code digest;
     - the old and new runs emit equal log records, warnings and stdout;
     - new CPU ≤ 5 × old + 50 ms.
   - Every call runs under a CPU itimer. One overtime aborts that function, so an infinite loop
     cannot hang the replay.
4. **Drop rule.** Test T is dropped iff all of the following hold:
   - every changed function is alive;
   - every global phase (IMPORT/OTHER) is ok;
   - T's own phases are ok, and so are the SETUP phases of T's classmates (setUpClass);
   - T has at least one recorded call (otherwise the map and the recording disagree);
   - T's class does not use a resource module (`time`, `gc`, `tracemalloc`, `resource`,
     `threading`, …) or `sys.setrecursionlimit`/`getsizeof`-style probes.
5. **Budget.** CPU per commit is at most min(3 s, 0.3 × benefit); per batch it is 60 s.

## Numbers

All laptop numbers come from a 20-core laptop with Python 3.13.12 and sqlglot @ eb11aec. They
are labelled "EC2" when they were measured in the pinned image under the scorer's `docker run`
flags.

### Recording cost and size

| Recording | Scope | Wall | CPU | Functions alive | Store (gz) |
|---|---|---|---|---|---|
| census (count only) | full suite, `-n 8` | 70 s | 227 s | — | — |
| rec1 strict (16 KB input, 0.5 s, 1 MB) | **full suite**, `-n 16` | 13.6 min | **5,483 s** (pickling 5,493 s) | 3,208 / 6,678 (48 %) | **6.9 MB** (9.5 MB tar) |
| recC (16 KB, 30 s, 1 MB, 50k calls) | full suite, `-n 14` | RECC_WALL | RECC_CPU | RECC_ALIVE | RECC_GZ |
| A strict (16 KB, 0.5 s, 2 MB) | 350 label-changed functions, `-n 4` | 3.1 min | 340 s | 449 / 612 touched | 0.74 MB |
| B generous (4 MB, 120 s, 200 MB) | 350 label-changed functions, `-n 4` | 27.6 min | 4,235 s | 639 | **151 MB** (963 MB raw) |

- The whole suite makes 199.7 M Python calls into sqlglot. `test_max_nodes` makes 70 M and
  `test_execute_tpcds` 66 M.
- Full-fidelity input capture costs about **2.5 ms per call** (micro-benchmark: 10 transpiles
  took 136 s). A generator method's `self` alone is 34 KB, mostly the per-instance `_dispatch`
  dict.
- **Stores A and B are measurement only.** `DIFFEXEC_ONLY` is derived from the labeled patches,
  so they must never ship. They simulate the full recorder for those functions, except that
  per-worker caps depend on how tests are split across workers: `case_sensitive` was alive in
  rec1 (`-n 16`) but dead in A (`-n 4`).

### Selection gains on main 9b775a4 (`eval/evaluate.py`, laptop; 0 misses on every row)

| Store | sample | private264 | private41 | adversarial46 |
|---|---|---|---|---|
| none (= main) | 183.9 | 3263.9 | 546.2 | 1213.4 |
| A strict (label-targeted) | 181.4 | 3258.3 | 546.1 | 1213.4 |
| B ceiling (label-targeted, default budgets) | **153.7** | 3239.7 | 538.5 | 1213.4 |
| C full suite, not label-derived | RECC_SAMPLE | RECC_P264 | RECC_P41 | RECC_ADV |

- Adversarial is unaffected because its changed functions were not in the targeted list. That
  says nothing about the method.
- On v1 (before the rebase) store A gave 198.8 → 196.3, 625.8 → 625.6 and 3922.0 → 3916.4.
  Store B gave 198.8 → 170.3 on the sample, with 2haz going 25.5 → 2.0.

### Replay CPU (charged)

| Where | sample | private41 | private264 |
|---|---|---|---|
| EC2 pinned image, scorer flags, store A, v1 base: CPU without → with | 2.00 → 3.09–3.12 | 5.96–6.05 → 7.35–7.40 | 51.3 → 55.4 |
| laptop, store B, child CPU (measure.py) | 9.5 | 10.6 | 60.0 (batch cap hit) |

- On EC2 the earlier subprocess-per-replay version cost 0.4 s of CPU per replay, mostly
  re-importing sqlglot (2.00 → 4.70 s on the sample). The fork version brought that to about
  0.05–0.1 s per replay, plus 0.3 s once to import sqlglot.

### Does it discriminate? (failing tests that were in diffexec's candidate sets)

| Store | Set | Failing candidates | Kept, replay **differed** | Kept, incomplete | Kept, no data (dead / no calls / precheck / bail) | Dropped |
|---|---|---|---|---|---|---|
| A | sample / p41 / p264 | 60 / 44 / 2,623 | 2 / 4 / 39 | 0 / 14 / 72 | 58 / 26 / 2,512 | **0** |
| B | sample / p41 / p264 | 60 / 44 / 2,623 | 23 / 15 / 496 | 0 / 14 / 73 | 37 / 15 / 2,054 | **0** |

### Per commit, ceiling (store B, v1 base)

| Commit | Changed function | Candidates | Dropped | Cost | Note |
|---|---|---|---|---|---|
| 2haz | `Dialect.case_sensitive` (`is`→`==`) | 101 | 99 | 25.5 → 2.0 | 1,055 distinct (dialect, text) inputs, all equal; 0.47 s CPU. 2 tests kept (not recorded in their own phases) |
| 5y72 | `_eliminate_cte` (copy=False→True) | 3 | 3 | 2.5 → 0 | |
| ary6 | `BigQueryParser._parse_table_parts` | 111 | 29–101 | 9.0 → 6.3 (4.4 with unlimited budget) | 10 failing tests all kept |
| 66ks | `Context.table` (`is None`→`== None`) | 51 | 0 | 24.8 | every phase incomplete: `Context` inputs cannot be pickled |
| 2qn5, 8y7j, bj7g | ClickHouse generator methods | — | 0 | — | real behaviour change on recorded inputs (2qn5: `<`→`<=` hits version (23,12)?) or dead (input > 16 KB) |
| op04 | `is_implicit_group_by_all` | 54 | 6–26 | 25.5 | outputs differ on 1,430 of 3,471 inputs, but no test fails. Function-level proof cannot drop tpcds |

## Soundness holes (why 0 misses is necessary but weak evidence)

1. **Global and class state is not compared.** Mutations of module globals, class attributes or
   caches by f (or its callees) are outside the effect digest. Cache warmth can also differ
   between record and replay. The three-way check (recorded == old replay) catches state that
   *old* code depends on, but not state that only *new* code reads.
2. **Order and grouping.** The recording is one pytest session order (xdist `loadfile`). The
   labelers' grouping can give f other inputs, for example a cache miss in a fresh worker. The
   maps share this hole.
3. **SyncPool vs spawn.** Pool jobs are recorded in-process. The real run executes them in fresh
   processes.
4. **Fidelity of the inputs.** Anything pickled by reference (classes, module functions, enums)
   is assumed to be identical at replay. Objects with custom `__reduce__` outside sqlglot are
   trusted. Inputs that cannot be pickled make the phase incomplete (safe).
5. **Resource and timing behaviour is invisible.** Memory, recursion depth and wall time are not
   compared. A test is excluded only by the resource-module rule and a 5× CPU guard, and the
   labelers' pytest-timeout is unknown.
6. **Environment.** The stores here were recorded on the laptop venv (3.13.12), not the pinned
   image. Hash seed 0 is assumed in both places. A mismatch would make replays disagree, so all
   tests would be kept (safe, gain lost).
7. **Identity outside the arguments.** `CmpPickler` encodes sharing among the arguments and the
   result, but not identity against globals. For example, f returning a module singleton vs an
   equal copy looks identical.

## Cheaper variants considered

- **Run the candidate tests at run time (report 03).** That variant gave about −4 s on the
  sample with about 7–9 CPU-s. Diffexec gets the same order of gain (2qn5/5y72-type commits)
  with less CPU per commit. Neither one moves the tpcds commits.
- **I13 bytecode equivalence.** It gives 0 commits beyond the AST filter on any set. It flags
  docstring and whitespace commits that the AST filter already clears.

## Files

| File | Job |
|---|---|
| `diffexec/record.py` | pytest plugin: per-phase call recording with caps |
| `diffexec/serial.py` | `InPickler` (rebuildable inputs) and `CmpPickler` (effect digests), code digests |
| `diffexec/build.py` | merge the worker recordings into the store |
| `diffexec/replay.py` | old-vs-new replay with code swapping, CPU limits, and log/warning/stdout capture |
| `selector/diffexec.py` | gates, the fork server, the drop rule and the resource rule |
| `diffexec/measure.py`, `report_store.py`, `changed.py`, `try_patch.py`, `i13_bytecode.py` | measurement tools |
| `diffexec/Dockerfile` | measurement image (`STORE=` prebuilt, or `RECORD=1` for an in-build recording) |
