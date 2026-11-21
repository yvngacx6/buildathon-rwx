# Submissions

| # | Time (PST) | Commit | Version | Sample s | Private s / misses | Board s | Board misses | Note |
|---|---|---|---|---|---|---|---|---|
| S01 | 11-20 21:12 | 1450263 | v0 | 994.1 | – | 1326.5 | 0/40 | Pipeline check. CPU on board 1.1 s (≈2× laptop). 36 of 40 commits ran ALL → 4 no-ops in practice. |
| S02 | 11-20 22:14 | 47907a2 | v1 | 198.8 | 625.8 / 0 (private41) | 232.1 (230.0 sim + 2.1 CPU) | 0/40 | Change-kind table on interim prototype maps. |
| S03 | 11-20 23:16 | eac471c | v1+I08+robust+slowtests | 185.0 | 3329.9 / 0 (264) | 216.0 (213.7 sim + 2.3 CPU) | 0/40 | First with robustness + slow-test statement rule. |
| S04 | 11-21 00:18 | c893605 | + definitive maps, import closure, class-creation state diff | 179.9 | 2882.7 / 0 (264); adv 0/46 | 212.7 (209.5 sim + 3.2 CPU) | 0/40 | CPU up 1 s from state diff. |
