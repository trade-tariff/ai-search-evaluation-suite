FROM python:3.12-slim AS worker-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRODUCT_APP_ROOT=/app/apps/product \
    PYTHONPATH=/app/apps/product/backend

WORKDIR /app

COPY apps/classification-evals/requirements-worker.txt apps/classification-evals/requirements-worker.txt
RUN pip install --no-cache-dir -r apps/classification-evals/requirements-worker.txt

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
# Keep empty directories owned by the worker for the existing Compose volumes.
RUN mkdir -p /app/apps/classification-evals/var /app/apps/product/data /app/apps/product/results \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

EXPOSE 8100
USER appuser

CMD ["uvicorn", "backend.worker_app:app", "--app-dir", "/app/apps/classification-evals", "--host", "0.0.0.0", "--port", "8100", "--no-access-log"]

FROM worker-base AS extraction-tools

USER root
ENV CLASSIFY_EVAL_STATE_DIR=/app/apps/classification-evals/var \
    AI_FAN_OUT_KG_LABEL_PROFILE=deployable
COPY apps/product/backend apps/product/backend
COPY apps/product/data apps/product/data
COPY apps/classification-evals apps/classification-evals
RUN pip install --no-cache-dir -r apps/classification-evals/requirements.txt \
    && chown -R appuser:appuser /app
USER appuser

FROM worker-base AS worker
