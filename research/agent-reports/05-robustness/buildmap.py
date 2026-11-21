"""Combine per-file coverage data into {file: {line: [contexts]}} json (contexts '' = import/collection time)."""
import glob, json, sys, coverage
out = {}
for f in glob.glob(sys.argv[1] + "/cov.*"):
    d = coverage.CoverageData(basename=f); d.read()
    for src in d.measured_files():
        rel = src.split("/sqlglot/", 1)[1] if "/sqlglot/sqlglot/" in src else src
        rel = "sqlglot/" + src.split("/sqlglot/sqlglot/", 1)[1]
        m = out.setdefault(rel, {})
        for ln, ctxs in d.contexts_by_lineno(src).items():
            m.setdefault(ln, set()).update(ctxs)
json.dump({f: {l: sorted(c) for l, c in m.items()} for f, m in out.items()}, open(sys.argv[2], "w"))
