"""Execute-to-decide prototype: per commit, select coverage candidates, run the cheap ones against the
patched tree (symlink overlay in /tmp), output only observed failures + statically-kept expensive candidates."""
import json, os, sys, time, pickle, subprocess, shutil, ast, resource
sys.path.insert(0, "/app")
from select_cands import Selector, parse_patch
DATA = os.environ.get("DATA", "/data"); SRC = "/opt/sqlglot"
EXPENSIVE = float(os.environ.get("EXPENSIVE", "5"))     # tests above this many recorded secs are decided statically
MODE = os.environ.get("MODE", "hybrid")                   # hybrid | static
PER_COMMIT_TIMEOUT = float(os.environ.get("TMO", "60"))
def log(*a): print(*a, file=sys.stderr, flush=True)

def apply_patch(old, hunks):
    lines = old.split("\n"); out = []; pos = 0
    for start, _, body in hunks:
        out.extend(lines[pos:start - 1]); pos = start - 1
        for ln in body:
            if ln[0] == " ": out.append(lines[pos]); pos += 1
            elif ln[0] == "-": pos += 1
            else: out.append(ln[1:])
    out.extend(lines[pos:]); return "\n".join(out)

def overlay(root, patched):
    """symlink farm: only directories on the path of a patched file are materialised"""
    os.makedirs(root)
    for name in ("sqlglot", "tests", "setup.cfg", "pyproject.toml"):
        os.symlink(f"{SRC}/{name}", f"{root}/{name}")
    for rel, text in patched.items():
        parts = rel.split("/"); cur = root
        for p in parts[:-1]:
            nxt = f"{cur}/{p}"
            if os.path.islink(nxt):
                target = os.readlink(nxt); os.unlink(nxt); os.mkdir(nxt)
                for e in os.listdir(target):
                    if e != "__pycache__" or True:
                        os.symlink(f"{target}/{e}", f"{nxt}/{e}")
            cur = nxt
        f = f"{cur}/{parts[-1]}"
        if os.path.lexists(f): os.unlink(f)
        if text is not None:
            with open(f, "w") as fh: fh.write(text)

def run_forked(root, ids, rec):
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    pid = os.fork()
    if pid == 0:
        try:
            os.chdir(root); sys.path[:] = [root, "/app"] + [p for p in sys.path if p not in (SRC, root, "/app", "")]
            os.environ.update(PYTHONPATH=f"{root}:/app", SKIP_INTEGRATION="0", FAILREC=rec)
            fd = os.open(os.devnull, os.O_WRONLY); os.dup2(fd, 1); os.dup2(fd, 2)
            if SYNC: import syncpool; syncpool.install()
            import pytest
            rc = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "failrec", "--timeout", os.environ.get("TEST_TIMEOUT", "20"), "--rootdir", root, *ids])
            os._exit(int(rc))
        except BaseException:
            os._exit(99)
    deadline = time.time() + PER_COMMIT_TIMEOUT
    while True:
        p, st = os.waitpid(pid, os.WNOHANG)
        if p: break
        if time.time() > deadline:
            os.kill(pid, 9); os.waitpid(pid, 0); st = 9 << 8; break
        time.sleep(0.02)
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    global LAST_CPU; LAST_CPU = (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime)
    class R: returncode = os.waitstatus_to_exitcode(st) if st != 9 << 8 else 98
    return R

LAST_CPU = 0.0
BUDGET = float(os.environ.get("BUDGET", "150"))
SYNC = os.environ.get("SYNC", "1") == "1"

OH = 0.7; PFAIL = float(os.environ.get("PFAIL", "0.4")); PFIX = float(os.environ.get("PFIX", "0.9"))
def pcost(ns, secs):
    w = partition(sorted(ns), secs)
    return max((sum(round(secs[n] * 1000) for n in x) for x in w), default=0) / 1000 + 2 * len(w)
def plan(C, pf, secs, cpu):
    """run the cheapest-CPU prefix R of C iff predicted exec cost beats outputting C statically"""
    best = (pcost(C, secs), set(), set(C))
    for X in sorted({cpu[n] for n in C}):
        R = {n for n in C if cpu[n] <= X}; S = set(C) - R
        pred = OH + sum(cpu[n] for n in R) + (1 - pf) * pcost(S, secs) + pf * pcost(C, secs)
        if pred < best[0]: best = (pred, R, S)
    return best[1], best[2]

