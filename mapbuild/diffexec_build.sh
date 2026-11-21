#!/usr/bin/env bash
# Rebuild maps/diffexec.zip (differential-execution store, idea I12) in the pinned image.
#   usage: mapbuild/diffexec_build.sh [work-dir] [jobs]   (needs Docker; ~2.5 CPU-h, ~10 min on 32 cores)
# Reads only sample/tests.json (the test file list). Writes maps/diffexec.zip + MANIFEST "diffexec".
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=${1:-${TMPDIR:-/tmp}/blast-diffexec-work}   # per-file recordings (a few GB), outside the repo
JOBS=${2:-$(nproc)}
rm -rf "$WORK" && mkdir -p "$WORK"

docker build -q -f "$REPO/mapbuild/Dockerfile" -t blast-mapbuild "$REPO" >/dev/null

start=$(date +%s)
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$REPO:/repo" -v "$WORK:/work" \
  blast-mapbuild \
  python /repo/mapbuild/diffexec_run.py --tests /repo/sample/tests.json --work /work \
    --out /repo/maps/diffexec.zip -j "$JOBS"
echo "diffexec store build wall time: $(( $(date +%s) - start ))s" >&2
ls -l "$REPO/maps/diffexec.zip" >&2
