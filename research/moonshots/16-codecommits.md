# 16 — Over-selection on practice-like code commits: what it is, and what sound rules can still cut

## Verdict and numbers
- **After R23 (statement-level for all tests, emulated here), code-commit waste is still ~85–98 s per
  40 practice commits.** Scaling: 24 code commits per 40, because P/Q are 60 % code.
  - practice_like: 34 code commits, base 319.9 → R23 259.4, oracle 120.8. Per 40 code commits:
    376 → 305 → 142.
  - private: 89 code commits, 873.0 → 806.0, oracle 493.2. R23 has 0 misses on all 139 labeled code commits.
- **The waste does not come from sub-statement imprecision.** Instruction-level (bytecode)
  coverage, prototyped and traced on 71 labeled function-body commits, saves **6.9 s total (≈ 2 s/40)**
  with 0 misses. Once R23 and diffexec run, most function-body commits keep only 1–3 non-slow tests,
  and the cost is the 2 s worker floor plus slow tests. **Do not build an instruction map.**
- **#1 sound refinement: R-DEF.** Edits that change only a default value currently take the
  "signature class users" path, i.e. about ALL (36.8 s).
  - **The rule:** select the tests that ran a call site that leaves the parameter at its default
    (statement level), intersected with the tests that ran the function. If any reference escapes,
    or for `*_sql` dispatch, use the function-level set instead. If a call or reference runs at
    definition time, keep today's rule. The rule also covers module constants that are read only as
    a default, e.g. `errors.ERROR_MESSAGE_CONTEXT_DEFAULT`.
  - **Offline re-score:** labeled 67.6 s saved over 123 code commits (≈ **13 s/40**), 0 misses (6 applicable).
    Unlabeled P+Q: 282 s saved over 400 code commits (≈ **17 s/40**).
  - **Mutant soundness check:** 14 default-flip mutants, 131 failing tests, **0 outside the rule**, 367 dropped tests passed (§4).
- **Expected gain for R-DEF on the practice board ≈ −10 to −15 s; P(works, no new misses) ≈ 0.8.** The default-edit
  mutant family recurs: 2 of 34 practice_like, 3 of 89 private, and 11 + 11 of 200 + 200 P/Q code commits.

## 1. Where the R23 residual goes (labeled, code kind; `an3.py`)
| case (mechanism that selected the extra tests) | practice_like 34 | private 89 | per 40 (avg) | slow share |
|---|---|---|---|---|
| function body: tests ran the touched statement but don't fail | 48.8 | 107.9 | 31 | 40 % (tpcds / max_nodes from the statement rule) |
| default-arg / signature edit → `_class_users` (≈ALL) | 34.4 + 27.4 | 36.8 + 14.6 (+ 12xo 32.8) | 25 | ~50 % |
| tokenizer class-creation hook (`_TokenizerBase.__init_subclass__`) → state diff over all dialects | – | 55.1 | 10 | 50 % |
| class body: state-diff readers (tokenizer flags, `NUMERIC_ESCAPES`, `INDEX_OFFSET`, …) | 7.0 | 54.6 | 9 | small |
| function runs at import, import state changed (`tokens._convert_quotes`) | 21.0 | – | 4 | 55 % |
| name readers (generator flags) / conservative def-time | – | 11.1 | 2 | 0 |

- **Function body.** The non-slow extras cost practice_like 22.4 s (12 commits; z8gc
  `datatype_sql` alone is 10.8) and private 27.9 s (27 commits). Zero-failure commits with a
  non-empty selection add 9.2 s and 34.5 s: the 2 s floor plus a few tests.
- **What keeps those tests: value equivalence, not sub-statement paths.** The changed instruction
  ran, but the value or assertion was unchanged. Only replay (diffexec, moon-12) or assertion
  slicing (moon-11) can remove them. Example, tggb (`decorrelate`): 14 tests ran the statement,
  only 2 ran the changed instruction, and neither fails. That is −3.6 s, the largest single
  instruction-level win.
- **Slow tests.** The statement rule keeps tpcds / max_nodes in gqeb, q26m, gbxa, 1k2u and 38yo:
  about 12 s per 40. That is the verification agents' domain (R22 / R24); I did not count it here.

