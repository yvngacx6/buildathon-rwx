# Moonshot 26: build-time headroom + bundle of the parked wins (branch `moon-26`)

## Verdict (numbers first)

- **Clean build on rwx (final_check C4 shape: --no-cache, 4 CPUs, 8 GB): cand-h 169.3 s → moon-26 113.5 s**,
  with moon-22 and moon-25 included (≈ 227 s on the 2x scorer, cap 600). Without the build work, cand-h +
  moon-22 + moon-25 would be ≈ 169 + 67 + 12 ≈ 250 s on rwx (≈ 500 s scorer).
- **Ported:** moon-22 (per-optimizer-rule TPC checkpoints), moon-25 (executor generate() checkpoint).
  **Dropped:** moon-15 (fixture-case slicing): its build needs duckdb + pandas (PyPI download in the image).
- **0 misses on every labeled set** (sample, practice_like, private, private41, adversarial, adversarial2) at
  each step; labeled sims identical to cand-h (private +0.006 s on one image = a diffexec timing keep on hgxu,
  see below).
- Repo (git archive): 17.32 → 17.45 MB (≤ 18 MB).
- A/B on /shared/PQ: RESULTS_PLACEHOLDER

## 1. Build profile and what changed

Per-step profile of cand-h's big RUN (`research/moonshots/26/prof.sh`, 4-CPU container on rwx, steps serial as
in the Dockerfile):

| step | wall | notes |
|---|---|---|
| FROM + fetch_sqlglot + COPY | 4.9 s | download is fast; nothing to win |
| compileall -j4 (stdlib, sqlglot, selector) | 1.7 s | |
| selector.maps build | 0.5 s | |
| selector.nameindex build (+ state snapshot) | 34.5 s | single-threaded |
| selector.subtest build | 74.3 s | single-threaded, serial over 121 queries |
| selector.slowverify build | 51.9 s | single-threaded |
| selector.diffexec resources | 0.7 s | |
| chmod | 1.3 s | |
| **total** | **165 s** | all four store builders run one after another on 1 of 4 cores |

None of the four store builders reads another's output (each writes its own file under /app/index; checked:
the build entry points only read /opt/sqlglot and the repo; `slowverify`'s stored `cpu` is never read at run
time). So:

1. **Dockerfile: the four builders run side by side** in the one RUN (`& p=$!` per job, then
   `wait $p1 && wait $p2 && ...` so any failure fails the build; a bare `wait` would hide it).
   compileall and maps stay serial and first (the builders import from the .pyc).
2. **subtest build: fork pool over queries** (`BR_SUBTEST_JOBS`, 3 in the Dockerfile, default 1). Every
   query already ran in fresh forks of the builder process, which runs nothing between queries, so a query's
   result does not depend on which worker or in which order it runs. `imap` keeps fixture order; ck members
   are keyed by fixture index (was `len(queries)`, same value).
3. **moon-22's two seed interpreters** (`PYTHONHASHSEED` 1 and 2 boundary digests) run side by side
   (≈ 11 → 6 s).

With moon-22 the subtest build is 127 s serial (rwx, alone) → **58 s with 3 workers**.

**Store equivalence (pooled vs serial, `research/moonshots/26/cmpstore.py`):** all 831 zip members
byte-equal except meta.pkl; meta.pkl equal outside the timing fields (`cpu_exec`, `cpu_opt`, `cpu_stage`,
`cpu_from`, `calib`, `cpu_steps`). Normalized timings, pooled/serial: median 0.93–0.97, p10 ≈ 0.6, p90 ≈ 1.1–1.6
(raw `calib` 1.7x: 4 vCPU = 2 physical cores, so concurrent processes slow each other; `_norm` corrects most
of it). Timings only feed the gate / caps (an underestimate → the CPU itimer fires → keep), never soundness.

Build times (rwx, `research/moonshots/26/btime.sh` = final_check C4 command):

