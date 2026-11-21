# Labeling image: the organizers' base image + requirements.lock. sqlglot (patched copy) is mounted at /src.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /label/requirements.lock
RUN pip install --no-cache-dir -r /label/requirements.lock
COPY outcomes_plugin.py /label/
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
WORKDIR /src
