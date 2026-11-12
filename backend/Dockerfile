# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Multi-stage build:
#   build   → bağımlılıklar venv'e kurulur (derleyici vb. son imaja girmez)
#   runtime → slim imaj, root olmayan kullanıcı, read-only root FS uyumlu
#   test    → runtime + pytest (CI ve `make test` için)
# Aynı imaj üç rolde çalışır: API (varsayılan CMD), worker, migration.
# ---------------------------------------------------------------------------
FROM python:3.12.8-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.12.8-slim AS runtime
ARG APP_VERSION=0.1.0
LABEL org.opencontainers.image.title="veon-sample-backend" \
      org.opencontainers.image.version="${APP_VERSION}"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION}
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
USER 10001:10001
EXPOSE 8000 9100
# Pod başına tek process; ölçek replika ile. Graceful shutdown 20 sn.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--timeout-graceful-shutdown", "20"]

FROM runtime AS test
USER root
COPY requirements.txt requirements-dev.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements-dev.txt
COPY setup.cfg ./
COPY tests ./tests
USER 10001:10001
CMD ["pytest"]
