#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Restore a tariff DB snapshot into the eval runner Postgres database.

This is destructive for the target `uk` and `kg` schemas. It refuses to run
unless --confirm-drop is supplied.

Usage:
  restore_tariff_snapshot.sh --target-dsn "$TARIFF_DB_DSN" --snapshot FILE --confirm-drop

Options:
  --target-dsn DSN   Target Postgres DSN. Also read from TARIFF_SNAPSHOT_TARGET_DSN.
  --snapshot FILE    Snapshot dump file. Default: ../var/db-snapshots/latest.dump.
  --confirm-drop     Required. Drops and replaces target uk/kg schemas.
  --skip-label-repair
                     Do not run 007_evidence_labels.sql after restore.
  -h, --help         Show this help.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "$script_dir/.." && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"

target_dsn="${TARIFF_SNAPSHOT_TARGET_DSN:-}"
if [ -z "$target_dsn" ] && [ -n "${POSTGRES_PASSWORD:-}" ]; then
  target_dsn="postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT:-5432}/${POSTGRES_DB:-tariff_db}"
fi
if [ -z "$target_dsn" ]; then
  target_dsn="${TARIFF_DB_DSN:-}"
fi
snapshot_file="${TARIFF_SNAPSHOT_FILE:-$app_dir/var/db-snapshots/latest.dump}"
confirm_drop=0
skip_label_repair=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target-dsn)
      target_dsn="${2:-}"
      shift 2
      ;;
    --snapshot)
      snapshot_file="${2:-}"
      shift 2
      ;;
    --confirm-drop)
      confirm_drop=1
      shift
      ;;
    --skip-label-repair)
      skip_label_repair=1
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

if [ -z "$target_dsn" ]; then
  echo "Missing target DSN. Pass --target-dsn or set TARIFF_SNAPSHOT_TARGET_DSN." >&2
  exit 2
fi

if [ ! -f "$snapshot_file" ]; then
  echo "Snapshot file not found: $snapshot_file" >&2
  exit 2
fi

if [ "$confirm_drop" -ne 1 ]; then
  echo "Refusing to overwrite target schemas without --confirm-drop." >&2
  exit 2
fi

for bin in psql pg_restore; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing required command: $bin" >&2
    exit 127
  fi
done

redact_dsn() {
  printf '%s' "$1" | sed -E 's#(//[^:/@]+):[^@]*@#\1:***@#'
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

echo "Restoring snapshot: $snapshot_file"
echo "Target: $(redact_dsn "$target_dsn")"

psql "$target_dsn" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql "$target_dsn" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
psql "$target_dsn" -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS kg CASCADE; DROP SCHEMA IF EXISTS uk CASCADE;"
pg_restore --no-owner --no-acl --dbname "$target_dsn" "$snapshot_file"

label_repair_sql="$repo_root/apps/product/backend/classification_core/sql/007_evidence_labels.sql"
if [ "$skip_label_repair" -ne 1 ] && [ -f "$label_repair_sql" ]; then
  psql "$target_dsn" -v ON_ERROR_STOP=1 -f "$label_repair_sql"
fi

echo "Restore complete."
echo "[table_counts]"
for table in \
  uk.goods_nomenclatures \
  uk.goods_nomenclature_descriptions \
  uk.measures \
  uk.geographical_areas \
  kg.eval_gold \
  kg.commodity_facets \
  kg.kg_edges \
  kg.kg_edge_commodities \
  kg.classify_runs \
  kg.audit_log; do
  echo "$table=$(safe_count_target "$table")"
done
