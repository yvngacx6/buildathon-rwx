# Submissions

| # | Time (PST) | Commit | Version | Sample s | Private s / misses | Board s | Board misses | Note |
|---|---|---|---|---|---|---|---|---|
| S01 | 11-20 21:12 | 1450263 | v0 | 994.1 | – | 1326.5 | 0/40 | Pipeline check. CPU on board 1.1 s (≈2× laptop). 36 of 40 commits ran ALL → 4 no-ops in practice. |
| S02 | 11-20 22:14 | 47907a2 | v1 | 198.8 | 625.8 / 0 (private41) | 232.1 (230.0 sim + 2.1 CPU) | 0/40 | Change-kind table on interim prototype maps. |
| S03 | 11-20 23:16 | eac471c | v1+I08+robust+slowtests | 185.0 | 3329.9 / 0 (264) | 216.0 (213.7 sim + 2.3 CPU) | 0/40 | First with robustness + slow-test statement rule. |
| S04 | 11-21 00:18 | c893605 | + definitive maps, import closure, class-creation state diff | 179.9 | 2882.7 / 0 (264); adv 0/46 | 212.7 (209.5 sim + 3.2 CPU) | 0/40 | CPU up 1 s from state diff. |
| S05 | 11-21 01:20 | 8585937 | + CPU cut (compiled stdlib, marshal shards) | 179.9 | 2882.7 / 0 (264) | 210.8 (209.5 sim + 1.3 CPU) | 0/40 | CPU −1.9 s. |
| S06 | 11-21 02:22 | d2e0730 | + tune, class-attr narrowing, wide-class (2 miss fixes) | 175.0 | 2865.3 / 0 (264); adv 0/46 | 210.8 (209.5 sim + 1.3 CPU) | 0/40 | Practice sim unchanged since S04: practice commits not in these mechanisms. Diffexec is the lever. |
| S07 | 11-21 03:24 | 27d27a7 | + differential execution (pinned store), r4-gap | 149.0 | 2721.3 / 0 (264); adv 0/46; p41 0/41 | 205.6 (202.8 sim + 2.9 CPU) | 0/40 | Practice only −6.8 sim vs sample −26. |
| S08 | 11-21 05:06 | 286a308 | + r7-robust2 (6 miss fixes: shared-state aliasing, class-table writes) | 149.0 | adv2 0/65 (was 6/65) | 217.0 (214.1 sim + 2.9 CPU) | 0/40 | +11.4 s sim vs S07 on practice only; investigating (r11-ab). |
| S09 | 11-21 06:08 | 321c834 | + r10 dialect state, r11 fix, r9 removed bindings, r8 diffexec stand-ins | 114.6 | 2517.8 / 0 (264); adv2 0/65 | 207.8 (205.2 sim + 2.7 CPU) | 0/40 | Practice sim WORSE than S07 (202.8) despite sample −34 s. Hypothesis: diffexec time budgets exhausted on slower scorer CPU. |
| S10 | 11-21 07:09 | 150ec36 | + diffexec budgets for slower scorer, r12 dialect-reach miss fix | 115.1 | 2524.7 / 0 (264) | 211.9 (209.1 sim + 2.7 CPU) | 0/40 | Budget hypothesis refuted (CPU flat). +4 s likely r12 widening. Practice cost probably dominated by a few ALL-fallback commits. |
| S11 | 11-21 08:12 | 92049eb (branch diag-noslow) | DIAGNOSTIC: main minus all tests >= 3 s | 84.1+600 (2 misses) | – | 427.9 sim (1 miss) + 3.7 CPU | 1/40 | ⇒ non-slow cost ≈ 127.9 s; slow tests ≈ 81 s of our ~209; only ONE practice commit needs a slow test. | Not a final candidate. Measures slow-test share of practice cost. |
| S12 | 11-21 09:14 | 252a14c | + r16 import-time narrowing, r17 TPC sub-query verify, r18 max_nodes verify | 115.1 | 2447.9 / 0 (264) | **194.2** (189.1 sim + 5.1 CPU) | 0/40 | NEW BEST. Slow-test lever works (−20 s sim vs S10). | First test of slow-test lever on practice. |
| S13 | 11-21 10:16 | 0bcd015 | + r19 import-fn narrowing, r20 genmem verify | 115.1 | 2427.1 / 0 (264) | 194.0 (189.1 sim + 4.9 CPU) | 0/40 | Same sim as S12: r19/r20 do not touch practice commits. |
| S14 | 11-21 15:02 | 17d41ba (branch diag-slow2) | DIAGNOSTIC: drop test_max_nodes only | – | – | pending | pending | Not a final candidate. (Laptop restart lost 11:00–15:00.) |
