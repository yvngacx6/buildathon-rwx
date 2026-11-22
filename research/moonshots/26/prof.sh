#!/bin/bash
# Usage: prof.sh <git-ref> <tag>  — per-step build profile on a 4-CPU container (run on rwx).
set -u; REF=$1; TAG=$2; D=~/work/p26-$TAG; rm -rf $D
git clone -q https://github.com/yvngacx6/buildathon-rwx.git $D && cd $D && git checkout -q $REF
# pre-stage image: everything up to (not incl.) the big RUN
awk '/^RUN python -m compileall/{exit} {print}' Dockerfile > Dockerfile.pre
T0=$(date +%s.%N)
sg docker -c "DOCKER_BUILDKIT=0 docker build -q --no-cache -f Dockerfile.pre -t p26pre-$TAG ." >/dev/null || { echo PRE FAILED; exit 1; }
echo "pre-stage (fetch+copy): $(python3 -c "import time;print(round(time.time()-$T0,1))") s"
# the big RUN's steps, timed one by one (same order, 4 CPUs)
awk '/^RUN python -m compileall/{f=1} f&&/^CMD/{exit} f{print}' Dockerfile | grep -v '^ *#' | sed 's/^RUN //' > steps.sh
cat > runsteps.py <<'PY'
import subprocess, time, os
src = open('/w/steps.sh').read().replace('\\\n', ' ')
steps = [s.strip() for s in src.split('&&') if s.strip()]
tot = 0
for s in steps:
    t = time.time(); c0 = os.times(); subprocess.run(['bash', '-c', s], check=True, cwd='/app'); c1 = os.times()
    dt = time.time() - t; tot += dt
    cpu = c1.children_user + c1.children_system - c0.children_user - c0.children_system
    print(f"{dt:7.1f}s wall {cpu:7.1f}s cpu  {s[:120]}", flush=True)
print(f"{tot:7.1f}s total")
PY
sg docker -c "docker run --rm --cpus 4 -v $D:/w p26pre-$TAG python /w/runsteps.py"
sg docker -c "docker rmi -f p26pre-$TAG" >/dev/null
