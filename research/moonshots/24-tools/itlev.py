#!/usr/bin/env python3
"""Instruction-level re-check of the B tests (fast extras that ran a touched statement and
survived diffexec) from dispatch.py's JSON.  Run in the pinned image from a repo checkout.

  python itlev.py prep d.json SETNAME setdir workdir   -> workdir/<commit>.{spec.json,ids}, jobs.txt
  (then: cd <old sqlglot copy>; PYTHONPATH=. python itrace.py spec out ids...  per job)
  python itlev.py score d.json SETNAME setdir workdir  -> per-commit seconds saved
A B test is droppable iff none of its hits ran a differing OLD instruction (idiff), counting
setup-phase hits for its whole class and pre-test hits ('*') for everyone.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.getcwd())
from selector import diff, pack  # noqa: E402
import idiff  # noqa: E402

SG = os.environ.get("BR_SQLGLOT", "/shared/sqlglot")


def spec(patch_text):
    res = {}
    for fp in diff.parse(patch_text):
        p = fp.path
        if not (p.startswith("sqlglot/") and p.endswith(".py")):
            continue
        old = open(f"{SG}/{p}").read()
        new = diff.apply(old, fp)
        try:
            res[p] = {f"{q}|{l}": (v if v == "WHOLE" else sorted(v))
                      for (q, l), v in idiff.diff_source(old, new, f"{SG}/{p}").items()}
        except SyntaxError:
            res[p] = "SYNTAX"
    return res


def main():
    mode, dj, name, setdir, work = sys.argv[1:6]
    rows = json.load(open(dj))[name]
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    tests = json.load(open(f"{setdir}/tests.json"))["tests"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}

    def cost(ns):
        return pack.cost_ms(pack.pack(set(ns), ms_of), ms_of) / 1000 if ns else 0.0

    if mode == "prep":
        jobs = []
        for r in rows:
            if r.get("all") or not r.get("Bl"):
                continue
            sp = spec(open(f"{setdir}/commits/{r['commit']}.patch").read())
            if not sp or any(v == "SYNTAX" for v in sp.values()):
                continue
            json.dump(sp, open(work / f"{r['commit']}.spec.json", "w"))
            fids = [id_of[n] for n in r["Fl"]][:300]
            (work / f"{r['commit']}.ids").write_text(" ".join(
                [r["ids"].get(str(n), r["ids"].get(n)) for n in r["Bl"]] + fids))
            jobs.append(r["commit"])
        (work / "jobs.txt").write_text("\n".join(jobs) + "\n")
        print(len(jobs), "jobs")
        return
    tot_b = tot_save = 0.0
    for r in rows:
        if r.get("all") or not r.get("Bl"):
            continue
        out = work / f"{r['commit']}.hits.json"
        ids = r["ids"]
        if not out.is_file():
            print(f"  {r['commit']:10s} no trace")
            continue
        hits = set(json.load(open(out)))
        drop = set()
        if "*" not in hits:
            for n in r["Bl"]:
                tid = ids.get(str(n), ids.get(n))
                if tid not in hits and "class:" + tid.rsplit("::", 1)[0] not in hits:
                    drop.add(n)
        fmiss = [id_of[n] for n in r["Fl"][:300] if id_of[n] not in hits
                 and "class:" + id_of[n].rsplit("::", 1)[0] not in hits and "*" not in hits]
        N = set(r["Nl"])
        save = cost(N) - cost(N - drop)
        tot_b += r["B"]
        tot_save += save
        print(f"  {r['commit']:10s} B {r['B']:5.1f}s/{len(r['Bl']):4d}  ran changed instr "
              f"{len(r['Bl']) - len(drop):4d}  droppable {len(drop):4d}  saves {save:5.1f}s"
              + ("  (pre-test hit)" if "*" in hits else "")
              + (f"  FAILING-NOT-HIT {len(fmiss)}/{len(r['Fl'])}" if fmiss else ""))
    print(f"== {name}: B {tot_b:.1f}s, instruction-level saves {tot_save:.1f}s")


if __name__ == "__main__":
    main()
