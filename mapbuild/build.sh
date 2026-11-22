#!/usr/bin/env bash
# Rebuild maps/ from sqlglot at the pinned commit, in the pinned image.
#   usage: mapbuild/build.sh [work-dir]      (run from anywhere; needs Docker, ~4 cores)
# Writes maps/{lines,classes,reads,test_imports,attrs}.json.gz and maps/MANIFEST.json.
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
# Dialect reach (static, pinned source): a dialect's users also use its parser/generator/tokenizer
# classes and their bases (mapbuild/dialect_reach.py). Run after classes_run.json.gz exists.
docker run --rm --user "$(id -u):$(id -g)" -v "$REPO:/repo" blast-mapbuild \
  python /mapbuild/dialect_reach.py /opt/sqlglot /repo/maps
# Attribute-lookup map (moon-19 / R31): which class attributes each test looks up, one
# interpreter per test with recording descriptors (mapbuild/trace_attrs.py) -> maps/attrs.json.gz.
# Its log must end "0 failed, 0 replaced".
docker run --rm --user "$(id -u):$(id -g)" -v "$REPO:/repo" -v "$WORK:/work" blast-mapbuild \
  python /mapbuild/attrs_map.py --tests /repo/sample/tests.json --work /work --out /repo/maps
echo "map build wall time: $(( $(date +%s) - start ))s" >&2
du -ch "$REPO"/maps/*.gz | tail -1 >&2
