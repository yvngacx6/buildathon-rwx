"""r26: real CPU of genmem's child (all dialects / one family module), in the image."""
import sys

sys.path.insert(0, "/app")
from selector import genmem  # noqa: E402

root = "/opt/sqlglot"
for path, everything in (("sqlglot/generator.py", True), ("sqlglot/dialects/duckdb.py", False)):
    text = open(f"{root}/{path}").read() + "\n# r26 probe\n"
    for _ in range(3):
        out, cpu = genmem.run_child(root, {path: text}, everything, 30)
        print(path, "everything" if everything else "family", f"cpu {cpu:.2f}s",
              (out or {}).get("ok"), len((out or {}).get("ran", [])), (out or {}).get("why"))
