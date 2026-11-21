# Moonshot 12: compact, position-independent input encoding for diffexec (hash-consed Expr trees)

## Verdict (DRAFT — replay-agreement experiment pending): encoding works, but it is not the binding constraint

- **The encoding works (prototype, `12-encoding-tools/hcons.py`).** Every Expr in a call input becomes
  `(root node id in a global content-addressed node table, per-input root ordinal, path)`, plus a
  hash-consed "type signature" that keeps `_type` object identity exact.
  - Round trip: digest-equal (`serial.digest_cmp`, the effect digest diffexec compares) on 80/80
    TPC-DS inputs, i.e. 20 queries, raw and optimized, as whole-tree and leaf-node inputs. It handles
    shared `_type` DataTypes and `Cast.to` used as `_type`; the first version fell back on those.
  - Size: 2.21 MB raw pickles → 0.20 MB solid-xz (today's best) → **0.078 MB node table + 0.5 kB of
    per-call residual** (xz). That is 2.5x better than solid xz on 40 distinct trees. The real gain is
    that per-call cost becomes ~10–50 bytes: storage now scales with *distinct tree states*, not with
    calls × tree size.
  - Decode: **4.4 µs/node** vs pickle 6.3 µs/node.
- **Scope.**
  - **test_generator_memory: impossible.** The recorder marks every tracemalloc phase incomplete, and a
    memory regression has no effect digest.
  - **test_max_nodes: impossible.** 70.4 M calls (`_match_set` 8.6 M, `_advance` 4.0 M), and every
    Parser call carries a distinct `_index` over a ~1 M-token list. Replay cost ≈ test cost whatever
    the encoding.
  - **Target = test_execute_tpcds** (+tpch/opt_tpch). The census gives 909 functions and 65.8 M calls.
    Of the functions: 215 are called ≤100 times, 519 ≤1,000, and 871 ≤100 k.
- **The binding constraint is replay CPU, not bytes.**
  - Each call's input reaches the whole query tree through `parent`. Replay must therefore decode and
    then digest the whole tree, twice (old and new).
  - Digest: 27 µs/node. The tree is ~290 nodes (optimized TPC-DS average). That is **≈ 2 × 9 ms ≈
    20 ms per distinct input on the laptop, ≈ 40 ms on the scorer**, before running f.
  - Break-even against a ~16 s tpcds drop means at most ~100 distinct tpcds inputs per changed function.
- **Gain ceiling (P + Q, 1,600 unlabeled practice-style mutants, run r20gm3).**
  - 133 commits cost ≥20 s with "not recorded (budget/calls)": 3.3 per 40 commits, 87 s per 40 of
    total cost.
  - Only **26** of them change functions that are called 1–100× in tpcds and are not whole-pipeline
    entry points (`optimize`, `qualify`, `execute`, whose replay re-runs the optimizer or executor).
    That is 0.65 per 40.
  - Expected ≈ 0.65 × 16 s × P(all replays agree). With P ≈ 0.5, about **5 s per 40 commits**
    (up to ~10 s if the 101–1,000-call tier is made cheap, see below).
  - practice_like is richer: 4/24 commits (1k2u, fq85, tggb, z8gc) keep tpcds through budget, all with
    tpcds passing.
