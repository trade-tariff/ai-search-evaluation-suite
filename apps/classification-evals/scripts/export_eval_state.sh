#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Export eval outputs and runner logs before stopping or terminating the EC2 host.

This is a safety export, not the primary database snapshot. It preserves:
  - kg.classify_runs / kg.eval_runs / kg.eval_run_results / kg.exp6_qa_runs
  - runner jobs.sqlite and var/jobs/*.log

Usage:
  export_eval_state.sh [options]

Options:
  --target-dsn DSN   Postgres DSN. Also read from TARIFF_SNAPSHOT_TARGET_DSN or
                     derived from POSTGRES_* env vars.
  --state-dir DIR    Runner state directory. Default: ../var.
  --out-dir DIR      Export directory. Default: <state-dir>/teardown-exports.
  --name NAME        Export base name. Default: eval_state_<UTC timestamp>.
  --skip-db          Export runner logs/status only.
  -h, --help         Show this help.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "$script_dir/.." && pwd)"

target_dsn="${TARIFF_SNAPSHOT_TARGET_DSN:-}"
if [ -z "$target_dsn" ] && [ -n "${POSTGRES_PASSWORD:-}" ]; then
  target_dsn="postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT:-5432}/${POSTGRES_DB:-tariff_db}"
fi
if [ -z "$target_dsn" ]; then
  target_dsn="${TARIFF_DB_DSN:-}"
fi

state_dir="${CLASSIFY_EVAL_STATE_DIR:-$app_dir/var}"
out_dir="${CLASSIFY_EVAL_EXPORT_DIR:-$state_dir/teardown-exports}"
export_name="${CLASSIFY_EVAL_EXPORT_NAME:-eval_state_$(date -u +%Y%m%dT%H%M%SZ)}"
skip_db=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target-dsn)
      target_dsn="${2:-}"
      shift 2
      ;;
    --state-dir)
      state_dir="${2:-}"
      shift 2
      ;;
    --out-dir)
      out_dir="${2:-}"
      shift 2
      ;;
    --name)
      export_name="${2:-}"
      shift 2
      ;;
    --skip-db)
      skip_db=1
      shift
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

mkdir -p "$out_dir"

redact_dsn() {
  printf '%s' "$1" | sed -E 's#(//[^:/@]+):[^@]*@#\1:***@#'
}

sha256_file() {
  if [ ! -f "$1" ]; then
    printf 'missing'
    return
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

safe_count_target() {
  local table="$1"
  local exists
  exists="$(psql "$target_dsn" -XAtq -v ON_ERROR_STOP=1 -c "SELECT to_regclass('$table') IS NOT NULL" 2>/dev/null || true)"
  if [ "$exists" = "t" ]; then
    psql "$target_dsn" -XAtq -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $table" 2>/dev/null || printf 'error'
  else
    printf 'missing'
  fi
}

eval_dump="$out_dir/$export_name.eval-runs.dump"
runner_tar="$out_dir/$export_name.runner-state.tgz"
manifest="$out_dir/$export_name.manifest.txt"

if [ "$skip_db" -ne 1 ]; then
  if [ -z "$target_dsn" ]; then
    echo "Missing target DSN. Pass --target-dsn, set TARIFF_SNAPSHOT_TARGET_DSN, or set POSTGRES_* env vars." >&2
    exit 2
  fi
  for bin in pg_dump psql; do
    if ! command -v "$bin" >/dev/null 2>&1; then
      echo "Missing required command: $bin" >&2
      exit 127
    fi
  done
  pg_dump \
    --format=custom \
    --data-only \
    --no-owner \
    --no-acl \
    --table=kg.classify_runs \
    --table=kg.eval_runs \
    --table=kg.eval_run_results \
    --table=kg.exp6_qa_runs \
    --file "$eval_dump" \
    "$target_dsn"
fi

tar_items=()
if [ -f "$state_dir/jobs.sqlite" ]; then
  tar_items+=("jobs.sqlite")
fi
if [ -d "$state_dir/jobs" ]; then
  tar_items+=("jobs")
fi

if [ "${#tar_items[@]}" -gt 0 ]; then
  (cd "$state_dir" && tar -czf "$runner_tar" "${tar_items[@]}")
fi

{
  echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "target_dsn=$(redact_dsn "$target_dsn")"
  echo "state_dir=$state_dir"
  echo "skip_db=$skip_db"
  echo "eval_dump=$(basename "$eval_dump")"
  echo "eval_dump_sha256=$(sha256_file "$eval_dump")"
  echo "runner_state=$(basename "$runner_tar")"
  echo "runner_state_sha256=$(sha256_file "$runner_tar")"
  echo
  echo "[table_counts]"
  if [ "$skip_db" -eq 1 ]; then
    echo "db=skipped"
  else
    for table in \
      kg.classify_runs \
      kg.eval_runs \
      kg.eval_run_results \
      kg.exp6_qa_runs; do
      echo "$table=$(safe_count_target "$table")"
    done
  fi
} > "$manifest"

ln -sfn "$(basename "$manifest")" "$out_dir/latest.manifest.txt"
if [ -f "$eval_dump" ]; then
  ln -sfn "$(basename "$eval_dump")" "$out_dir/latest.eval-runs.dump"
fi
if [ -f "$runner_tar" ]; then
  ln -sfn "$(basename "$runner_tar")" "$out_dir/latest.runner-state.tgz"
fi

echo "Eval state export written under: $out_dir"
echo "Manifest: $manifest"
