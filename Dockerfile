FROM node:24-alpine AS frontend-build

WORKDIR /srv/ai-search-evaluation-suite/apps/product/frontend
COPY apps/product/frontend/package.json apps/product/frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/product/frontend ./
RUN npm run build


FROM python:3.12-alpine AS python-build

RUN apk add --no-cache build-base

ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv

WORKDIR /srv/ai-search-evaluation-suite

COPY apps/product/backend/requirements.txt apps/product/backend/requirements.txt
COPY apps/classification-evals/requirements.txt apps/classification-evals/requirements.txt
RUN pip install --no-cache-dir -r apps/classification-evals/requirements.txt


FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRODUCT_APP_ROOT=/srv/ai-search-evaluation-suite/apps/product \
    CLASSIFY_EVAL_STATE_DIR=/srv/ai-search-evaluation-suite/apps/classification-evals/var \
    AI_FAN_OUT_KG_LABEL_PROFILE=deployable \
    PYTHONPATH=/srv/ai-search-evaluation-suite/apps/product/backend \
    PATH="/opt/venv/bin:$PATH"

RUN apk add --no-cache bash

COPY --from=python-build /opt/venv /opt/venv

WORKDIR /srv/ai-search-evaluation-suite

COPY apps/product/backend/_retry.py \
     apps/product/backend/atar.py \
     apps/product/backend/auth.py \
     apps/product/backend/benchmark.py \
     apps/product/backend/commodity_codes.py \
     apps/product/backend/complexity_charts.py \
     apps/product/backend/config.py \
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
COPY --from=frontend-build /srv/ai-search-evaluation-suite/apps/product/frontend/dist apps/product/frontend/dist
COPY apps/classification-evals apps/classification-evals
RUN mkdir -p /srv/ai-search-evaluation-suite/apps/classification-evals/var /srv/ai-search-evaluation-suite/apps/product/results \
    && addgroup -S tariff && adduser -S tariff -G tariff \
    && chown -R tariff:tariff /srv/ai-search-evaluation-suite

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/api/health', context=ssl._create_unverified_context()).read()"]

WORKDIR /srv/ai-search-evaluation-suite/apps/classification-evals
EXPOSE 8443
USER tariff

RUN chmod +x docker-entrypoint.sh

CMD ["./docker-entrypoint.sh"]
