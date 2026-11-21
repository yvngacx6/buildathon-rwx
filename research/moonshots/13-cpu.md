# Moonshot 13: container CPU

Branch `moon-13` (on main d48ac42, commit 1bfa5f7 + this report).

**Verdict: modest, safe win. Container CPU −10…−18 % (median −14 %), stdout byte-identical on
all 4 sets x 3 runs. Expected board saving ≈ 0.6–0.7 s per 40 practice commits, ≈ 1.0 s per 60
(scale of today's 4.9 s / 40). Merge-ready; the larger structural cuts (zygote, shared cold-import
child, digest) are worth ≈ another 0.3–0.6 s / 40 and are described at the end, not built.**

## Result (rwx-big, pinned python 3.13 image via drun.sh, `--cpus 2`, `python -S`, CPU = self + children)

3 parallel containers, each interleaving base (main d48ac42) and new; medians of 3, seconds.
The box was oversubscribed (load ~110 on 32 cores), so absolute numbers are ~2x a quiet EC2;
the ratio is what transfers.

| set (commits) | base | new | Δ | state | diffexec | subtest | genmem | static rest | stdout md5 (all 6 runs) |
|---|---|---|---|---|---|---|---|---|---|
| sample (30) | 9.05 | 7.82 | −13.6 % | 2.63→2.44 | 5.82→4.79 | 0.05→0.07 | – | 0.55→0.52 | identical |
| practice_like (120, frozen) | 45.66 | 40.94 | −10.3 % | 20.19→18.38 | 8.84→7.62 | 5.99→6.04 | 1.74→1.53 | 8.90→7.37 | identical |
| P subset (60) | 31.34 | 25.56 | −18.4 % | 10.03→9.14 | 8.60→7.17 | 2.56→2.52 | 0.02 | 10.13→6.72 | identical |
| Q subset (60) | 21.98 | 18.82 | −14.4 % | 7.17→6.57 | 4.83→3.67 | 3.79→3.78 | 0.58→0.50 | 5.61→4.30 | identical |

Board translation: practice CPU 4.9 s / 40 x 0.86 → ≈4.2 s (−0.7 s / 40; −1.0 s / 60 commits).
Where it came from: diffexec −15…−24 % (no per-child recompile / index unpickle, no test-file
parsing in `_is_resource`), static analysis −17…−34 % (deepcopy gone), state diff and genmem
−8…−12 % (no interpreter teardown). subtest unchanged (its compile is shared only when diffexec
ran first on the same commit).

Reproduce: research scripts in the session scratchpad (`prof.py` wraps state.diff /
DiffExec.prune / SubTest.prune / SlowVerify.prune / GenMem.prune and sums os.times incl.
children; `bench_in.sh` / `build_in.sh` run inside `~/shared/drun.sh`); rwx-big `~/work/m13`.

## Where container CPU goes (laptop profile, main, per-mechanism wrappers incl. children)

Laptop (loaded, ~1.7x EC2), python 3.13 `-S`, image-equivalent build (pinned tarball, compileall,
maps / names / state / subtest / slowverify indexes). CPU = self + children (forks, subprocesses).

| set (commits) | total | state diff | diffexec | subtest+slowverify | genmem | static analysis (rest of select) |
|---|---|---|---|---|---|---|
| sample (30) | 7.4 | 1.93 (3) | 4.96 (12 calls) | 0.06 | – | 0.45 |
| practice_like (51 then) | 22.5 | 6.65 (11) | 5.77 (32) | 3.86 (9) | 1.21 (6) | 4.97 |
| P subset (60) | 34.0 | 14.22 (11) | 8.56 (30) | 1.49 (8) | 0.02 | 9.66 |
| Q subset (60) | 14.6 | 5.07 (8) | 3.84 (24) | 1.90 (2) | 0.32 | 3.41 |

(The practice_like set has since grown to 120 commits; the bench below uses a frozen copy.)

Per mechanism:
* **State diff** (≈0.85 s laptop per call): cold `python -P -s -S` child; ≈0.3 import of patched
  sqlglot (182 modules, pyc for all but the patched file), ≈0.3–0.45 digest of 45k objects
  (`Snapshot.fp`, 285k calls), ≈0.1 interpreter + stdlib + 2 MB old snapshot unpickle,
  plus interpreter teardown of the whole imported package (freeing ~50 MB of objects).
* **diffexec**: once per run the parent imports old sqlglot (fork base). Per call: parent-side
  `_is_resource` re-parsed every candidate test file (up to 1.1 s parent on one commit); each
  forked child recompiled the pinned file AND the patched file from source and unpickled the store
  index; the replay itself.
* **subtest / slowverify**: forked from the same warm parent; each child recompiled the pinned
  and patched files again (`_swap`) — the same compile as the diffexec child of the same commit.
* **genmem**: a second cold patched-import child (dialects loaded in test order).
* **Static analysis**: `_default_changes` deep-copied both module trees (`copy.deepcopy` of
  generator.py-sized ASTs: 4.1 M deepcopy calls, ~3 s laptop on one P commit); `ast.parse` and
  `_check_compiles` ≈0.1 s per .py commit (needed).

## Cuts on the branch (all output-preserving by construction)

1. `_default_changes.blank`: blank defaults in place during the same walk, dump, restore in
   reverse (identical walk order: defaults are replaced before `ast.walk` descends, exactly as in
   the copy). No deepcopy.
2. `_is_resource`: build-time table `index/resources.pkl` (Dockerfile step
   `python -m selector.diffexec resources`), from the pinned test sources it already read; a file
   missing from the table is parsed at run time as before.
3. Shared compiled code across forks: `diffexec/replay.py` caches the pinned file's code, the
   patched file's code and the store index in the parent (`warm`, `warm_files`), called before every
   fork (diffexec replay, subtest, slowverify). Children inherit them; per commit the pinned file
   is compiled once per run (not once per child) and the patched file once per commit (not 3x).
4. State-diff and genmem children end with `os._exit(0)` after flushing stdout (no teardown).

Caveat (not a semantic change): the replay / subtest / slowverify / genmem children have CPU
budgets. Moving compile work out of the child lowers child CPU, so a commit that was over budget
by a hair can now finish and drop its tests. That is a cost improvement, and the same
nondeterminism already exists between runs (budgets are CPU-time, the box is loaded).

## Not built (proposals, with estimates)

* **Fork-server ("zygote") for the cold patched imports**: one child started once per run that
  imports stdlib + selector.state/genmem and unpickles the old snapshot, then forks per request
  (state diff, genmem) BEFORE any sqlglot import, so each request still sees a cold import of the
  patched package. Saves ≈0.1 s laptop (≈0.05 EC2) per state diff / genmem call. Risk: the
  pre-import heap differs from a fresh interpreter, so address-ordered entries (sets of classes)
  could vary in a way the 16 build samples never saw → a spurious "changed" entry in a patched
  module (wider selection, never a miss). Fix: take the build samples through the same zygote.
* **Shared state-diff + genmem child**: both do `import unittest; import sqlglot` cold; fork after
  that common prefix (walk_packages for the diff, `get_or_raise` per dialect for genmem). Saves
  one `import sqlglot` (~0.1–0.15 s laptop) on commits running both (6/120 practice_like).
* **Digest speed-ups** (identical digests): skip the `fp(dict(v.__dict__))` temp-dict path for
  functions with empty `__dict__` (the common case), inline the str / memo checks in the dict
  branch. Expected 20–30 % of the 0.3–0.45 s digest per state diff.
* Not worth it: native code (needs gcc in the build image for ≤0.2 s per diff), precompiled
  bytecode for patched modules (one compile per commit is already all that is left).
