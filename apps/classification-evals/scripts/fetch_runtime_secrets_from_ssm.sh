#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Fetch runtime secrets from AWS SSM Parameter Store or Secrets Manager into a
tmpfs env file.

This avoids storing provider API keys in git or on the persistent EBS volume.
Run it on the EC2 host before docker compose up.

Usage:
  fetch_runtime_secrets_from_ssm.sh --openai-param /path/to/parameter [options]
  fetch_runtime_secrets_from_ssm.sh --openai-secret-id secret-id [options]

Options:
  --openai-param NAME  SSM SecureString parameter containing OPENAI_API_KEY.
  --openai-secret-id ID
                       Secrets Manager secret containing either the raw
                       OPENAI_API_KEY or JSON with an OPENAI_API_KEY field.
  --out-file FILE      Output env file. Default: /run/ai-search-evaluation-suite/secrets.env.
  --region REGION      AWS region. Defaults to AWS_REGION/AWS_DEFAULT_REGION.
  -h, --help           Show this help.
USAGE
}

openai_param="${OPENAI_API_KEY_SSM_PARAM:-}"
openai_secret_id="${OPENAI_API_KEY_SECRET_ID:-}"
out_file="${AI_FAN_OUT_RUNTIME_SECRETS_FILE:-/run/ai-search-evaluation-suite/secrets.env}"
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --openai-param)
      openai_param="${2:-}"
      shift 2
      ;;
    --openai-secret-id)
      openai_secret_id="${2:-}"
      shift 2
      ;;
    --out-file)
      out_file="${2:-}"
      shift 2
      ;;
    --region)
      region="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$openai_param" ] && [ -z "$openai_secret_id" ]; then
  echo "Missing --openai-param/OPENAI_API_KEY_SSM_PARAM or --openai-secret-id/OPENAI_API_KEY_SECRET_ID." >&2
  exit 2
fi

if [ -n "$openai_param" ] && [ -n "$openai_secret_id" ]; then
  echo "Choose either SSM Parameter Store or Secrets Manager, not both." >&2
  exit 2
fi

if [ -z "$region" ]; then
  echo "Missing --region or AWS_REGION/AWS_DEFAULT_REGION." >&2
  exit 2
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "Missing required command: aws" >&2
  exit 127
fi

if [ -n "$openai_param" ]; then
  openai_key="$(
    aws ssm get-parameter \
      --region "$region" \
      --name "$openai_param" \
      --with-decryption \
      --query 'Parameter.Value' \
      --output text
  )"
else
  secret_value="$(
    aws secretsmanager get-secret-value \
      --region "$region" \
      --secret-id "$openai_secret_id" \
      --query 'SecretString' \
      --output text
  )"
  if printf '%s' "$secret_value" | grep -q '"OPENAI_API_KEY"'; then
    openai_key="$(
      SECRET_VALUE="$secret_value" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["SECRET_VALUE"]).get("OPENAI_API_KEY", ""))
PY
    )"
  else
    openai_key="$secret_value"
  fi
fi

if [ -z "$openai_key" ] || [ "$openai_key" = "None" ]; then
  echo "Secret source returned no OpenAI API key." >&2
  exit 1
fi

install -d -m 0700 "$(dirname "$out_file")"
umask 077
{
  echo "# Generated at $(date -u +%Y-%m-%dT%H:%M:%SZ). Stored in /run so it is not persisted on EBS."
  printf 'OPENAI_API_KEY=%s\n' "$openai_key"
} > "$out_file"
chmod 0600 "$out_file"

echo "Runtime secrets written: $out_file"
