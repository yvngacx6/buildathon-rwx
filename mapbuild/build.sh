#!/usr/bin/env bash
# Rebuild maps/ from sqlglot at the pinned commit, in the pinned image.
#   usage: mapbuild/build.sh [work-dir]      (run from anywhere; needs Docker, ~4 cores)
# Writes maps/{lines,classes,reads,test_imports}.json.gz and maps/MANIFEST.json.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=${1:-${TMPDIR:-/tmp}/blast-mapbuild-work}   # per-test traces (~100 MB), outside the repo
rm -rf "$WORK" && mkdir -p "$WORK"

docker build -q -f "$REPO/mapbuild/Dockerfile" -t blast-mapbuild "$REPO" >/dev/null

start=$(date +%s)
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$REPO:/repo" -v "$WORK:/work" \
  blast-mapbuild \
  python /mapbuild/run_tracing.py --tests /repo/sample/tests.json --work /work --out /repo/maps
echo "map build wall time: $(( $(date +%s) - start ))s" >&2
du -ch "$REPO"/maps/*.gz | tail -1 >&2
