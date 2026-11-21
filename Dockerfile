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
# Bytecode for the pinned tree, so each verification imports sqlglot/tests from .pyc: plain
# compileall, plus pytest's assertion-rewritten test modules (a collect-only pass writes them).
RUN cd /opt/sqlglot && python -m compileall -q sqlglot tests \
 && SKIP_INTEGRATION=0 python -m pytest --collect-only -q -p no:cacheprovider tests > /dev/null

COPY selector /app/selector
COPY maps /app/maps

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
# Build-time static scan of the pinned source for name-reader narrowing (selector/nameindex.py).
# The build is not charged; the run only unpickles what a commit needs.
RUN python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl \
 && chmod -R a+rX /app /opt/sqlglot
CMD ["python", "-m", "selector"]
