# Moonshot 17 — Determinism audit

**Verdict.** On a quiet machine main's output does not change from run to run. Every flip we
saw (2 commits out of ~470 commit-runs) came from a **wall-clock guard that fired while the host
was stalled** (load average 35–80, swapping 5 GB). CPU-time budgets, hash seeds, blob loading
order and digests gave identical decisions in every run. Fixed on `moon-17`: wall clocks no
longer decide anything. Each child is capped by its own CPU (ITIMER_PROF / RLIMIT_CPU), and the
wall timeout only catches a child that blocks. A capped child that overruns still keeps
everything. **Seconds impact: 0 on a quiet machine (same output, same CPU). On a stalled host
each fixed flip is worth +5 s (op07 +5.2 s, ktz3 +4.9 s).** On the final (5 reruns on an idle
machine, median taken) the expected gain is small, but nothing timing-based is left to decide
an outcome except at pathological margins.

66ks does **not** reproduce: 12/12 runs (laptop, under 6 load modes) give `diffexec: dropped 46`,
2.1 s. Moon-06's "dropped 46 / dropped 47" compared **two different programs**: main against
STMT_SECONDS=0, which changes diffexec's candidate set (test_execute_tpcds lands in diffexec's
jobs instead of subtest's). That is not run-to-run noise.

## Method

* Laptop (i7-13700H, 6P+8E cores, SMT): `python -S -m selector` with the run-time index built
  from origin/main. `runsel.py` maps the store's recorded `/opt/sqlglot` paths onto the local
  checkout; without it 11 blobs of 66ks fail with FileNotFoundError, which is a local-only
  artefact. Load modes: idle, 8–40 busy loops, pinned to 2 E-cores (≈1.3× CPU), E-cores plus
  busy loops on the same cores, and pinned to one P-core's two SMT threads with a busy loop on
  each.
* The laptop crashed twice under memory pressure from other agents, and the coordinator then
  moved all heavy work to EC2 (`drun.sh`, one container). See "EC2 reruns" for those numbers.

## Results (main = origin/main 80d3b31 ≡ bd16852 for selector/diffexec code)

| set | runs (modes) | distinct outputs | flips |
|---|---|---|---|
| sample (30) | 6 (idle×2, stress40, ecore, ecore+stress, pinned-SMT) | 2 | ktz3 in the pinned run: the state child's 60 s **wall** timeout fired during a host stall (449 s wall for 9 s of CPU). Result: "import-state diff unavailable", 3→130 tests, +4.9 s |
| private41 (41) | 5 (idle×2, stress8, ecore, ecore+stress) | 2 | op07 under stress: `diffexec: no answer`. The child needs ≈1.0–1.3 s of CPU and has budget 4.42 s, so the wall guard is 9.4 s. It fired during swap stalls: 4→127 tests, +5.2 s. Not reproducible in isolation (6 more runs under 8 and 24 busy loops) |
| practice_like (51), adversarial2 (65) | see EC2 | | |

* **Blob verdicts are bit-stable.** All 20 commits that ran a diffexec replay gave the same
  `blobs ok/total` in every run (e.g. 66ks 2531/2543, ary6 1316/1398, op07 867/873).
* **CPU-time margins (instrumented `moon-17` notes, E-cores ≈ 1.3× slower).** diffexec child
  CPU / per-commit budget ≤ 0.35 (ary6 2.6/7.6 s, 66ks 2.7/15 s). `late` (blobs skipped at the
  CPU deadline) = 0 on every commit. genmem 0.56–0.67 s against a 3 s cap. subtest re-run
  0.52 s. slowverify never reached verification.
* **CPU time is not load-invariant on SMT hosts.** Under the stress-8 run, private41 user CPU
  went from 15.0 to 28.4 s, because SMT siblings share a core and `process_time` almost
  doubles. Every budget is therefore checked against ≥2× headroom (see the table).
* **PYTHONHASHSEED.** On sample with BR_DIFFEXEC=0 (no re-exec), parent seeds 1, 2 and 3 gave
  byte-identical stdout. Every child (diffexec fork/subprocess, state, genmem, subtest build)
  runs with seed 0.

## Inventory: every timing- or order-dependent decision