| ref | what | build |
|---|---|---|
| cand-h b048c6a | baseline | 169.3 s |
| 8754be6 | parallel RUN + moon-22 | 150.5 s |
| d013d8d | + subtest pool (3) + parallel seeds | 111.4 s |
| ffddeb3 | + moon-25 | 113.5 s |
| FINAL_PLACEHOLDER | | |

The remaining build is ≈ throughput-bound: ≈ 240 CPU-s of builders on 4 vCPU (2 physical cores).
Further wins would need less CPU (e.g. nameindex 35 s), not more parallelism.

## 2. Ports

### moon-22 (per-optimizer-rule checkpoints) — kept
- Merge of `origin/moon-22` (one textual conflict: the drop/keep message; merged both).
- Unit tests 223/223 (incl. r29's 15 stage tests).
- MOON22_PLACEHOLDER

### moon-25 (executor generate() checkpoint) — kept
- Semantic merge with moon-22: the run-time query tuple is now
  `(suite, i, opt, mode, parse_hit, optb_hit, duck_hit, k, gen_hit)`; `_cpu` adds gen replay CPU on top of the
  checkpoint (`cpu_from[k]`) or stage CPU; the child's itimer = 3 × (checkpoint-or-stage CPU + gen CPU) + 0.8.
  gen replay runs after the optb check (rule checkpoint or moon-05 stage), as in moon-25.
  Its build-time `gen_timed` uses cand-h's calibrate-after convention (as `stage_timed`).
- MOON25_PLACEHOLDER

### moon-15 (fixture-case slicing) — dropped
- Its build imports `tests/test_optimizer.py`, which imports `duckdb` and `pandas` at module top and uses
  duckdb in `setUpClass` and non-case asserts. The scorer image is bare python:3.13-slim: landing it means a
  PyPI download of duckdb + pandas + numpy in the build (new network dependency at the final rebuild,
  ≈ +20–40 s) plus ≈ 6.6 CPU-min of build (5.6 CPU-min case tracing + 1 min warm pass) ≈ +150 s on
  rwx → ≈ 265–300 s, over the 260 s budget, for ≈ −1…−2 s/40. Stubbing duckdb would change setUpClass
  coverage (and break non-case asserts), so it is not a safe shortcut.

## Noise seen in the A/Bs (not caused by these changes)
- **Q_e9bs** (errors.py `highlight_sql`): slowverify verification takes ≈ 2.0 s CPU inside the 1000-commit
  batch against a 2.0 s itimer (BUDGET 4 s / 2), but 0.19 s alone (re-run 2x on both images: drop). cand-h
  passed at 2.02 s, moon-26 failed at 2.05 s → +28.9 s on that one commit. The batch-vs-alone gap (heap walk
  in `_swap` over a heap that grows over the batch) is a pre-existing fragility; on the 60-commit final the heap
  is smaller.
- **private hgxu**: diffexec child 0.67–0.69 s vs a 0.78 s budget; under imgcheck load one image kept one more
  test (+0.006 s). Alone, both images give the identical selection.
- rwx-big was shared with other agents' imgchecks during these runs (load 3–10).

## Ground truth
- **Q_1h4b** (the one new P/Q drop vs cand-h: `unnest` condition `and` → `or`): applied with the selector's
  patcher, the three TPC executor tests **PASS** (79 subtests). cand-h kept it only because its moon-05 path's
  fallback CPU hit FALLBACK_CAP 1.5 s (store timing); moon-26 drops it with rule checkpoints (10 queries).

## Reproduce
- `research/moonshots/26/prof.sh REF TAG` (rwx): per-step build profile.
- `research/moonshots/26/btime.sh REF...` (rwx): final_check C4 clean build time.
- `research/moonshots/26/sub.sh REF JOBS` (rwx): the subtest build alone, timestamped; `cmpstore.py A B`.
- `research/moonshots/26/pqrun.sh TAG [LABEL -e VAR=..]` (rwx-big): image on /shared/PQ/{P,Q} with the scorer's
  flags, sim + container child CPU. Labeled sets: `~/shared/imgcheck.sh` (https clone variant `~/ic26.sh`).
