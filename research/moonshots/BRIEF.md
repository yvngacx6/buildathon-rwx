# Moonshot brief (shared context for moonshot research agents)

Repo: ~/Desktop/hackathons/buildathon-rwx (main = origin/main). Read README.md, SCORING.md, ARCHITECTURE.md,
experiments/SUBMISSIONS.md, experiments/LOG.md, research/ideas/I12-differential-execution.md.

## Task recap
Per commit (one-token-ish mutant patch of sqlglot @ eb11aec): cost = busiest worker recorded secs + 2 s/worker (0 if nothing);
+300 s per commit whose selection misses a failing test; + all container CPU. Run: 2 cores, 4 GB, 64 MB /tmp, read-only,
no network, 3 min per 40 commits (4.5 min / ~60 in final). Build: 10 min, 4 cores, network (PyPI etc.), repo ≤ 20 MB
(the IMAGE has no size limit). Banned: patch→failures or edit-kind→failures tables. Allowed: maps from the pinned commit.
Final: unseen commits incl. new kinds; top 5 re-run 5x on an idle machine; ties within 1 s → earlier submission wins.

## Where we are (2026-11-21 ~11:00 PST, cutoff Sun 08:00 PST)
- Board best 194.0 s (40 practice commits, 0 misses). Leaders: 126.1, 130.3, 134.3, 135.3, 139.9 s. We're ~#120/277.
- Diagnostic: dropping every test ≥3 s gives 427.9 s with 1 miss ⇒ our non-slow cost ≈ 128 s (≈ leaders' TOTAL);
  slow tests (test_execute_tpcds 21.5 s, test_max_nodes 28.8 s, test_generator_memory 6.7 s, test_execute_tpch 3.9 s,
  test_optimized_tpch 3.5 s) cost us ~61 s on practice and only ONE practice commit needs any of them.
- Sample oracle 94.4 s / 30 commits; our sample 115 s.
- Mechanisms: per-test line/class/read/import maps; AST no-op filter; name/attribute narrowing; import-state diff
  (subprocess imports patched sqlglot, diffs every global/class attr); differential execution (replay recorded
  function inputs old vs new; 13 MB store, size-capped); TPC sub-query verification; max_nodes frontier verification;
  generator_memory verification; collection-error → ALL policy. 0 misses on 446 labeled patches incl. 111 adversarial.
- Scorer CPU ≈ 2x slower than our EC2 (c7i) box.

## Your job
Think BIG: a moonshot that could plausibly cut practice cost from ~190 s toward ~120 s or below (or make us
unbeatable on the final) WITHOUT misses and within the rules. Be concrete and quantitative: mechanism, why it's
sound, expected seconds saved per 40 practice-style commits, CPU cost (scorer 2x slower), feasibility in the ~18 h
left, rule compliance, and the smallest experiment that would validate it. Prototype/measure on the laptop if it
takes < 60 min (sqlglot clone: ~/rwx-research-robust/sqlglot, venv: ~/rwx-research-robust/venv; unlabeled
practice-style patch sets: /tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/{P,Q};
labeled sets: eval/data/*). EC2 (`ssh rwx`) is SHARED and busy — at most short read-only checks there.
Do NOT modify main or other agents' branches. Write your report to research/moonshots/<NN>-<slug>.md (≤150 lines,
verdict + numbers first) on your own branch `moon-<NN>` and push; final message = 10-line summary with your
honest probability that it works and expected gain.
