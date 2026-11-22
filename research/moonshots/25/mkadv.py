"""Hand-written moon-25 adversarial mutants (generate-checkpoint controls): mkadv.py SQLGLOT OUTDIR.
Writes OUTDIR/commits/<id>.patch; labels come from the real tests (truth25.sh)."""
import difflib, os, sys

S, out = sys.argv[1], sys.argv[2]
M = {
    # output of the executor's generator changes on tpcds paths (sort direction, CASE default,
    # safe division)
    "a25o": ("sqlglot/generators/python.py",
             '    desc = "True" if expression.args.get("desc") else "False"',
             '    desc = "False" if expression.args.get("desc") else "True"'),
    "a25c": ("sqlglot/generators/python.py",
             '    chain = self.sql(expression, "default") or "None"',
             '    chain = self.sql(expression, "default") or "0"'),
    "a25d": ("sqlglot/generators/python.py",
             '        denominator += " or None"',
             '        denominator += " or 1"'),
    # generator instance state only (_next_name counter): same strings, different state
    "a25n": ("sqlglot/generators/python.py",
             '            return self.func(e.key, *values)\n\n        if',
             '            self._next_name()\n            return self.func(e.key, *values)\n\n        if'),
    # same, without shifting lines (the swap keys code objects by first line): same strings,
    # the _next_name counter advances → only the final generator state differs
    "a25s": ("sqlglot/generators/python.py",
             '                return self.func(e.key, values)\n',
             '                return self.func(e.key, values) if self._next_name() else None\n'),
    # identify never restored after no_identify: state leaks into later calls
    "a25i": ("sqlglot/generator.py",
             '        self.identify = original\n',
             '        self.identify = original and False\n'),
}
os.makedirs(f"{out}/commits", exist_ok=True)
for cid, (path, old, new) in M.items():
    a = open(os.path.join(S, path)).read()
    assert a.count(old) == 1, cid
    b = a.replace(old, new)
    d = difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{path}", f"b/{path}", n=3)
    open(f"{out}/commits/{cid}.patch", "w").write(f"diff --git a/{path} b/{path}\n" + "".join(d))
print("ok", len(M))
