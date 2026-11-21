# The scorer builds this on linux/amd64 and runs it with no network.
# Base image digest = the image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot source at the pinned commit (read-only reference for diffs and AST checks).
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot

COPY selector /app/selector
COPY maps /app/maps
# Differential-execution replay (I12); its store is maps/diffexec.zip.
COPY diffexec /app/diffexec

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
# The build is not charged, the run's CPU is: do everything that depends only on the pinned
# source and the maps here, so the run only reads what a commit needs.
#  * bytecode for the standard library (the base image ships without it), the selector and
#    sqlglot: else the run, and its patched-import subprocess, compile every module they
#    import, every time (PYTHONDONTWRITEBYTECODE only stops writing the cache)
#  * the maps as per-file marshal shards (selector/maps.py)
#  * the static name scan for name-reader narrowing (selector/nameindex.py), plus the
#    import-time state snapshot (selector/state.py) next to it
#  * the per-query line coverage and pinned outputs of the TPC executor tests
#    (selector/subtest.py, ~30 s)
RUN python -m compileall -q -j 4 "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" \
    /opt/sqlglot/sqlglot /app/selector /app/diffexec \
 && python -m selector.maps build /app/maps /app/index/maps \
 && python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl \
 && PYTHONHASHSEED=0 python -m selector.subtest build /opt/sqlglot /app/index/subtest.zip \
 && chmod -R a+rX /app /opt/sqlglot
# -S: no site module (the selector needs only the standard library).
CMD ["python", "-S", "-m", "selector"]