def ast_equal(a, b):
    try: return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError: return False

def partition(nums, secs):
    if not nums: return []
    best = None
    for k in range(1, min(8, len(nums)) + 1):
        w = [[] for _ in range(k)]; ld = [0] * k
        for n in sorted(nums, key=lambda n: -secs[n]):
            i = ld.index(min(ld)); w[i].append(n); ld[i] += round(secs[n] * 1000)
        c = max(ld) + 2000 * k
        if best is None or c < best[0]: best = (c, w)
    return best[1]

def main():
    global LAST_CPU
    t0 = time.time()
    try: os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:int(os.environ.get("NCPU", "2"))])
    except Exception: pass
    import pytest, duckdb, pandas, numpy, pandas.testing, unittest.mock, concurrent.futures, multiprocessing  # warm fork server (no sqlglot!)
    assert "sqlglot" not in sys.modules
    sys.path[:] = [p for p in sys.path if p != SRC]
    tests = json.load(open(f"{DATA}/tests.json"))["tests"]
    secs = {t["number"]: t["seconds"] for t in tests}; byid = {t["id"]: t["number"] for t in tests}
    byfile = {}
    for t in tests: byfile.setdefault(t["file"], []).append(t["number"])
    cj = json.load(open("/app/cpu.json")); cpu = {t["number"]: cj.get(t["id"], t["seconds"]) for t in tests}
    sel = Selector(pickle.load(open("/app/map.pkl", "rb")), tests, SRC)
    log(f"setup {time.time()-t0:.2f}s")
    for pf in sorted(os.listdir(f"{DATA}/commits")):
        cid = pf[:-6]; tc = time.time()
        patch = open(f"{DATA}/commits/{pf}").read()
        try:
            files = parse_patch(patch); patched = {}; skip = set()
            for rel, info in files.items():
                src = f"{SRC}/{rel}"
                old = open(src).read() if os.path.exists(src) else ""
                patched[rel] = apply_patch(old, info["hunks"])
                if rel.endswith(".py") and old and ast_equal(old, patched[rel]): skip.add(rel)
            cand, notes = sel.candidates(patch, skip)
            datafile = any(not f.endswith(".py") for f in files)
        except Exception as e:
            log(cid, "selector error", e); cand = set(secs); patched = None
        if MODE == "static": run, static = [], set(cand)
        elif MODE == "all": static = {n for n in cand if secs[n] > EXPENSIVE}; run = sorted(cand - static)
        else:
            R, static = plan(cand, PFIX if datafile else PFAIL, secs, cpu); run = sorted(R)
        fails = set()
        if run and time.time() - t0 > BUDGET:   # batch-level deadline guard: stop executing, go static
            static |= set(run); run = []; notes.append("budget")
        if run and patched is not None:
            root = f"/tmp/w/{cid}"; rec = f"/tmp/fail_{cid}.txt"; open(rec, "w").close()
            try:
                overlay(root, patched)
                ids = [tests[n]["id"] for n in run]
                r = run_forked(root, ids, rec)
                for ln in open(rec):
                    kind, nid = ln.rstrip("\n").split(" ", 1)
                    if kind == "T" and nid in byid: fails.add(byid[nid])
                    elif kind == "C": fails |= {n for n in run if tests[n]["file"] == nid or tests[n]["id"].startswith(nid)}
                if r.returncode not in (0, 1): log(cid, "pytest rc", r.returncode); fails |= set(run) if r.returncode != 5 else set()
            except Exception as e:
                log(cid, "exec error", repr(e)); fails = set(run)
            finally:
                shutil.rmtree(root, ignore_errors=True)
        ccpu = LAST_CPU; LAST_CPU = 0.0
        out = sorted(fails | static)
        print(json.dumps({"commit": cid, "workers": partition(out, secs)}), flush=True)
        log(f"{cid} cand={len(cand)} ran={len(run)} static={len(static)} fails={len(fails)} {time.time()-tc:.1f}s cpu={ccpu:.2f} {'; '.join(notes)[:100]}")
    ru = resource.getrusage(resource.RUSAGE_SELF); rc = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"total wall {time.time()-t0:.1f}s cpu self {ru.ru_utime+ru.ru_stime:.1f} children {rc.ru_utime+rc.ru_stime:.1f}")
if __name__ == "__main__": main()
