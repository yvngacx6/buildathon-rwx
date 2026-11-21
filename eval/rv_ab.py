#!/usr/bin/env python3
"""A/B one switch (default BR_RUNVERIFY) in the built image, under the scorer's docker flags.

usage: python3 eval/rv_ab.py IMAGE OUTDIR SETDIR [SETDIR ...] [--reps 3] [--env NAME]

For each set and each value of the switch (0 = off, 1 = on), runs the image `reps` times
(the Makefile's RUN_FLAGS, data mounted read-only) and records stdout, stderr and the
container's CPU (cgroup cpu.stat after the selector exits). Prints, per set: simulated
seconds (as the scorer prices it), misses (labeled sets), median container CPU, and the net
delta on - off with CPU charged at 2x (the scorer's CPU is ~2x slower than EC2), also per
40 commits. Raw runs go to OUTDIR/<set>.<switch>.<rep>.{out,err}.
"""

from __future__ import annotations

import json
import shlex
import statistics
import subprocess
import sys
from pathlib import Path

FLAGS = ("--rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m "
         "--pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL "
         "--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp")
CMD = ("python -S -m selector; rc=$?; "
       "echo CPUSTAT $(grep usage_usec /sys/fs/cgroup/cpu.stat) >&2; exit $rc")


def run(image: str, data: Path, env: dict, out: Path) -> float:
    envs = " ".join(f"--env {k}={v}" for k, v in env.items())
    vols = (f"--volume {data}/commits:/data/commits:ro "
            f"--volume {data}/tests.json:/data/tests.json:ro")
    cmd = f"docker run {FLAGS} {envs} {vols} {image} sh -c {shlex.quote(CMD)}"
    proc = subprocess.run(["sg", "docker", "-c", cmd], capture_output=True, text=True)
    out.with_suffix(".out").write_text(proc.stdout)
    out.with_suffix(".err").write_text(proc.stderr)
    if proc.returncode != 0:
        sys.exit(f"run failed: {out}\n{proc.stderr[-2000:]}")
    for line in proc.stderr.splitlines():
        if line.startswith("CPUSTAT"):
            return int(line.split()[-1]) / 1e6
    return float("nan")


def score(data: Path, stdout: str) -> tuple[float, list, int]:
    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    rows = {json.loads(l)["commit"]: json.loads(l)["workers"] for l in stdout.splitlines() if l}
    answers = {}
    if (data / "answers.json").exists():
        answers = json.loads((data / "answers.json").read_text())["commits"]
    total, misses = 0, []
    commits = sorted(p.stem for p in (data / "commits").glob("*.patch"))
    for c in commits:
        workers = [w for w in rows.get(c, []) if w]
        cost = (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
        chosen = {n for w in workers for n in w}
        if c not in rows or (c in answers and set(answers[c]["failing"]) - chosen):
            misses.append(c)
            cost += 300_000
        total += cost
    return total / 1000, misses, len(commits)


def main() -> None:
    args = sys.argv[1:]
    reps, switch = 3, "BR_RUNVERIFY"
    if "--reps" in args:
        i = args.index("--reps")
        reps = int(args[i + 1])
        del args[i:i + 2]
    if "--env" in args:
        i = args.index("--env")
        switch = args[i + 1]
        del args[i:i + 2]
    image, outdir, sets = args[0], Path(args[1]), [Path(a) for a in args[2:]]
    outdir.mkdir(parents=True, exist_ok=True)
    for data in sets:
        res = {}
        for val in ("0", "1"):
            cpus, sims, miss = [], [], []
            for r in range(reps):
                base = outdir / f"{data.name}.{val}.{r}"
                cpu = run(image, data, {switch: val}, base)
                sim, m, n = score(data, base.with_suffix(".out").read_text())
                cpus.append(cpu)
                sims.append(sim)
                miss.append(len(m))
                if m:
                    print(f"  {data.name} {switch}={val} rep {r}: MISSES {m}", flush=True)
            res[val] = (statistics.median(sims), statistics.median(cpus), max(miss), n)
            print(f"{data.name:14s} {switch}={val}: sim {res[val][0]:8.1f}s  cpu(med) "
                  f"{res[val][1]:6.2f}s  cpus {[round(c, 2) for c in cpus]}  sims "
                  f"{[round(s, 1) for s in sims]}  misses {max(miss)}/{n}", flush=True)
        dsim = res["1"][0] - res["0"][0]
        dcpu = res["1"][1] - res["0"][1]
        n = res["1"][3]
        net = dsim + 2 * dcpu
        print(f"{data.name:14s} delta: sim {dsim:+.1f}s  cpu {dcpu:+.2f}s  net(2x cpu) {net:+.1f}s"
              f"  = {40 * net / n:+.2f}s per 40 commits", flush=True)


if __name__ == "__main__":
    main()
