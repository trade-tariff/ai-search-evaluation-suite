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

COPY apps/product/backend apps/product/backend
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
