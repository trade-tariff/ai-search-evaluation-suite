FROM node:20-slim AS frontend-build

WORKDIR /srv/ai-search-evaluation-suite/apps/product/frontend
COPY apps/product/frontend/package.json apps/product/frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/product/frontend ./
RUN npm run build


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRODUCT_APP_ROOT=/srv/ai-search-evaluation-suite/apps/product \
    CLASSIFY_EVAL_STATE_DIR=/srv/ai-search-evaluation-suite/apps/classification-evals/var \
    AI_FAN_OUT_KG_LABEL_PROFILE=deployable \
    PYTHONPATH=/srv/ai-search-evaluation-suite/apps/product/backend

WORKDIR /srv/ai-search-evaluation-suite

COPY apps/product/backend/requirements.txt apps/product/backend/requirements.txt
COPY apps/classification-evals/requirements.txt apps/classification-evals/requirements.txt
RUN pip install --no-cache-dir -r apps/classification-evals/requirements.txt

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
COPY apps/product/backend/classification_core/data/commodities.json \
     apps/product/backend/classification_core/data/countries.json \
     apps/product/backend/classification_core/data/facets.json \
     apps/product/backend/classification_core/data/kg_edges.json \
     apps/product/backend/classification_core/data/
COPY apps/product/data apps/product/data
COPY --from=frontend-build /srv/ai-search-evaluation-suite/apps/product/frontend/dist apps/product/frontend/dist
COPY apps/classification-evals apps/classification-evals
RUN mkdir -p /srv/ai-search-evaluation-suite/apps/classification-evals/var /srv/ai-search-evaluation-suite/apps/product/results \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv/ai-search-evaluation-suite

WORKDIR /srv/ai-search-evaluation-suite/apps/classification-evals
EXPOSE 8100
USER appuser

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8100"]
