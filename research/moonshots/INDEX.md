# Moonshot index

Recurring loop: 10 moonshot agents run in parallel with the core improvement loop. When one finishes,
its report is linked here with a verdict, promising ideas become core rounds (experiments/LOG.md), and a
new moonshot angle is started in its slot.

| # | Angle | Branch | Status | Verdict / next step |
|---|---|---|---|---|
| 01 | Reverse-engineer the leaders | moon-01 | done | Practice oracle ≈ 95 s; leaders 10–60 s above it, us ~100 above. Copy: statement-level selection for ALL tests (−10…−25 s), TPC fixture-entry rule (−8 s), CPU 4.9→2 s (−3 s) → R23. |
| 02 | Warm fork-server: near-free test execution | moon-02 | done | ✗ Works (0 disagreements on 98 labeled) but per-test CPU ≈1.3× recorded secs (≈2.6× on scorer); net ≈ 0 s/40. Not built. |
| 03 | General semantic equivalence (value profiles) | moon-03 | done | ✗ ~1–2 s/40; most edits aren't comparisons/boolops. Warning: pickle-only paths (serde) invisible to non-pickling recorders → relayed to R23. |
| 04 | Build-time recording beyond the 20 MB repo limit | moon-04 | done | ✗ Store is saturated: recording all 4,134 'budget-dead' functions saves 0.3% of the bucket; own release/PyPI assets violate rules. |
| 05 | Divergence-point re-execution of slow tests | moon-05 | done | ✓ TPC stage checkpoints: practice_like −91 s sim (≈−19 s/40 net), 0 misses, 38/38 ground-truth drops pass → R29 (r29-tpcstage) with shared-state guard. |
| 06 | Generic sub-test decomposition | moon-06 | done | Full mechanism not worth it; zero-CPU special case STMT_SECONDS=0 (statement rule for all tests) 0 misses/440, −9…−14 s/40 → folded into R23. Flagged diffexec nondeterminism on 66ks → moon-17. |
| 07 | Calibrated risk model (P(first place)) | moon-07 | done | ✗ AUC 0.56 (no better than base rate); keep 0-miss policy. Spin-offs → R22 (import-run slow tests w/ 0 touched lines never fail), moon-12 (record tpcds callees). |
| 08 | Practice-board experiment design | moon-08 | done | Slot plan adopted in part (ablations; always pick final explicitly after diagnostics). Score-encoded 'telemetry word' REJECTED: it circumvents 'you never see what your program printed' — not used. |
| 09 | Final-round strategy | moon-09 | done | Deterministic under ±2x CPU. Must-fix: global wall deadline, Dockerfile download retry, Selector-construction guard, config/no-reader files → R25. Gap is practice-like code commits → moon-16. |
| 10 | Wildcard ideas | moon-10 | done | 17 angles; V1 = run leftover cheap tests via plain unittest (agrees with labels 418/418), gated ≤0.6 s → −3…−9 s/40 → R24. Fixture-case slicing → moon-15. |
| 11 | Assertion-level slicing | moon-11 | done | ✗ Sound (0 false drops/2811) but ~0 s/40 net. Found possible import_time.py one-liner hole → moon-18. |
| 12 | Compact diffexec encoding | moon-12 | stopped | Stopped: moon-04 showed the store is saturated (more recording ≈ 0 s). |
| 13 | Container CPU | moon-13 | done | ✓ Merged (28767ce): −14% CPU, byte-identical output (≈ −0.7 s/40). |
| 14 | Fixture-edit precision | moon-14 | done | ✓ Loader-output diff per reader: private −84.5 s, adversarial −19.6 s, P −10.5/40, 0 misses on 73 fixture commits → porting as R27 (r27-fixtures). |
| 15 | Per-fixture-case slicing | moon-15 | done | ~ Sound (0 misses, 75/75 drops really pass) but ≈ −2 s/40 net and +3 min build. Parked; maybe bundle with V1 later. |
| 16 | Code-commit over-selection | moon-16 | done | ✓ R-DEF (literal-default edits → callers that use the default): −13…−17 s/40, 0 misses → R28. Next: tokenizer-settings read sites (moon-20), key-level state diff (moon-21). |
| 17 | Determinism audit | moon-17 | done | ✓ Only wall-clock guards flip (tggb +18 s, op07, ktz3 under load). CPU caps decide + timing re-check → R30 (r30-determinism). 66ks flip was a mis-comparison. |
| 18 | Red-team of run-time pruners | moon-18 | done | ✓ Found 2 real holes (one-line lambdas run at import → 3/5 probes missed on main; diffexec exception attrs) and fixed both; 0 cost on labeled sets. Merged into cand-a. |
| 19 | 'Conservative def-time' + 'state diff' buckets on practice_like (~87 s): sound narrowing | moon-19 | running | |
| 20 | Tokenizer settings → TokenizerCore(...) read sites ∩ dialect users | moon-20 | running | |
| 21 | Key-level state diff for class-creation hooks & import-run builders | moon-21 | running | |
| 22 | Per-optimizer-rule TPC checkpoints (3–5x cheaper re-runs) | moon-22 | running | |
| 23 | Final-day plan + final_check.sh | moon-23 | done | ✓ eval/final_check.sh (13 checks, EC2-only); main PASS. Build 171–188 s on rwx (≈2x on scorer → watch 600 s cap). Freeze 03:00, pick final ≈04:00. |
| 24 | Dispatch precision | moon-24 | done | ✗ Dispatch pairs are coarser than statement coverage (R23 already captures it); instruction-level ≤2 s/40. Remaining waste = unrecorded functions, but moon-04 showed recording them saves ~0. |
| 25 | Executor-touched TPC commits: PythonExecutor.generate checkpoint (record generate() in/out per query, replay patched generate) | moon-25 | running | |
