"""Which part of the tree does the pickle round trip lose at rule boundaries?"""
import pickle, sys, zipfile
sys.path.insert(0, "/w/repo")
sys.path.insert(0, "/shared/sqlglot")
sys.setrecursionlimit(1000)
from selector import subtest as S
from tests.helpers import TPCDS_SCHEMA
import sqlglot.executor as E
from sqlglot.expressions import Expr


def walk(root):
    out = []
    seen = {}
    st = [(root, "")]
    while st:
        n, path = st.pop()
        if not isinstance(n, Expr):
            out.append((path, "V", repr((type(n).__name__, n))))
            continue
        if id(n) in seen:
            out.append((path, "R", seen[id(n)]))
            continue
        seen[id(n)] = path
        out.append((path, "N", type(n).__name__))
        t = getattr(n, "_type", None)
        out.append((path, "T", t.sql() if t is not None and t is not n else None))
        out.append((path, "C", repr(getattr(n, "comments", None))))
        m = getattr(n, "_meta", None)
        out.append((path, "M", repr(sorted(m.items())) if m else None))
        out.append((path, "K", tuple(n.args)))
        for k, v in n.args.items():
            if type(v) is list:
                for i, x in enumerate(v):
                    st.append((x, f"{path}/{k}[{i}]"))
            else:
                st.append((v, f"{path}/{k}"))
    return out


meta = pickle.loads(zipfile.ZipFile("/w/repo/index/subtest.zip").read("meta.pkl"))
import collections
kinds = collections.Counter()
for i, q in enumerate(meta["suites"]["tpcds"][:12]):
    if not q.get("execute") or not q["outs"].get("d_opt"):
        continue
    e = pickle.loads(q["outs"]["pexpr"])
    sch = E.ensure_schema(TPCDS_SCHEMA, dialect=None)
    _, cks, digs, dfin, _ = S.rule_steps(e, sch, keep=True)
    for k in (1, 5, 14):
        # rebuild the live tree at boundary k (rule_steps keeps only pickles): re-run
        import sqlglot.optimizer.optimizer as O
        e2 = pickle.loads(q["outs"]["pexpr"])
        live = O.optimize(e2, E.ensure_schema(TPCDS_SCHEMA, dialect=None), leave_tables_isolated=True,
                          dialect=None, rules=O.RULES[:k])
        assert S.strict_digest(live) == digs[k], "live mismatch"
        a, b = walk(live), walk(pickle.loads(cks[k]))
        if len(a) != len(b):
            print(i, k, "len", len(a), len(b))
        for x, y in zip(a, b):
            if x != y:
                kinds[(k, x[1])] += 1
                if kinds[(k, x[1])] <= 2:
                    print(i, k, "live", x, "\n      rt  ", y)
                break
print(kinds)
