"""pytest plugin (controller side only): append one JSON line per report to $OUTCOMES.

Lines: {"k": "r", "id": nodeid, "when": setup|call|teardown, "o": passed|failed|skipped, "sub": bool}
       {"k": "c", "id": collector nodeid, "o": ...}          (collection reports; only failures are written)
Works with xdist: worker reports (including pytest 9 subtest reports) are replayed on the controller.
"""
import json
import os

_out = None


def pytest_configure(config):
    global _out
    if hasattr(config, "workerinput"):  # xdist worker: the controller records
        return
    _out = open(os.environ["OUTCOMES"], "a", buffering=1)


def pytest_runtest_logreport(report):
    if _out is None:
        return
    sub = type(report).__name__ != "TestReport" or bool(getattr(report, "context", None))
    _out.write(json.dumps({"k": "r", "id": report.nodeid, "when": report.when, "o": report.outcome,
                           "sub": sub, "lr": (report.longreprtext or "")[-300:] if report.failed else ""}) + "\n")


def pytest_collectreport(report):
    if _out is not None and report.failed:
        _out.write(json.dumps({"k": "c", "id": report.nodeid, "o": report.outcome,
                               "lr": (report.longreprtext or "")[-500:]}) + "\n")


def pytest_sessionfinish(session, exitstatus):
    if _out is not None:
        _out.write(json.dumps({"k": "end", "exit": int(exitstatus)}) + "\n")
