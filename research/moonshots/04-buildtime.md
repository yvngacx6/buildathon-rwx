# Moonshot 04: escape the 20 MB repo limit (build-time artefacts)

Branch `moon-04`. Measured on rwx-big (pinned-Python image via `drun.sh`), recordings from the
laptop. Scripts and data: rwx-big `~/work/m04/`.

## Verdict: NO-GO (probability it helps the final: under 5 %, expected gain about 0 s)

| Idea | Verdict | Why |
|---|---|---|
| (b) release asset on our own GitHub repo | **not allowed** | "Nothing from an address your team controls" (rules below) |
| (c) our own PyPI data package | **not allowed** | same rule; we could yank it or re-upload after the cutoff |
| (a) record the budget-dead diffexec functions in the Docker build | **infeasible and worthless** | about 3,800 CPU-s of build work against a 2,400 CPU-s build; saves 9.1 s per 1,600 A/B patches while adding at least 61 s of replay CPU |
| (d) better compression / regenerate from seeds | **moot** | bytes are not what limits the store (r14, below) |

The A/B bucket "diffexec: function not recorded (budget)" is 25 % of A/B cost (3,204 s of
12,766 s, 263 patches, 158 functions). It is **not recoverable by recording more**:

1. **The label is misleading.** `build.restrict()` marks *every* function without a plan as
   "budget". That includes functions with no droppable test at all: an incomplete IMPORT/OTHER
   phase, inputs over 16 kB even after elision, the 2 MB per-file cap, unpicklable inputs.
2. **Most of the bucket's commits really break tests.** The A/B patches are mutants. Once
   recorded, diffexec replays them, sees different effects, and correctly keeps the tests.

## Measurements

**Ceiling (measurement hack, not sound).** `prune()` drops every candidate whenever the
precheck says "not recorded (budget)". On the 264 bucket patches the cost goes from
3,341.7 s to 84.4 s. This would also drop failing tests, so it is only an upper bound, and it
turned out to be a very loose one.

**r14 (an earlier run, reused here).** A 13.1 MB store against a 77.8 MB store (same caps,
store_budget 27e6, 3,071 functions alive instead of 2,039) saves 35.7 s per 1,600 patches. On
the bucket's commits it saves 17.6 s of 3,363 s.

**This experiment: infinite budget.**
- I recorded only the 4,134 budget-dead functions (`DIFFEXEC_ONLY`, default caps), built them
  with `DIFFEXEC_STORE_BUDGET=1e12`, and merged that into the committed store
  (`~/work/m04/merge.py`).
- 2,616 more functions became alive. The extra store is 94 MB, so it can only live in the image.
- A/B on the 264 bucket patches, same checkout, only `BR_DIFFEXEC_STORE` differs:

| | base (committed store) | ext (+2,616 functions) |
|---|---|---|
| simulated s, 264 bucket patches | 3,363.2 | **3,354.1 (−9.1 s, −0.3 %)** |
| still "not recorded (budget)": planless even with no budget | 264 commits | about 130 commits |
| replayed, "dropped 0" (effects differ: real behaviour change) | – | 69 commits, 476 s |
| "no answer" (replay CPU budget / timeout: big inputs) | – | 38 commits, 408 s |
| replay CPU logged (child + parent, EC2) | 0 | **≥ 60.8 s** (+ up to 3 s for each of the 38 "no answer" commits) |

- Net on these 1,600 sample-style patches: −9.1 s simulated, against +61…175 s CPU on EC2.
  At the scorer's ~2x slower CPU that is **+120…350 s**. Net cost **goes up**.
- Scaled to practice: the bucket is ~24 % of A/B cost. Recovered ≈ 0.3 % of the bucket, so
  about 0.3 % × 24 % × 189 s ≈ **0.1 s simulated saved per 40 commits**, before the extra CPU.
- The single best gain was `Parser._kv_to_prop_eq`: dropped 44 tests, −6.3 s, for 1.75 s of
  child CPU.

**Build cost (pinned image, rwx-big, 4 CPUs, box at load ~200, so wall times are inflated).**

| Step | CPU | Wall |
|---|---|---|
| census, `-j 4` | – | 119 s |
| record 4,134 functions, `-j 4` | **2,454 CPU-s** (user 40m54s) | 34 min |
| merge + LZMA store build (single process) | **1,368 CPU-s** | 26 min |
| laptop, same recording, `-j 16` | 1,866 CPU-s of pickling alone | 525 s |

- On the laptop, test_executor.py and test_optimizer.py are each about 520 s in ONE process.
  That critical path alone nearly fills the 10-minute build, even before the scorer's slower
  CPU.
- The build budget is 4 cores × 10 min = 2,400 CPU-s, and it must also cover `pip install`
  (duckdb, pandas, numpy, pytest) and the existing map steps. If the build exceeds 10 min,
  the submission is lost. A watchdog that cuts the recording short keeps it safe, but per
  the A/B there is nothing worth recording.

## Rules: (b) and (c)

README: the build "may download only from public package registries: PyPI, npm, Docker Hub,
GitHub releases at a fixed tag, and sqlglot's own repository at the pinned commit. **Nothing
from an address your team controls.** The final rebuilds every image after the cutoff, so
anything else could change a submission after the deadline."

- A release on `yvngacx6/buildathon-rwx` is an address our team controls. Its assets can be
  deleted and re-uploaded under the same tag, which is exactly the mutability the rule targets.
  The fixed-tag allowance is for third-party tools.
- A PyPI project we own is ours too: we can yank it or publish a new file name. The rule names
  its intent ("could change a submission"), not a host list.
- The downside is disqualification, or a judge reading it as smuggling data past the 20 MB
  limit. The upside, per the A/B above, is about 0. **Do not use either.**
- (a) and (d) themselves are allowed. README: the build "may clone sqlglot at the pinned
  commit, install it and run its tests to make coverage or import maps". A store recorded in
  the build from the pinned commit is the same kind of artefact.

## What this tells the team

- **Store coverage is saturated.** The 13.1 MB committed store already holds essentially every
  function where replay pays off. Do not spend more effort on store size, compression, or
  in-build recording.
- The 25 % "budget" bucket is mostly real behaviour change, plus functions whose inputs are
  too big to record or to replay within the CPU budget (optimizer and executor entry points
  such as `optimize`, `qualify`, `isolate_table_selects`, whose callers include
  test_execute_tpcds).
- Shrinking that bucket needs finer granularity than whole functions: sub-test or per-query
  verification (as `selector/subtest.py` already does for TPC), or statement-level replay.
  Wider recording does not help.
- Caveats:
  - The A/B patches are generated mutants, not practice commits. Practice-style "no-op"
    edits would replay equal more often, but r14 and this run agree to within noise.
  - The subtest index on rwx-big showed `exec=False` for the TPC queries. Base and ext used
    the same index, so the delta is fair, but absolute costs differ slightly from the laptop
    runs.

## Reproduce

rwx-big `~/work/m04/`:
- `rec.sh`: 4-core recording with `DIFFEXEC_ONLY=only_all.txt`.
- `job.sh`: store build with an infinite budget, then the merge, the index build, and the A/B.
- Logs: `recB.log`, `buildA.log`, `ab_base.log`, `ab_ext.log`.
- Results: `BUD/runs/{base,ext}.json`.
- The ceiling hack is in `ceil/selector/diffexec.py` (`M04_CEILING=1`), scratch only.
