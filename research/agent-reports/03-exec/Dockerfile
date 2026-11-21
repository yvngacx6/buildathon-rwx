FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
RUN python -c "import urllib.request,tarfile,io; d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(d)).extractall('/opt')" && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot && chmod -R a+rX /opt/sqlglot
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests; chmod -R a+rX /opt/sqlglot
ENV PYTHONPATH=/opt/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/sqlglot
COPY *.py map.pkl cpu.json /app/
RUN chmod -R a+rX /app
CMD ["python", "/app/run.py"]
