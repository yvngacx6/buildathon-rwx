"""Pick the hot functions (> 50k calls over the suite) worth recording per test phase (round 15).

    python mapbuild/diffexec_hot.py CENSUS_DIR tests.json OUT.pkl [K] [CALL_BUDGET]

CENSUS_DIR holds per-test call counts (census_*.pkl: {phase: {function key: calls}}, a pytest
plugin counting PY_START per (test phase, sqlglot code object), pinned sqlglot). Inputs are the
census and tests.json only: no labels, patches or mutants.

A hot function's test is droppable only if all its calls in that test are recorded, so it is
recorded with a cap of K calls per test phase (record.py DIFFEXEC_PHASE_CAP). Per function:
saving = what a commit changing only it would save (scorer packing cost) if every test with
<= K calls were dropped (test_max_nodes is never droppable for parser / tokenizer methods: its
`self` holds a ~1M-token list, which is elided, and any replay that indexes it is refused);
cost = calls to pickle. Functions are taken by lines * saving ** 1.5 / calls until
CALL_BUDGET calls (recording costs ~1 ms per call).
"""

from __future__ import annotations

import collections
import glob
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diffexec import build  # noqa: E402
from selector import pack  # noqa: E402

MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"


def main() -> None:
    census, tests_json, out = sys.argv[1:4]
    K = int(sys.argv[4]) if len(sys.argv) > 4 else 20000
    budget = float(sys.argv[5]) if len(sys.argv) > 5 else 1.5e6
    T = json.loads(Path(tests_json).read_text())["tests"]
    num = {t["id"]: t["number"] for t in T}
    ms = {t["number"]: pack.duration_ms(t["seconds"]) for t in T}
    per: dict = collections.defaultdict(collections.Counter)
    for f in glob.glob(census + "/census_*.pkl"):
        for k, c in pickle.load(open(f, "rb")).items():
            node = k[1] if isinstance(k, tuple) else "<" + k + ">"
            for fk, n in c.items():
                per[fk][node] += n

    def cost(nums):
        return pack.cost_ms(pack.pack(set(nums), ms), ms) / 1000 if nums else 0

    rows = []
    for fk, c in per.items():
        if sum(c.values()) <= 50000:
            continue
        g = sum(v for t, v in c.items() if t.startswith("<"))
        users = {t: v for t, v in c.items() if not t.startswith("<") and t in num}
        small = {t for t, v in users.items() if v <= K}
        if fk[0].startswith(("sqlglot/parser", "sqlglot/parsers/", "sqlglot/tokenizer")):
            small.discard(MAX_NODES)
        sav = cost({num[t] for t in users}) - cost({num[t] for t in users if t not in small})
        calls = g + sum(users[t] for t in small)
        if sav > 0.5:
            rows.append((build.lines_of(fk) * sav ** 1.5 / calls, fk, sav, calls))
    rows.sort(key=lambda r: (-r[0], repr(r[1])))
    acc, keys = 0, set()
    for _, fk, sav, calls in rows:
        if acc + calls <= budget:
            acc += calls
            keys.add(fk)
    Path(out).write_bytes(pickle.dumps(keys))
    print(f"K={K}: {len(keys)} of {len(rows)} hot functions, {acc} calls to pickle")


if __name__ == "__main__":
    main()
