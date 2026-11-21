# pytest plugin: per-test CPU seconds (setup+call+teardown) -> /out/cpu.json ; installs SyncPool
import time, json, os, pytest, syncpool
syncpool.install()
acc = {}
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    t = time.process_time(); yield; acc[item.nodeid] = time.process_time() - t
def pytest_sessionfinish(session):
    json.dump(acc, open(os.environ.get("CPUOUT", "/out/cpu.json"), "w"))
