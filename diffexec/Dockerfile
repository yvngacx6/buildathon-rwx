# I12 differential execution: image for measurements (build from the repo root:
#   docker build -f diffexec/Dockerfile --build-arg STORE=<dir under repo> -t blast-diffexec .)
#
# STORE=<dir>: copy a store built elsewhere (measurement only: a store recorded with
#   DIFFEXEC_ONLY from labeled patches must NEVER ship, it is derived from the labels).
# RECORD=1: record the whole pinned suite during the build (the shippable form). Measured on a
#   20-core laptop at 5,483 CPU-s with strict caps: it does not fit the 10-minute, 4-core build.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot

COPY selector /app/selector
COPY maps /app/maps
COPY diffexec /app/diffexec

ARG RECORD=0
COPY requirements.lock /tmp/requirements.lock
RUN if [ "$RECORD" = "1" ]; then \
      pip install --no-cache-dir -r /tmp/requirements.lock \
      && mkdir -p /tmp/rec && cd /opt/sqlglot \
      && PYTHONHASHSEED=0 SKIP_INTEGRATION=0 DIFFEXEC_OUT=/tmp/rec PYTHONPATH=/app \
         python -m pytest -q -p diffexec.record -p no:cacheprovider -n 4 --dist loadfile tests \
      && cd /app && python -m diffexec.build /tmp/rec /app/diffexec_store && rm -rf /tmp/rec; \
    fi

ARG STORE=diffexec/empty_store
COPY ${STORE} /app/diffexec_store
RUN chmod -R a+rX /app /opt/sqlglot

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "selector"]
