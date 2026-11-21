# Map-building image: the exact environment the organizers labeled in.
# Build from the repo root:  docker build -f mapbuild/Dockerfile -t blast-mapbuild .
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# sqlglot at the pinned commit (same fetch as the submission Dockerfile).
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io,tarfile,urllib.request; \
data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot

COPY mapbuild /mapbuild

ENV PYTHONPATH=/opt/sqlglot \
    SKIP_INTEGRATION=0 \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/sqlglot
