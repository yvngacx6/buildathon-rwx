import os
def pytest_runtest_logreport(report):
    if report.failed:
        with open(os.environ['FAILREC'], 'a') as f:
            f.write(report.nodeid.split('[')[0] + '\n')
