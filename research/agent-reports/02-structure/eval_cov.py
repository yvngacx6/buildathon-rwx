"""Evaluate a line-coverage selector on sample commits.
usage: eval_cov.py covmap.json.gz sample_dir"""
import json, gzip, re, sys, os
covmap = json.load(gzip.open(sys.argv[1], "rt"))
S = sys.argv[2]
tests = json.load(open(f"{S}/tests.json"))["tests"]
ans = json.load(open(f"{S}/answers.json"))["commits"]
num = {t["id"]: t["number"] for t in tests}
secs = {t["number"]: t["seconds"] for t in tests}
cidx2num = {i: num.get(t) for i, t in enumerate(covmap["tests"])}
files = {f: {int(k): v for k, v in m.items()} for f, m in covmap["files"].items()}

# fixture file -> test functions (static, from check_file / load_sql_fixture* calls)
O = "tests/test_optimizer.py::TestOptimizer::"
FIX = {f"tests/fixtures/optimizer/{f}.sql": [O + t] for f, t in [
    ("optimizer", "test_optimize"), ("isolate_table_selects", "test_isolate_table_selects"),
    ("qualify_tables", "test_qualify_tables"), ("normalize", "test_normalize"),
    ("qualify_columns", "test_qualify_columns"), ("qualify_columns_ddl", "test_qualify_columns"),
    ("qualify_columns__with_invisible", "test_qualify_columns__with_invisible"),
    ("pushdown_cte_alias_columns", "test_pushdown_cte_alias_columns"),
    ("normalize_identifiers", "test_normalize_identifiers"), ("quote_identifiers", "test_quote_identifiers"),
    ("pushdown_projections", "test_pushdown_projection"), ("simplify", "test_simplify"),
    ("unnest_subqueries", "test_unnest_subqueries"), ("pushdown_predicates", "test_pushdown_predicates"),
    ("optimize_joins", "test_optimize_joins"), ("eliminate_joins", "test_eliminate_joins"),
    ("eliminate_ctes", "test_eliminate_ctes"), ("merge_subqueries", "test_merge_subqueries"),
    ("eliminate_subqueries", "test_eliminate_subqueries"),
    ("canonicalize_internal_names", "test_canonicalize_internal_names"), ("canonicalize", "test_canonicalize"),
    ("qualify_columns__invalid", "test_qualify_columns__invalid"), ("annotate_types", "test_annotate_types"),
    ("annotate_functions", "test_annotate_funcs")]}

def parse_patch(text):
    """yield (file, removed_old_lines, insertion_points)"""
    out = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else line[4:]
            out[cur] = ([], [])
        elif line.startswith("@@"):
            o = int(re.match(r"@@ -(\d+)", line).group(1)); old = o
        elif cur and line.startswith("-") and not line.startswith("---"):
            out[cur][0].append(old); old += 1
        elif cur and line.startswith("+"):
            out[cur][1].append(old)  # inserted before old line `old`
        elif cur and (line.startswith(" ") or line == ""):
            old += 1
    return out

def select(patch, comment_neighbors=True, imp=False):
    sel, notes = set(), []
    for f, (rem, ins) in parse_patch(patch).items():
        if f in FIX:
            sel |= {num[t] for t in FIX[f]}; continue
        if f.startswith("tests/fixtures"):
            notes.append("unmapped fixture " + f); continue
        m = files.get(f)
        if m is None:
            notes.append("no coverage for " + f); continue
        lines = set(rem)
        if comment_neighbors:
            for p in ins: lines |= {p - 1, p}
        for ln in lines:
            for c in m.get(ln, []):
                if c == -1: notes.append(f"{f}:{ln} import-time only" if len(m[ln]) == 1 else f"{f}:{ln} import+tests")
                elif cidx2num.get(c) is not None: sel.add(cidx2num[c])
    return sel, notes

def cost(sel):
    if not sel: return 0.0
    ms = sorted((round(secs[n] * 1000) for n in sel), reverse=True)
    best = None
    for k in range(1, 9):
        loads = [0] * k
        for d in ms:
            i = loads.index(min(loads)); loads[i] += d
        c = max(loads) + 2000 * k
        best = c if best is None or c < best else best
    return best / 1000

if __name__ == "__main__":
  for cn in (True, False):
      tot = miss = 0; tsum = 0; nsel = 0
      print(f"\n=== comment_neighbors={cn}")
      for cid in sorted(ans):
          fail = set(ans[cid]["failing"])
          sel, notes = select(open(f"{S}/commits/{cid}.patch").read(), cn)
          missed = fail - sel
          c = cost(sel) + (300 if missed else 0)
          tot += c; miss += bool(missed); tsum += sum(secs[n] for n in sel); nsel += len(sel)
          print(f"{cid} {ans[cid]['kind']:7s} fail={len(fail):2d} sel={len(sel):4d} secs={sum(secs[n] for n in sel):7.2f} cost={c:7.1f} missed={sorted(missed)} {'; '.join(sorted(set(notes)))[:150]}")
      print(f"TOTAL cost={tot:.1f} misses={miss} selected_tests={nsel} selected_secs={tsum:.1f}")
