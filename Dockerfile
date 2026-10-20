FROM python:3.12-alpine AS python-build

RUN apk add --no-cache build-base

ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .


FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apk add --no-cache bash curl libgcc libstdc++

# CVE-2026-53612/53613/53614/76642/78408/78409/78410: bump libuuid past the
# vulnerable 2.42.1-r0 pulled in transitively by the base image.
RUN apk add --no-cache --upgrade libuuid

COPY --from=python-build /opt/venv /opt/venv
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN addgroup -S tariff && adduser -S tariff -G tariff \
    && chown -R tariff:tariff /app

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["curl", "-fsS", "--insecure", "https://127.0.0.1:8443/api/health"]

WORKDIR /app
EXPOSE 8443
USER tariff

CMD ["/app/docker-entrypoint.sh"]
