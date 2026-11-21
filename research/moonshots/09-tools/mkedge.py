"""Craft edge-case patches (format / kind surprises) to check the selector never crashes and falls back sanely."""
import json, shutil, sys
from pathlib import Path
out = Path(sys.argv[1]); sample = Path(sys.argv[2])
(out / 'commits').mkdir(parents=True, exist_ok=True)
shutil.copy(sample / 'tests.json', out / 'tests.json')
P = {}
P['mode'] = """diff --git a/sqlglot/errors.py b/sqlglot/errors.py
old mode 100644
new mode 100755
"""
P['binary'] = """diff --git a/sqlglot.png b/sqlglot.png
index 1111111..2222222 100644
Binary files a/sqlglot.png and b/sqlglot.png differ
"""
P['binsrc'] = """diff --git a/sqlglot/errors.py b/sqlglot/errors.py
index 1111111..2222222 100644
Binary files a/sqlglot/errors.py and b/sqlglot/errors.py differ
"""
P['empty'] = ""
P['crlf'] = ("diff --git a/sqlglot/errors.py b/sqlglot/errors.py\r\n--- a/sqlglot/errors.py\r\n+++ b/sqlglot/errors.py\r\n"
             "@@ -1,3 +1,3 @@\r\n-from __future__ import annotations\r\n+from __future__ import annotations  # x\r\n \r\n import typing as t\r\n")
P['rename100'] = """diff --git a/sqlglot/errors.py b/sqlglot/errors2.py
similarity index 100%
rename from sqlglot/errors.py
rename to sqlglot/errors2.py
"""
P['copy'] = """diff --git a/sqlglot/errors.py b/sqlglot/errors3.py
similarity index 100%
copy from sqlglot/errors.py
copy to sqlglot/errors3.py
"""
P['symlink'] = """diff --git a/sqlglot/link.py b/sqlglot/link.py
new file mode 120000
index 0000000..1111111
--- /dev/null
+++ b/sqlglot/link.py
@@ -0,0 +1 @@
+errors.py
\\ No newline at end of file
"""
P['conftest'] = """diff --git a/tests/conftest.py b/tests/conftest.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/tests/conftest.py
@@ -0,0 +1,2 @@
+import pytest
+raise RuntimeError
"""
P['initpy'] = """diff --git a/tests/__init__.py b/tests/__init__.py
new file mode 100644
--- /dev/null
+++ b/tests/__init__.py
@@ -0,0 +1 @@
+X = 1
"""
P['pth'] = """diff --git a/sqlglot/py.typed b/sqlglot/py.typed
deleted file mode 100644
index e69de29..0000000
"""
P['nonl'] = """diff --git a/sqlglot/__main__.py b/sqlglot/__main__.py
--- a/sqlglot/__main__.py
+++ b/sqlglot/__main__.py
@@ -1,1 +1,1 @@
-import argparse
\\ No newline at end of file
+import argparse  # c
\\ No newline at end of file
"""
P['latin1'] = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,1 @@\n-x\n+\xe9\xff\n"
P['gitmodules'] = """diff --git a/.gitmodules b/.gitmodules
--- a/.gitmodules
+++ b/.gitmodules
@@ -1,1 +1,1 @@
-[submodule "sqlglot-integration-tests"]
+[submodule "sqlglot-integration-testsX"]
"""
for k, v in P.items():
    data = v.encode('latin-1') if k == 'latin1' else v.encode()
    (out / 'commits' / f'{k}.patch').write_bytes(data)
print(len(P))
