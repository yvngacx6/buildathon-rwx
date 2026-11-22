# Moonshot 19 — conservative def-time + state diff on practice_like

Branch `moon-19` (base a8fbd6b). All runs on rwx-big in the pinned image (`drun.sh`), selector
without subtest/slowverify indexes (same for base and new, so deltas are comparable).

## Verdict (numbers first)
- **Rule A ("never called"), ready to port**: −23.4 s / 120 practice_like (−7.8 s/40), −2.3 s private,
  0 on sample / private41 / adversarial / adversarial2; **0 misses on all 6 labeled sets (566 commits)**;
  unlabeled P −54 s / 600 (−3.6 s/40), Q −82 s / 1000 (−3.3 s/40). 24 labeled probes: 0 misses.
  Expected **−3.5 … −8 s per 40 practice commits** (best guess −4.5), ~0 CPU (no subprocess).
- **Rule B (attrs map: restrict state-diff class-attribute readers to tests that actually looked the
  attribute up)**: see section B — (pending re-score).
- 09if (30.5 s) and most of 7zco (20.9 s) are genuine: the edit changes `_COMMENTS` / `_KEYWORD_TRIE` /
  `_FORMAT_STRINGS` of nearly every tokenizer, and the slow tests do tokenize with those dialects. Only
  value-level verification (record each slow test's tokenizer inputs at build, re-tokenize under the
  patched state, compare token streams) could drop them; sketched at the end, not built.

## Where the 107.7 s (def-time 57.9 + state diff 28.9 + import-time map 20.9) goes

| commit | gap s | edit | why the extra tests |
|---|---|---|---|
| 09if | 30.5 | `_TokenizerBase.__init_subclass__`: `c[0]: c[1]` → `c[1]: c[1]` | class-creation hook; changed `_COMMENTS`/`_KEYWORD_TRIE` in all tokenizers → ≈ALL |
| c7om | 22.4 | `planner.py` `Join._to_s` body | body never ran under any test nor at import → "unrun def → definition-time" → planner/executor users incl. test_execute_tpcds |
| 7zco | 20.9 | `tokens._convert_quotes` (runs at import) | 122 changed keys → 1179 readers; tpcds via `DuckDB.Tokenizer._FORMAT_STRINGS/_KEYWORD_TRIE` |
| mn99, ptjt | 2.7, 2.3 | lambda body in a `FUNCTIONS` table (exasol, tableau) | lambda never ran → `_by_names` Fallback → conservative set |
| wumx 0aqd swmi k5pu o89y 6o3v 2qcc h917 358q qur8 vlcc | 28.9 | dialect/parser flag flips | readers = tests that ran the reading line ∩ "users" of the owner class. Users come from the classes map, where `dialect_reach` credits every test that *names* a dialect (e.g. `write={"sqlite": …}`) to its tokenizer/parser/generator classes; and many reads are `self.dialect.FLAG` / `self.FLAG` in base-class code that every test runs (e.g. `Tokenizer._init_core` reads `self.dialect.NUMBERS_CAN_BE_UNDERSCORE_SEPARATED`): test_snowflake holds SQLite (writes it) and runs `_init_core` (for Snowflake) → selected, though it never tokenizes SQLite |

## Rule A — a body that is never called (selector/select.py `_never_called`)
A function region whose edit is body-only (not def line / decorators; for a lambda: its call lines
exclude the creation line, and every lambda's parameters/defaults are unchanged), where **no test ran a
body line (lines map) and no module import runs one (import-time map)**, plain def (decorators only
staticmethod / classmethod / property; not `__del__`). The new code object is bound at import but never
executed, so a test can observe the edit only by reading the code object itself. sqlglot has exactly one
such reader: `tests/test_typing.py::_same_code` compares annotator `__code__`. So the region selects the
tests that ran a line naming a code-introspection API (`__code__`, `__closure__`, `co_code`, `co_consts`,
`getsource*`, `getclosurevars`, `findsource`, `dis`) ∩ loaders of the module (imports map); a
def-time mention of one in sqlglot/ → no rule. Before: such regions fell to `_definition_time`
(conservative class users ∪ file runners). The same test replaces the `_by_names` Fallback
"lambda at line N never ran under a test".

Soundness: the same lines-map completeness every function-region selection already relies on. Checked:
the tracer (`sys.monitoring`, per-interpreter, so all threads) runs process-pool jobs inline with pickle
round-trips; the only subprocess in tests (`test_lazy_load`) just runs `import sqlglot` (import-time map
root 0). 218 bodies in sqlglot/ qualify (no test-time, no import-time lines, plain decorators).

Validation:
- Labeled sets (gap.py, base vs new): practice_like 1041.0 → 1017.6 s (c7om 25.5→3.0, mn99 2.7→2.0,
  ptjt 2.3→2.0), private 2484.5 → 2482.2 (jyy4, rzlz), others unchanged; misses 0 everywhere.
- 24 probes (`gen_probes.py`, one-token mutants inside accepted bodies, biased to `__repr__`,
  planner/executor, typing annotators, table lambdas), labeled on rwx-big with br-label (full suite,
  SKIP_INTEGRATION=0, --continue-on-collection-errors): **21/24 no failing test**; 3 reported
  `test_max_nodes` "worker crashed" (OOM under the 3 GB limit while rwx-big was at load 200+ / 44 GB
  used). Re-labeled: m1911, m1913 pass all 1620; m1908 (planner `Step.to_s`) crashed again under xdist,
  but test_max_nodes alone under the m1908 patch passes (peak 1.66 GB) and noop passes: an OOM flake,
  not an outcome (a planner `to_s` body cannot run inside `parse_one`). Net 0 misses.
- Remaining cost in triggered commits is the 1 test_typing test (2.0 s) only where the module is a
  sqlglot/typing one or loaded by test_typing; with the loaders intersection, lazily loaded modules
  (parsers/*, executor/*, planner) select nothing.

Frequency: practice_like 3/120 commits, P 6/600, Q 9/1000, private 2/264. Typical saving per trigger:
2–34 s (25.5 → 2.0 when a planner/executor/tokenizer_core helper drags in tpcds/max_nodes).
