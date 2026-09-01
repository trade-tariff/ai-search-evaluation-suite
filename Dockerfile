ARG PYTHON_VERSION=3.13
ARG ALPINE_VERSION=3.22

FROM python:${PYTHON_VERSION}-alpine${ALPINE_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_PORT=8443

WORKDIR /app

COPY apps/deployment /app/deployment

# This image only ever runs deployment.app (stdlib-only), so pip is unused at
# runtime. Removing it drops its vendored msgpack/setuptools copies, which
# Trivy flags via pip's own vendor.txt even though nothing here calls pip.
RUN apk upgrade --no-cache libcrypto3 libssl3 && \
    rm -rf /usr/local/lib/python3.*/site-packages/pip* \
           /usr/local/lib/python3.*/ensurepip

RUN addgroup -S tariff \
    && adduser -S -D -H -h /app -G tariff tariff \
    && chown -R tariff:tariff /app

EXPOSE 8443

USER tariff

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/healthcheckz', context=ssl._create_unverified_context()).read()"]

CMD ["python", "-m", "deployment.app"]
