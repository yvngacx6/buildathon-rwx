#!/bin/bash
# usage: label_one.sh <patch> <outdir> <cpuset>   (host side; needs git, docker, $PRISTINE tarball, $IMAGE)
# Applies the patch to a fresh copy of pinned sqlglot and runs the FULL suite in the pinned labeling image.
set -u
P=$1; OUT=$2; CPUS=$3; ID=$(basename "$P" .patch)
PRISTINE=${PRISTINE:-$HOME/label/pristine.tar}; IMAGE=${IMAGE:-br-label}; T=${TEST_TIMEOUT:-120}
W=${WORKROOT:-$HOME/label/work}/$ID
rm -rf "$W"; mkdir -p "$W"; tar -x -C "$W" -f "$PRISTINE"
if [ -s "$P" ] && ! (cd "$W" && git apply "$P" 2> "$OUT/$ID.applyerr"); then echo applyfail > "$OUT/$ID.done"; rm -rf "$W"; exit 0; fi
rm -f "$OUT/$ID.jsonl" "$OUT/$ID.applyerr"
start=$(date +%s)
# -n 2 + thread-method timeout: a hung test kills only its xdist worker (reported as failed), a fresh worker continues.
# --cpuset-cpus (not --cpus): the tests size their spawn pool from os.process_cpu_count(), which follows affinity.
timeout -k 30 ${WALL:-2400} docker run --rm --name "lbl_$ID" --cpuset-cpus "$CPUS" --memory 3g \
  -v "$W":/src -v "$OUT":/out -e OUTCOMES=/out/$ID.jsonl -e PYTHONPATH=/src:/label "$IMAGE" \
  python -m pytest -p outcomes_plugin -p no:cacheprovider -q -rfE -n 2 --continue-on-collection-errors \
  --timeout "$T" --timeout-method=thread --max-worker-restart=100000 tests > "$OUT/$ID.log" 2>&1
rc=$?
docker rm -f "lbl_$ID" >/dev/null 2>&1
echo "rc=$rc secs=$(( $(date +%s) - start ))" > "$OUT/$ID.done"
rm -rf "$W"