## 2. Ranked sound refinements (excluding R22/R23/R24, moon-11/12/14/15)
| # | refinement | why it's sound | s/40 (labeled; P/Q) | evidence |
|---|---|---|---|---|
| 1 | **R-DEF**: literal-default edit → tests that ran a call site omitting the parameter (∩ the tests that ran the function); escaping refs / `*_sql` → tests that ran the function; definition-time sites → today's rule; + a constant read only in a default | A default is observed only by a call that binds it, and every such call runs the function body. sqlglot's two `inspect` uses read parameter names only | **13 ; 17** | a66z 36.8→0 (0 failing), 12xo 32.8→2.0 (0), P 4e2b/2c4k, Q 0pc9/0rqb/5t19/7r8j/ai6g → 0–10 s; 0 misses on 6 labeled |
| 2 | **Tokenizer field hop**: a tokenizer setting copied into `TokenizerCore(...)` (tokens.py:606) → follow the field to its `self.<field>` read sites in tokenizer_core, ∩ tests holding the dialect | The setting is read only where the field is read; today the "reader" is the constructor, which every tokenizing test runs | ~5–8 (est.) ; ~6 | p3ow, gozk, 2iie, ga8d, w7sj, 48vt, d3ad (35 s, 0 failing) + part of the hook cases; not prototyped |
| 3 | **Key-level state diff** for class-creation hooks and import-run builders (dv9g, jtc6, 7zco; P/Q 93uz, 5ney, bc18, b0be, 2vm8, 7evr, 3l6t, 8du4, 2l1l): diff the changed dict keys, then select the tests that looked up those keys | A lookup of an unchanged key returns the same value. Needs a new build-time key-read map (wrap the class dicts). Risky: `type(d) is dict` checks, `.items()` iteration counts as reading every key | ≤ 15 ; ≤ 18 (upper bound) | dv9g has 0 failing; jtc6 has 4 of 754. Large effort, ~1 day |
| 4 | **Instruction-level coverage** (bytecode diff of old vs new code, `idiff.py`) | Execution matches step for step until the first differing instruction | **2** ; – | 71 traced commits: −6.9 s, 0 misses. Not worth a map |
| 5 | **R-DEF for constructors** (`__init__` defaults → `ClassName(` call sites; 38yo `Simplifier`, u7s0) | Same as #1, with constructor resolution through subclasses | ~2 ; ~1 | 38yo has 5 failing; the slow part needs the statement rule anyway |

- **Dialect dispatch ("generator method of dialect X selected via subclass Y that overrides the
  branch").** R23 already handles it: an overriding subclass never runs the base statement. The
  dialect-scoped cases that remain are class attributes (#2, #3), not method dispatch.

## 3. R-DEF prototype (`16-tools/proto_def.py`, `run_def.py`, `const12.py`)
- **Trigger.** The AST diff is confined to `args.defaults[i]` or `kw_defaults[i]` of one or more defs.
- **Call sites.** Every `Call` whose `.attr` or bare name equals the function name, over sqlglot/ + tests/:
  - The parameter counts as supplied if a keyword names it, or if the positional count is ≥ its index
    counting `self` (conservative for unbound calls). `*args` / `**kw` never count as supplying it.
  - A call that omits the parameter is "observing". It contributes the tests that ran its statement
    (header lines for compound statements). The result is intersected with the function-level tests.
- **Fallbacks.** A non-call reference, or the name as a string literal (getattr), makes the
  function-level tests the answer. So does a `*_sql` / dunder name (dynamic dispatch). A
  definition-time site returns `None`, which keeps today's rule.
- **Integration.** Replace `_class_users` for `r.signature` when `_edited_defs` shows a
  literal-default-only edit.
  - Keep the existing `_function_at_import` import-state check: a66z `_parse_factor` runs at import
    and the state diff says +0.
  - Keep diffexec afterwards, and take the min: Q 9qe8 is already 0 via diffexec.
- **Results.** P: 46.6 → 21.2 s; Q: 205 → 108 s (+ the errors.py constant in P 1hwf and Q af97,
  32.8 → ~2 s each). Labeled: a66z 36.8 → 0; 12xo 32.8 → 2.0 (12 tests); 0 misses.

## 4. Soundness check by mutants (EC2)
See `16-tools/mkdefmut.py` and `dmver.py`.
- **Mutants:** flip a literal `True`/`False` default in sqlglot functions where R-DEF would *drop*
  tests: tests that ran the function but none of the default-observing call sites.
- **Labels:** run the rule's tests plus the dropped tests (non-slow) on the mutated tree.
- **Pass condition:** no failure outside the rule.

**Results (EC2, pinned image).**

| | count |
|---|---|
| mutants | 14 |
| mutants with failing tests | 5 (131 failing tests) |
| failures outside the rule | **0** |
| tests dropped by the rule (run, all passed) | 367 |

| mutant | ran the function | rule keeps | failing | outside rule |
|---|---|---|---|---|
| `_parse_select.parse_subquery_alias` | 1155 | 1121 | 123 | 0 |
| `to_dot.include_dots` | 30 | 16 | 2 | 0 |
| `clickhouse._parse_function.anonymous` | 99 | 58 | 2 | 0 |
| `_parse_wrapped.optional` | 319 | 200 | 1 | 0 |
| `schema._find.raise_on_missing` | 239 | 209 | 3 | 0 |
| `merge_ctes.leave_tables_isolated` | 96 | 0 | 0 | – |
| `_parse_withdata.no` | 5 | 0 | 0 | – |
| `date_delta_to_binary_interval_op.cast` | 17 | 0 | 0 | – |

- Mutants m0 and m1 were parsed with an older failure regex. Their summary lines show no failures at all.
- **Caveat on the instruction-level numbers (§1).** Hits were attributed in a single process.
  setUp/setUpClass hits count for the whole class, and hits outside any test count for all tests.
  Hits through lazy caches can be under-counted, so −6.9 s is an upper bound.

## 5. Recommendation
Implement R-DEF in `_library`'s signature branch (~120 lines, reuse `nameindex` sites instead of the
AST walk), run the full labeled regression plus the mutant check, and ship it if it shows 0 misses. Skip
instruction-level. #2 is the next candidate if someone has 3–4 h; #3 only with a day to spare.
