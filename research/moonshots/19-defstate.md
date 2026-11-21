# Moonshot 19 — conservative def-time + state diff on practice_like (DRAFT, numbers pending)

Branch `moon-19` (base a8fbd6b). All runs on rwx-big in the pinned image (`drun.sh`).

## Verdict
(pending)

## Where the 107.7 s (def-time 57.9 + state diff 28.9 + import-time map 20.9) goes

| commit | gap s | edit | why extra tests are selected |
|---|---|---|---|
| 09if | 30.5 | `_TokenizerBase.__init_subclass__`: `c[0]: c[1]` → `c[1]: c[1]` (+ no-op, fixture) | class-creation hook: `_COMMENTS` / `_KEYWORD_TRIE` change in every tokenizer → readers ≈ ALL (near-ALL rule) |
| c7om | 22.4 | `planner.py` `Join._to_s` body (+ no-op, fixture) | body never ran under any test, not at import → "unrun def → definition-time" → planner users incl. test_execute_tpcds |
| 7zco | 20.9 | `tokens._convert_quotes` (runs at import) | 122 changed keys (tokenizer `_FORMAT_STRINGS`/`_KEYWORD_TRIE`, dialect `BYTE_START`…) → 1179 readers incl. tpcds via DuckDB tokenizer |
| mn99, ptjt | 2.7, 2.3 | lambda body in `FUNCTIONS` table (exasol, tableau) | lambda never ran → `_by_names` Fallback → conservative |
| wumx, 0aqd, swmi, k5pu, o89y, 6o3v, 2qcc, h917, 358q, qur8, vlcc | 28.9 | dialect / parser flag flips | state diff readers = tests that ran the reading line ∩ "users" of the owner class; users come from the classes map, where `dialect_reach` credits every test that *names* a dialect (even write-only) to its tokenizer / parser / generator classes |
