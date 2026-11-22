#!/bin/bash
# Usage: btime.sh <git-ref> [<ref> ...] — final_check C4-style clean build time per ref (run on rwx).
for REF in "$@"; do
  D=$(mktemp -d); git clone -q https://github.com/yvngacx6/buildathon-rwx.git $D/src && cd $D/src && git checkout -q $REF
  L=$(cut -d' ' -f1-3 /proc/loadavg); B0=$(date +%s.%N)
  sg docker -c "DOCKER_BUILDKIT=0 timeout 600 docker build --no-cache --platform linux/amd64 --cpu-period 100000 --cpu-quota 400000 --memory 8g --memory-swap 8g -t bt26 ." > $D/build.log 2>&1; rc=$?
  echo "$REF $(git rev-parse --short HEAD) build $(python3 -c "import time;print(round(time.time()-$B0,1))")s rc=$rc load0=$L size=$(git archive HEAD | wc -c)"
  cd ~; rm -rf $D
done
