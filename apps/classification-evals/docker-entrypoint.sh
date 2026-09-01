#!/usr/bin/env bash
set -euo pipefail

: "${SSL_CERT_PEM:?SSL_CERT_PEM is required}"
: "${SSL_KEY_PEM:?SSL_KEY_PEM is required}"

CERT_DIR="$(mktemp -d)"
trap 'rm -rf "$CERT_DIR"' EXIT

printf '%s' "$SSL_CERT_PEM" > "$CERT_DIR/certificate.pem"
printf '%s' "$SSL_KEY_PEM" > "$CERT_DIR/private-key.pem"
chmod 600 "$CERT_DIR/certificate.pem" "$CERT_DIR/private-key.pem"

exec uvicorn backend.app:app \
  --host 0.0.0.0 \
  --port 8443 \
  --ssl-keyfile "$CERT_DIR/private-key.pem" \
  --ssl-certfile "$CERT_DIR/certificate.pem"
