#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Create a portable tariff DB snapshot for the classification eval runner.

The snapshot is read-only against the source DB. It dumps the `uk` and `kg`
schemas as a custom-format pg_dump file. By default it excludes kg.audit_log and
previous eval/classification run rows.

Usage:
  dump_tariff_snapshot.sh --source-dsn "$TARIFF_DB_DSN" [options]

Options:
  --source-dsn DSN   Source Postgres DSN. Also read from TARIFF_SNAPSHOT_SOURCE_DSN.
  --out-dir DIR      Output directory. Default: ../var/db-snapshots.
  --name NAME        Snapshot base name. Default: tariff_snapshot_<UTC timestamp>.
  --schemas LIST     Comma-separated schemas. Default: uk,kg.
  --include-run-history
                     Include previous eval/classification result rows.
  --yes              Skip the operator confirmation prompt.
  -h, --help         Show this help.

Environment:
  TARIFF_SNAPSHOT_EXCLUDE_TABLE_DATA
      Comma-separated table patterns to exclude in addition to default
      audit/run-history exclusions.
USAGE
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "$script_dir/.." && pwd)"

source_dsn="${TARIFF_SNAPSHOT_SOURCE_DSN:-${TARIFF_DB_DSN:-}}"
out_dir="${TARIFF_SNAPSHOT_DIR:-$app_dir/var/db-snapshots}"
snapshot_name="${TARIFF_SNAPSHOT_NAME:-tariff_snapshot_$(date -u +%Y%m%dT%H%M%SZ)}"
schemas="${TARIFF_SNAPSHOT_SCHEMAS:-uk,kg}"
assume_yes=0
include_run_history="${TARIFF_SNAPSHOT_INCLUDE_RUN_HISTORY:-0}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-dsn)
      source_dsn="${2:-}"
      shift 2
      ;;
    --out-dir)
      out_dir="${2:-}"
      shift 2
      ;;
    --name)
      snapshot_name="${2:-}"
      shift 2
      ;;
    --schemas)
      schemas="${2:-}"
      shift 2
      ;;
    --include-run-history)
      include_run_history=1
      shift
      ;;
    --yes)
      assume_yes=1
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

if [ -z "$source_dsn" ]; then
  echo "Missing source DSN. Pass --source-dsn or set TARIFF_SNAPSHOT_SOURCE_DSN." >&2
  exit 2
fi

for bin in pg_dump psql; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing required command: $bin" >&2
    exit 127
  fi
done

redact_dsn() {
  printf '%s' "$1" | sed -E 's#(//[^:/@]+):[^@]*@#\1:***@#'
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

safe_count() {
  local table="$1"
  local exists
  exists="$(psql "$source_dsn" -XAtq -v ON_ERROR_STOP=1 -c "SELECT to_regclass('$table') IS NOT NULL" 2>/dev/null || true)"
  if [ "$exists" = "t" ]; then
    psql "$source_dsn" -XAtq -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $table" 2>/dev/null || printf 'error'
  else
    printf 'missing'
  fi
}

mkdir -p "$out_dir"
dump_path="$out_dir/$snapshot_name.dump"
manifest_path="$out_dir/$snapshot_name.manifest.txt"

if [ -e "$dump_path" ]; then
  echo "Refusing to overwrite existing snapshot: $dump_path" >&2
  exit 2
fi

if [ "$assume_yes" -ne 1 ]; then
  echo "About to create a DB snapshot from: $(redact_dsn "$source_dsn")"
  echo "Schemas: $schemas"
  echo "Output: $dump_path"
  printf 'Continue? Type "snapshot" to proceed: '
  read -r confirmation
  if [ "$confirmation" != "snapshot" ]; then
    echo "Aborted."
    exit 1
  fi
fi

IFS=',' read -r -a schema_list <<< "$schemas"
exclude_table_data=("kg.audit_log")
if [ "$include_run_history" != "1" ]; then
  exclude_table_data+=(
    "kg.classify_runs"
    "kg.eval_runs"
    "kg.eval_run_results"
    "kg.exp6_qa_runs"
  )
fi
if [ -n "${TARIFF_SNAPSHOT_EXCLUDE_TABLE_DATA:-}" ]; then
  IFS=',' read -r -a extra_excludes <<< "$TARIFF_SNAPSHOT_EXCLUDE_TABLE_DATA"
  for table_pattern in "${extra_excludes[@]}"; do
    if [ -n "$table_pattern" ]; then
      exclude_table_data+=("$table_pattern")
    fi
  done
fi

pg_dump_args=(
  --format=custom
  --compress=9
  --no-owner
  --no-acl
  --file "$dump_path"
)

for schema in "${schema_list[@]}"; do
  if [ -n "$schema" ]; then
    pg_dump_args+=(--schema "$schema")
  fi
done

for table_pattern in "${exclude_table_data[@]}"; do
  pg_dump_args+=(--exclude-table-data "$table_pattern")
done

pg_dump "${pg_dump_args[@]}" "$source_dsn"

dump_sha="$(sha256_file "$dump_path")"
dump_bytes="$(wc -c < "$dump_path" | tr -d ' ')"
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  echo "created_at=$created_at"
  echo "source_dsn=$(redact_dsn "$source_dsn")"
  echo "schemas=$schemas"
  echo "exclude_table_data=$(IFS=','; echo "${exclude_table_data[*]}")"
  echo "dump_file=$(basename "$dump_path")"
  echo "dump_bytes=$dump_bytes"
  echo "dump_sha256=$dump_sha"
  echo
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
    echo "$table=$(safe_count "$table")"
  done
} > "$manifest_path"

ln -sfn "$(basename "$dump_path")" "$out_dir/latest.dump"
ln -sfn "$(basename "$manifest_path")" "$out_dir/latest.manifest.txt"

echo "Snapshot written: $dump_path"
echo "Manifest written: $manifest_path"
echo "SHA-256: $dump_sha"
