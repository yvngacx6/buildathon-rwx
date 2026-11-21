# pytest plugin: append failing nodeids (any phase, incl. subtests and collection errors) to $FAILREC
import os
_out = open(os.environ["FAILREC"], "a")
def pytest_runtest_logreport(report):
    if report.failed:
        _out.write("T " + report.nodeid + "\n"); _out.flush()
def pytest_collectreport(report):
    if report.failed:
        _out.write("C " + report.nodeid + "\n"); _out.flush()