| # | where | decides | kind | main | risk | fix on moon-17 |
|---|---|---|---|---|---|---|
| 1 | diffexec `_replay_fork` parent | whole replay → None | **wall** budget+5 s | observed flip (op07) | high on a stalled host | wall guard 4·b+30 s; child RLIMIT_CPU 2·b+3 s (SIGXCPU → keep) |
| 2 | state `_run` | import-state diff → None (→ ALL / no narrowing) | **wall** 60 s | observed flip (ktz3) | high on a stalled host | RLIMIT_CPU 60 s in the child, wall 180 s |
| 3 | subtest / slowverify `verify` parent | verification → keep | **wall** 2·b+2 s | not observed | medium | wall 4·b+30 s; the child's ITIMER_PROF already caps CPU |
| 4 | genmem `run_child` | keep | **wall** 2·b+5 s | not observed | medium | 4·b+30 s; ITIMER_PROF in the child |
| 5 | diffexec child: `process_time() > deadline` per blob | blob → not ok | CPU | late=0 everywhere, ≤0.35 of budget | low (flips only near the budget) | kept; `late` counter in the note. Proposal below |
| 6 | diffexec child: `cpu_new <= 5·cpu_old + 0.05`, per-call cap 10·cpu_old+0.2 | blob → not ok | CPU ratio | never fired | low (needs a ≈5× slowdown) | kept |
| 7 | diffexec `BATCH_BUDGET_S - spent` | budget for later commits | CPU, cumulative | spent ≤ 4 s/set ≪ 100 | low | kept |
| 8 | subtest per-query ITIMER 3·cpu_q+0.8, total min(6, 2.5·est+1) | verification fails → keep | CPU against **build-time** CPU | 0.52 s used | low if build = run hardware | kept (see rebuild note) |
| 9 | subtest plan `spend ≤ min(12, 0.6·gain)` with build-time `cpu_exec` | which TPC tests are verified | **build-time timing** | fixed per image | different across *rebuilds* | proposal: commit the per-query estimates |
| 10 | genmem ITIMER 3 s, est constants | keep | CPU | 0.6/3 s | low | kept |
| 11 | parent PYTHONHASHSEED | set iteration order | seed | seed-independent (3 seeds) | none | — |
| 12 | effect digests (`FastCmpPickler`) of sets / frozensets of id-hashed objects | blob → not ok | ASLR address order | identical blob counts in every run | low (only sets with >~256 id-hashed members reach page bits) | proposal: sort set members by digest |
| 13 | `gc.get_objects()` order in replay/subtest `_swap` | which live functions are swapped | allocation order | all swapped, so order does not matter | none | — |
| 14 | id()-keyed memos (names.py, testimport.py, state.py) | caches | object identity | trees and modules are kept alive, so no id reuse | none | — |
| 15 | tracemalloc growth (genmem) | keep | allocator | limit 256 B against a 32 B baseline | low | — |

## Proposals not implemented (each keeps the "overrun → keep" rule)

* **Deterministic diffexec work budget (#5).** Replace the CPU deadline with a work counter
  that is a pure function of the store: Σ bytes of replayed blobs + k·blobs. On the commits we
  measured, replay CPU ≈ 0.6–1.1 s per MB on P-cores (66ks 1.78 MB→1.5 s, ary6 2.57 MB→2.0 s,
  op07 1.16 MB→1.0 s). Gate before replaying: if the estimate exceeds the budget, keep
  everything, deterministically. Keep RLIMIT_CPU as the backstop. Not worth doing now: the CPU
  deadline never bound (late = 0), so this only moves determinism at margins that do not occur.
* **Rebuild-invariant subtest plan (#9).** If the final rebuilds the image, `cpu_exec` per
  query is re-measured, so which TPC tests get verified can change. Commit a rounded
  per-query cost table (≈120 floats, < 4 kB) and let the build use it in place of its own timings.
* **Digest canonical set order (#12).** In `FastCmpPickler.persistent_id`, write set and
  frozenset members sorted by their own digest. The cost is a recording rebuild, so this is
  only worth doing together with moon-12's re-record.
* **Global wall deadline (moon-09 R25)** is the one wall clock that has to stay. It should trip
  only in a real overrun (> 4 min), never at normal load.

## EC2 reruns (scorer-like image, 2-CPU container, host shared with ~9 other containers)

PENDING
