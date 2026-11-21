#!/usr/bin/env python3
"""Label many patches in parallel: one container per patch, each pinned to its own pair of CPUs.

usage: run_queue.py <patch_dir> <out_dir> [slots]     (resumable: patches with <out>/<id>.done are skipped)
"""
import os, queue, subprocess, sys, threading
from pathlib import Path

pdir, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
slots = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count() // 2
out.mkdir(parents=True, exist_ok=True)
order = pdir / "ORDER"  # optional: labeling order, one id per line
ids = order.read_text().split() if order.exists() else sorted(p.stem for p in pdir.glob("*.patch"))
q = queue.Queue()
for i in ids:
    if not (out / f"{i}.done").exists():
        q.put(i)
print(f"{q.qsize()} to label with {slots} slots", flush=True)
here = Path(__file__).resolve().parent

def worker(slot):
    cpus = f"{2 * slot},{2 * slot + 1}"
    while True:
        try:
            i = q.get_nowait()
        except queue.Empty:
            return
        subprocess.run([str(here / "label_one.sh"), str(pdir / f"{i}.patch"), str(out), cpus])
        print(i, (out / f"{i}.done").read_text().strip(), flush=True)

ts = [threading.Thread(target=worker, args=(s,)) for s in range(slots)]
[t.start() for t in ts]
[t.join() for t in ts]
