FROM python:3.12-alpine AS python-build

RUN apk add --no-cache build-base

ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv

WORKDIR /app

COPY apps/classification-evals/requirements-worker.txt apps/classification-evals/requirements-worker.txt
RUN pip install --no-cache-dir -r apps/classification-evals/requirements-worker.txt


FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRODUCT_APP_ROOT=/app/apps/product \
    PYTHONPATH=/app/apps/product/backend \
    PATH="/opt/venv/bin:$PATH"

RUN apk add --no-cache bash curl libgcc libstdc++

# CVE-2026-53612/53613/53614/76642/78408/78409/78410: bump libuuid past the
# vulnerable 2.42.1-r0 pulled in transitively by the base image.
RUN apk add --no-cache --upgrade libuuid

COPY --from=python-build /opt/venv /opt/venv

WORKDIR /app

COPY apps/product/backend/evaluation_ingress.py apps/product/backend/
COPY apps/product/backend/classification_core/__init__.py \
     apps/product/backend/classification_core/provider_guard.py \
     apps/product/backend/classification_core/trader_simulator.py \
     apps/product/backend/classification_core/
COPY apps/product/backend/classification_core/trade_tariff_backend \
     apps/product/backend/classification_core/trade_tariff_backend
COPY apps/classification-evals/backend/auth.py \
     apps/classification-evals/backend/logging_config.py \
     apps/classification-evals/backend/worker_app.py \
     apps/classification-evals/backend/
COPY apps/classification-evals/docker-entrypoint.sh apps/classification-evals/
# Keep empty directories owned by the worker for the existing Compose volumes.
RUN mkdir -p /app/apps/classification-evals/var /app/apps/product/data /app/apps/product/results \
    && addgroup -S tariff && adduser -S tariff -G tariff \
    && chown -R tariff:tariff /app

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["curl", "-fsS", "--insecure", "https://127.0.0.1:8443/api/health"]

WORKDIR /app
EXPOSE 8443
USER tariff

CMD ["/app/apps/classification-evals/docker-entrypoint.sh"]
