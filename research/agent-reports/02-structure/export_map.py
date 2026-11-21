"""Export coverage .coverage (pytest-cov --cov-context=test) to compact map:
{"tests": [nodeid...], "files": {relpath: {line: [test_idx...]}}} with "" context -> -1."""
import coverage, json, sys, os, gzip
cov_file, root, out = sys.argv[1], sys.argv[2], sys.argv[3]
d = coverage.CoverageData(cov_file); d.read()
tests = sorted({c.split("|")[0] for c in d.measured_contexts() if c})
idx = {t: i for i, t in enumerate(tests)}
files = {}
for f in d.measured_files():
    rel = os.path.relpath(f, root)
    m = {}
    for ln, ctxs in d.contexts_by_lineno(f).items():
        s = sorted({idx[c.split("|")[0]] if c else -1 for c in ctxs})
        if s: m[ln] = s
    files[rel] = m
with gzip.open(out, "wt") as fh:
    json.dump({"tests": tests, "files": files}, fh, separators=(",", ":"))
