# The scorer builds this on linux/amd64 and runs it with no network.
# Base image digest = the image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot source at the pinned commit (read-only reference for diffs and AST checks).
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot

# The labelers' environment, for selector/verify.py (runs cheap selections against the patch).
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# Bytecode for the pinned tests, so each verification imports them from .pyc: plain compileall,
# plus pytest's assertion-rewritten test modules (a collect-only pass writes them).
RUN cd /opt/sqlglot && python -m compileall -q -j 4 tests \
 && SKIP_INTEGRATION=0 python -m pytest --collect-only -q -p no:cacheprovider tests > /dev/null

COPY selector /app/selector
COPY maps /app/maps

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
RUN python -m compileall -q -j 4 "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" \
    /opt/sqlglot/sqlglot /app/selector \
 && python -m selector.maps build /app/maps /app/index/maps \
 && python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl \
 && chmod -R a+rX /app /opt/sqlglot
# -S: no site module (the selector needs only the standard library). selector/verify.py starts
# its pytest server with plain `python` (site on), so pytest and requirements.lock import there.
CMD ["python", "-S", "-m", "selector"]
