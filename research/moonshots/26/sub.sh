#!/bin/bash
# Usage: sub.sh <git-ref> — the subtest store build alone, timestamped stderr, 4 CPUs (run on rwx).
set -u; REF=$1; JOBS=${2:-1}; D=~/work/s26-$JOBS; rm -rf $D
git clone -q https://github.com/yvngacx6/buildathon-rwx.git $D && cd $D && git checkout -q $REF
awk '/^RUN python -m compileall/{exit} {print}' Dockerfile > Dockerfile.pre
sg docker -c "DOCKER_BUILDKIT=0 docker build -q -f Dockerfile.pre -t s26pre ." >/dev/null
cat > $D/in.sh <<'EOS'
cd /app; JOBS=$1
python -m compileall -q -j 4 /opt/sqlglot/sqlglot /app/selector /usr/local/lib/python3.13 >/dev/null 2>&1
PYTHONHASHSEED=0 BR_SUBTEST_JOBS=$JOBS python -m selector.subtest build /opt/sqlglot /w/s$JOBS.zip 2>&1 | python -u /w/ts.py
ls -la /w/s$JOBS.zip
EOS
cat > $D/ts.py <<'EOS'
import sys, time
t = time.time()
for l in sys.stdin:
    print(f"{time.time() - t:6.1f} {l}", end="", flush=True)
EOS
sg docker -c "docker run --rm --cpus 4 -v $D:/w s26pre bash /w/in.sh $JOBS"
