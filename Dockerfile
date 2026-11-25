# syntax=docker/dockerfile:1
# Two stages: dependencies are installed from the hashed lock file, then only the virtualenv is copied
# into a clean image that runs as a non-root user with nothing to write to.

FROM python:3.13-slim AS build
WORKDIR /src
COPY requirements.lock pyproject.toml README.md ./
COPY themis ./themis
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock \
 && /venv/bin/pip install --no-cache-dir --no-deps .

FROM python:3.13-slim
# 65532 is the user the Kubernetes manifests run as (deploy/kubernetes/workload.yaml)
RUN useradd --uid 65532 --no-create-home --shell /usr/sbin/nologin themis
COPY --from=build /venv /venv
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532
EXPOSE 8600
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/healthz', timeout=2)"]
ENTRYPOINT ["themis"]
# All interfaces: this runs in a pod and is reached through the Service. THEMIS_DB_URL must point at Postgres.
CMD ["serve", "--host", "0.0.0.0", "--port", "8600"]
