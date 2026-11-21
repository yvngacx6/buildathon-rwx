#!/bin/bash
# Build maps/runverify.json.gz inside the built image, under the scorer's docker flags
# (per-test CPU of the run-time verification harness, measured where it will run).
# usage: eval/rv_buildmap.sh IMAGE [REPS]   (run from the repo root; needs docker via sg)
set -e
IMAGE=$1; REPS=${2:-2}
OUT=$(mktemp -d); chmod 777 "$OUT"
sg docker -c "docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m \
  --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume $PWD/sample/tests.json:/data/tests.json:ro --volume $OUT:/out $IMAGE \
  python -S -m selector.runverify build /opt/sqlglot /data/tests.json /out/runverify.json.gz $REPS"
cp "$OUT/runverify.json.gz" maps/runverify.json.gz
rm -rf "$OUT"
