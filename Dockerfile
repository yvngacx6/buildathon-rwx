# The scorer builds this on linux/amd64 and runs it with no network.
# Base image digest = the image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot source at the pinned commit (read-only reference for diffs and AST checks).
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
# Retries with backoff; the tarball's sha256 is checked (docker/fetch_sqlglot.py): a mismatch
# after every attempt fails the build.
COPY docker/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py ${SQLGLOT_SHA} /opt/sqlglot && rm /tmp/fetch_sqlglot.py

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
#  * the phase lines and errors.py frontier calls of test_max_nodes (selector/slowverify.py)
#  * which test classes probe resources (selector/diffexec.py `_is_resource`)
# The four store builders are independent single-threaded processes (none reads another's
# output): run them side by side on the build's cores (subtest, the long pole, also runs its
# queries in 3 forked workers). Each job's status is checked (a bare
# `wait` would hide a failure).
RUN python -m compileall -q -j 4 "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" \
    /opt/sqlglot/sqlglot /app/selector /app/diffexec \
 && python -m selector.maps build /app/maps /app/index/maps \
 && { PYTHONHASHSEED=0 BR_SUBTEST_JOBS=3 python -m selector.subtest build /opt/sqlglot /app/index/subtest.zip & p1=$!; \
      PYTHONHASHSEED=0 python -m selector.slowverify build /opt/sqlglot /app/index/slowverify.pkl & p2=$!; \
      python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl & p3=$!; \
      python -m selector.diffexec resources /opt/sqlglot /app/index/resources.pkl & p4=$!; \
      wait $p1 && wait $p2 && wait $p3 && wait $p4; } \
 && chmod -R a+rX /app /opt/sqlglot
# -S: no site module (the selector needs only the standard library).
# DIAGNOSTIC r38: drop the 3 TPC tests on commits in these keep categories
ENV BR_DIAG_TPC_DROP=budget
CMD ["python", "-S", "-m", "selector"]
