# Research findings: ranked methods

Synthesis of the five agent reports in `agent-reports/`. Written in ASD-STE100 style.

## 1. Cross-validation

Three agents built three separate prototypes. Each prototype got 0 misses on the 30 sample commits.

| Prototype | Report | Sample score | Misses |
|---|---|---|---|
| Line map + statement expansion + dialect-family rule | 02 | 166.7 s | 0 |
| Line map + read map + use map, measured in Docker on EC2 | 03 | 194.0 s | 0 |
| Function map + class-use map + read map + fallback table | 05 | 198.6 s | 0 |
| Oracle (perfect knowledge) | 04 | 94.4 s | 0 |
| Starter (all tests, 3 workers) | kit | 1210.5 s | 0 |

All three agents found the same four traps independently:

1. **Process pool.** `test_optimizer` and `test_executor` do their work in a subprocess pool. Coverage does not see it. To build the map, run the pool inline and send arguments and results through pickle.
2. **Short-circuit lines.** In a multi-line condition, a changed line can stay unexecuted. Select by the full statement or the full function, not by the single line.
3. **Import-time code.** 31% of covered lines run only at import (class bodies, module constants). Coverage cannot credit them to a test. Use a map of the classes each test uses.
4. **Fixture reads.** Coverage does not see `open()`. Record the files that each test reads.

**Caution.** Each agent tuned its prototype on the sample. The only held-out evidence is 11 private patches, and only 2 of them break a test. The scores above are not a forecast for the final.

## 2. Ranked methods

| Rank | Method | Status |
|---|---|---|
| 1 | Dynamic map at function level, plus class-use map (with inheritance), plus fixture read map, plus AST no-op filter, plus fallback table | **Base design. Build this.** |
| 2 | Line map with statement expansion and dialect-family rule | Cheaper variant. More tuned to the sample. Use its ideas, not its rules. |
| 3 | Expected-cost rule for the three slow tests (tpcds 21.5 s, max_nodes 28.8 s, generator_memory 6.7 s) | Add on top of rank 1. |
| 4 | Execute candidate tests at run time, only on cheap commits | Optional. Off by default. Saves about 4 s. Adds risk. |
| 5 | All tests, optimal packing (about 37 s per commit) | Fallback only. Use it when the risk of a miss is more than about 10%. |

### Methods we rejected

| Method | Reason |
|---|---|
| Whole-file selection | 256.5 s even with a perfect file choice (report 04). |
| Static import graph (STARTS style) | Almost every test imports `sqlglot`. Dialects load by name string. Use it only for edits to test files. |
| ML on past failures (Facebook, Launchable) | Banned: a table from patch to failures. Also, 30 samples is too few. |
| Mutation kill matrix in the image | Banned: works as an answer key for a mutant-generated hidden set (reports 01, 05). |
| Run every candidate at run time | Exceeds the 3-minute limit. CPU cost is larger than the savings (report 03). |

## 3. Facts that the plan must use

- The practice set has 40 commits. The final has about 60 (report 04).
- The sample patches are synthetic one-token mutants, plus template comments, plus fixture corruptions (report 05).
- A miss costs 300 s. Leaders have 0 misses. One miss costs more than their full score.
- The run image has no `git` and no `patch`. Apply diffs in pure Python (report 05).
- The labelers' image skips one SQLite test. Build the map in the pinned image, not on the laptop (report 02).
- `test_optimizer` and `test_executor` start a pool that is sized to the host CPU count. This matters only if we run tests at run time (report 03).
- Map sizes: function map is about 181 KB gzipped, line map is about 0.7 MB gzipped. Both fit in the 20 MB repo.

## 4. Compliance line

- **Allowed:** maps from the pinned commit (coverage, class use, fixture reads, imports). Private mutants used only to measure our recall.
- **Not allowed:** any data in the image that comes from labeled mutants. Any rule of the form "edit kind X is harmless" or "edit kind X breaks tests Y".
