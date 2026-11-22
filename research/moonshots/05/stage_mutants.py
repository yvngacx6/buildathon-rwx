"""Adversarial mutants for the stage-checkpoint gap (optimize writes state that execute reads).
python mkmut.py SQLGLOT OUTDIR  → OUTDIR/commits/<id>.patch + OUTDIR/meta.json"""
import difflib, json, os, sys

root, out = sys.argv[1], sys.argv[2]
os.makedirs(out + "/commits", exist_ok=True)

MUTANTS = {
    "stg1": ("sqlglot/optimizer/optimize_joins.py", "def _is_reorderable(", [
        "from sqlglot.executor.env import ENV",
        'ENV["SUM"] = lambda *a, **k: 0',
    ], "import-bound ENV, subscript store"),
    "stg2": ("sqlglot/optimizer/optimize_joins.py", "def normalize(", [
        "import sqlglot.executor.env as _env_mod",
        "table = _env_mod.ENV",
        'table["ROUND"] = lambda *a, **k: 0',
    ], "local alias of a module dict, subscript store"),
    "stg3": ("sqlglot/optimizer/isolate_table_selects.py", "def isolate_table_selects(", [
        "import sqlglot.executor.env as _env_mod",
        '_env_mod.ENV.update({"COALESCE": lambda *a: None})',
    ], "mutating method on an imported module's dict"),
    "stg4": ("sqlglot/optimizer/pushdown_predicates.py", "def pushdown(", [
        "from sqlglot.generators.python import PythonGenerator",
        'setattr(PythonGenerator, "TRANSFORMS", {})',
    ], "setattr on a class the executor's generator uses"),
    "stg5": ("sqlglot/optimizer/canonicalize.py", "    def _canonicalize(", [
        "exp.Sum.is_var_len_args = True",
        "exp.Coalesce.is_var_len_args = False",
    ], "class-attribute store through module-level exp, inside a closure"),
}

meta = {}
for mid, (path, head, lines, desc) in MUTANTS.items():
    src = open(os.path.join(root, path)).read().splitlines(keepends=True)
    i = next(k for k, l in enumerate(src) if l.startswith(head))
    j = i
    while not src[j].rstrip().endswith(":"):
        j += 1
    indent = " " * (len(src[i]) - len(src[i].lstrip()) + 4)
    new = src[: j + 1] + [indent + l + "\n" for l in lines] + src[j + 1:]
    d = difflib.unified_diff(src, new, f"a/{path}", f"b/{path}")
    with open(f"{out}/commits/{mid}.patch", "w") as fh:
        fh.write(f"diff --git a/{path} b/{path}\n" + "".join(d))
    meta[mid] = {"kind": "adversarial-stage", "desc": desc, "files": [path]}
json.dump(meta, open(out + "/meta.json", "w"), indent=1)
print(sorted(meta))
