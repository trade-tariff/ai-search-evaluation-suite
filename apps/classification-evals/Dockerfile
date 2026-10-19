FROM python:3.12-slim AS worker-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRODUCT_APP_ROOT=/app/apps/product \
    CLASSIFY_EVAL_STATE_DIR=/app/apps/classification-evals/var \
    AI_FAN_OUT_KG_LABEL_PROFILE=deployable \
    PYTHONPATH=/app/apps/product/backend

WORKDIR /app

COPY apps/classification-evals/requirements-worker.txt apps/classification-evals/requirements-worker.txt
RUN pip install --no-cache-dir -r apps/classification-evals/requirements-worker.txt

COPY apps/product/backend/_retry.py \
     apps/product/backend/atar.py \
     apps/product/backend/auth.py \
     apps/product/backend/benchmark.py \
     apps/product/backend/commodity_codes.py \
     apps/product/backend/complexity_charts.py \
     apps/product/backend/config.py \
     apps/product/backend/evaluation_ingress.py \
     apps/product/backend/experiment_retrieval.py \
     apps/product/backend/fact_store.py \
     apps/product/backend/intercept_kpis.py \
     apps/product/backend/intercept_retrieval.py \
     apps/product/backend/intercepts.py \
     apps/product/backend/judge.py \
     apps/product/backend/kg.py \
     apps/product/backend/llm_judge.py \
     apps/product/backend/main.py \
     apps/product/backend/prompts.py \
     apps/product/backend/providers.py \
     apps/product/backend/schemas.py \
     apps/product/backend/search.py \
     apps/product/backend/sections.py \
     apps/product/backend/simulator.py \
     apps/product/backend/
COPY apps/product/backend/classification_core/__init__.py \
     apps/product/backend/classification_core/adapter.py \
     apps/product/backend/classification_core/classification.py \
     apps/product/backend/classification_core/classify_matrix_view.py \
     apps/product/backend/classification_core/evidence_labels.py \
     apps/product/backend/classification_core/local_db.py \
     apps/product/backend/classification_core/multi_query.py \
     apps/product/backend/classification_core/provider_guard.py \
     apps/product/backend/classification_core/qa_loop.py \
     apps/product/backend/classification_core/run_classify_matrix.py \
     apps/product/backend/classification_core/run_eval.py \
     apps/product/backend/classification_core/run_hydrated_e2e_matrix.py \
     apps/product/backend/classification_core/run_qna_mode_comparison.py \
     apps/product/backend/classification_core/session_facts.py \
     apps/product/backend/classification_core/trader_simulator.py \
     apps/product/backend/classification_core/triage.py \
     apps/product/backend/classification_core/
COPY apps/product/backend/classification_core/trade_tariff_backend/__init__.py \
     apps/product/backend/classification_core/trade_tariff_backend/cli.py \
     apps/product/backend/classification_core/trade_tariff_backend/client.py \
     apps/product/backend/classification_core/trade_tariff_backend/execute_run.py \
     apps/product/backend/classification_core/trade_tariff_backend/qa_loop.py \
     apps/product/backend/classification_core/trade_tariff_backend/
COPY apps/product/backend/classification_core/data/commodities.json \
     apps/product/backend/classification_core/data/countries.json \
     apps/product/backend/classification_core/data/facets.json \
     apps/product/backend/classification_core/data/kg_edges.json \
     apps/product/backend/classification_core/data/
COPY apps/product/data apps/product/data
COPY apps/classification-evals apps/classification-evals
RUN mkdir -p /app/apps/classification-evals/var /app/apps/product/results \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

WORKDIR /app
EXPOSE 8100
USER appuser

CMD ["uvicorn", "backend.worker_app:app", "--app-dir", "/app/apps/classification-evals", "--host", "0.0.0.0", "--port", "8100", "--no-access-log"]

FROM worker-base AS extraction-tools

USER root
COPY apps/product/backend apps/product/backend
COPY apps/product/data apps/product/data
COPY apps/classification-evals apps/classification-evals
RUN pip install --no-cache-dir -r apps/classification-evals/requirements.txt \
    && chown -R appuser:appuser /app
USER appuser

FROM worker-base AS worker
