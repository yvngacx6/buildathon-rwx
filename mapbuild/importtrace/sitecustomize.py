"""Loaded by every Python process started with this folder on PYTHONPATH (mapbuild/import_map.py).

Records every module this process loads (the "import" audit event fires each time a module
is found and loaded, including importlib.import_module and lazy __getattr__ loads), one name
per line, in $BR_TRACE_OUT/<pid>.txt. Lines are flushed at once, so a pool worker that is
killed still leaves its record. Spawned children inherit the environment and trace too.
"""

import os
import sys

_OUT = os.environ.get("BR_TRACE_OUT")

if _OUT:
    _state = {"pid": None, "file": None}

    def _emit(text: str) -> None:
        pid = os.getpid()
        if _state["pid"] != pid:  # first event, or a forked child
            os.makedirs(_OUT, exist_ok=True)
            _state["file"] = open(os.path.join(_OUT, f"{pid}.txt"), "a", buffering=1)
            _state["pid"] = pid
        _state["file"].write(text + "\n")

    def _hook(event: str, args: tuple) -> None:
        if event == "import":
            try:
                _emit(f"import {args[0]}")
            except Exception:
                pass

    sys.addaudithook(_hook)

    import atexit

    @atexit.register
    def _files() -> None:  # name → file of every module still loaded (exact path mapping)
        for name, mod in list(sys.modules.items()):
            f = getattr(mod, "__file__", None)
            if f:
                try:
                    _emit(f"file {name} {f}")
                except Exception:
                    pass
