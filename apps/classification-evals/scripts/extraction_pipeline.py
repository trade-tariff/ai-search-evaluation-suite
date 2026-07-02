#!/usr/bin/env python3
"""Operational ETL runner for the deployed KG extraction pipeline.

The deployed app intentionally keeps provider-backed extraction disabled unless
both the CLI flag and environment gate are present. The safe profile only uses
local tariff/DB sources and bundled data; it does not call LLMs or external
services.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
SEEDER_DIR = SCRIPT_DIR / "etl_seeders"
STATE_DIR = Path(os.environ.get("CLASSIFY_EVAL_STATE_DIR", APP_ROOT / "var")).resolve()
EXTRACTION_STATE_DIR = STATE_DIR / "extraction"
MANIFEST_PATH = EXTRACTION_STATE_DIR / "manifest.json"
LOCK_PATH = EXTRACTION_STATE_DIR / "run.lock"
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("EXTRACTION_STEP_TIMEOUT_SECONDS", "10800"))


@dataclass(frozen=True)
class Step:
    name: str
    description: str
    action: str
    command: tuple[str, ...] = ()
    cwd: str = "."
    safe_profile: bool = False
    provider_spend: bool = False
    external_network: bool = False
    mutates_db: bool = True
    optional: bool = False
    required_files: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    target_tables: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    prompt_names: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(APP_ROOT))
    except ValueError:
        return str(path)


def _python() -> str:
    return os.environ.get("EXTRACTION_PYTHON") or sys.executable


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT / "apps" / "product" / "backend"))
    env.setdefault("AI_FAN_OUT_ENV_FILE", str(APP_ROOT / ".env"))
    env.setdefault("TARIFF_DB_DSN", "postgresql:///tariff_db")
    return env


def _safe_atar_path() -> str:
    return str(SEEDER_DIR / "export_data" / "atar_drafts.json")


def _steps() -> list[Step]:
    seed = str(SEEDER_DIR)
    return [
        Step(
            name="kg_schema",
            description="Apply bundled KG schema, audit, embedding, eval, scope, and evidence-label migrations.",
            action="sql_migrations",
            safe_profile=True,
            required_files=("etl_seeders/sql/001_facets_kg_schema.sql",),
            target_tables=("kg.facet_definitions", "kg.commodity_facets", "kg.kg_edges", "kg.eval_gold"),
            sources=("bundled sql migrations",),
            scopes=("schema", "audit"),
        ),
        Step(
            name="girs",
            description="Seed General Interpretive Rules as global tier-1 KG edges.",
            action="command",
            command=(_python(), f"{seed}/seed_girs.py"),
            cwd=str(SEEDER_DIR),
            safe_profile=True,
            required_files=("etl_seeders/seed_girs.py", "etl_seeders/data/girs.json"),
            target_tables=("kg.kg_edges",),
            sources=("Combined Nomenclature / UK Tariff GIRs",),
            scopes=("classification", "qa", "audit"),
        ),
        Step(
            name="base_facets_kg",
            description="Seed hand-authored facts, chapter/section note edges, and bundled ATAR-derived facts. LLM extraction is forced off.",
            action="command",
            command=(_python(), f"{seed}/seed_facets_kg.py"),
            cwd=str(SEEDER_DIR),
            safe_profile=True,
            required_files=(
                "etl_seeders/seed_facets_kg.py",
                "etl_seeders/data/commodities.json",
                "etl_seeders/data/facets.json",
                "etl_seeders/data/kg_edges.json",
                "etl_seeders/export_data/atar_drafts.json",
            ),
            target_tables=("kg.facet_definitions", "kg.commodity_facets", "kg.kg_edges", "kg.kg_edge_commodities"),
            sources=("hand-authored slice", "uk.chapter_notes", "uk.section_notes", "bundled ATAR drafts"),
            scopes=("retrieval", "classification", "qa", "audit"),
            env={"ENABLE_LLM": "", "ATAR_DRAFTS_PATH": _safe_atar_path()},
            prompt_names=("seed_facets_kg.EXTRACTION_PROMPT (provider-gated; disabled in safe profile)",),
        ),
        Step(
            name="extra_sources",
            description="Seed tariff footnotes, measure conditions, and HMRC search references.",
            action="command",
            command=(_python(), f"{seed}/seed_extra_sources.py"),
            cwd=str(SEEDER_DIR),
            safe_profile=True,
            required_files=("etl_seeders/seed_extra_sources.py",),
            target_tables=("kg.commodity_facets", "kg.kg_edges", "kg.kg_edge_commodities"),
            sources=("uk.footnote_*", "uk.measure_conditions", "uk.search_references"),
            scopes=("retrieval", "classification", "duty", "declaration", "audit"),
        ),
        Step(
            name="hsen",
            description="Parse HSEN DOCX files into interpretive KG edges when HSEN_DIR is mounted/provided.",
            action="command",
            command=(_python(), f"{seed}/seed_hsen.py"),
            cwd=str(SEEDER_DIR),
            safe_profile=True,
            optional=True,
            required_files=("etl_seeders/seed_hsen.py",),
            required_env=("HSEN_DIR",),
            target_tables=("kg.kg_edges", "kg.kg_edge_commodities"),
            sources=("HSEN DOCX directory",),
            scopes=("retrieval", "classification", "qa", "audit"),
        ),
        Step(
            name="notes_decomposition",
            description="LLM-decompose chapter and section notes into atomic rules.",
            action="command",
            command=(_python(), f"{seed}/seed_notes_decomposition.py"),
            cwd=str(SEEDER_DIR),
            provider_spend=True,
            required_files=("etl_seeders/seed_notes_decomposition.py",),
            required_env=("OPENAI_API_KEY",),
            target_tables=("kg.kg_edges", "kg.kg_edge_commodities"),
            sources=("uk.chapter_notes", "uk.section_notes"),
            scopes=("retrieval", "classification", "qa", "audit"),
            prompt_names=("seed_notes_decomposition.DECOMPOSE_PROMPT",),
        ),
        Step(
            name="atar_scrape_extract",
            description="Scrape GOV.UK ATAR pages and LLM-extract product facts/rationale edges.",
            action="command",
            command=(_python(), f"{seed}/seed_atars_for_chapters.py"),
            cwd=str(SEEDER_DIR),
            provider_spend=True,
            external_network=True,
            required_files=("etl_seeders/seed_atars_for_chapters.py",),
            required_env=("OPENAI_API_KEY",),
            target_tables=("kg.commodity_facets", "kg.kg_edges", "kg.kg_edge_commodities"),
            sources=("GOV.UK Advance Tariff Rulings",),
            scopes=("retrieval", "classification", "qa", "audit"),
            prompt_names=("seed_atars_for_chapters.EXTRACTION_PROMPT",),
        ),
        Step(
            name="atar_full_listing",
            description="Fetch full ATAR listing corpus for broader source coverage.",
            action="command",
            command=(_python(), f"{seed}/seed_atars_full_listing.py"),
            cwd=str(SEEDER_DIR),
            provider_spend=True,
            external_network=True,
            required_files=("etl_seeders/seed_atars_full_listing.py",),
            required_env=("OPENAI_API_KEY",),
            target_tables=("kg.commodity_facets", "kg.kg_edges", "kg.kg_edge_commodities"),
            sources=("GOV.UK Advance Tariff Rulings",),
            scopes=("retrieval", "classification", "qa", "audit"),
        ),
        Step(
            name="eval_queries_v1",
            description="Generate naive-trader retrieval-eval queries from ATAR descriptions.",
            action="command",
            command=(_python(), f"{seed}/seed_eval_queries.py"),
            cwd=str(SEEDER_DIR),
            provider_spend=True,
            required_files=("etl_seeders/seed_eval_queries.py",),
            required_env=("OPENAI_API_KEY",),
            target_tables=("kg.eval_gold",),
            sources=("kg.kg_edges where id like atar_%",),
            scopes=("eval",),
            prompt_names=("seed_eval_queries.PARAPHRASE_SYSTEM",),
        ),
        Step(
            name="eval_queries_v2",
            description="Generate emulator-tier retrieval-eval queries from ATAR descriptions.",
            action="command",
            command=(_python(), f"{seed}/seed_eval_queries_v2.py"),
            cwd=str(SEEDER_DIR),
            provider_spend=True,
            required_files=("etl_seeders/seed_eval_queries_v2.py",),
            required_env=("OPENAI_API_KEY",),
            target_tables=("kg.eval_gold",),
            sources=("kg.kg_edges where id like atar_%",),
            scopes=("eval",),
            prompt_names=("intercepts tiered trader-emulator prompt",),
        ),
        Step(
            name="composite_search_text",
            description="Build and embed production-style composite search text for vector/FTS retrieval.",
            action="command",
            command=(_python(), f"{seed}/seed_composite.py"),
            cwd=str(SEEDER_DIR),
            provider_spend=True,
            required_files=("etl_seeders/seed_composite.py",),
            required_env=("OPENAI_API_KEY",),
            target_tables=("kg.composite_search_text",),
            sources=("uk.goods_nomenclature_self_texts", "uk.goods_nomenclature_labels", "uk.search_references"),
            scopes=("retrieval",),
        ),
        Step(
            name="fact_edge_embeddings",
            description="Embed stale commodity facts and KG edges for semantic retrieval.",
            action="command",
            command=(_python(), f"{seed}/seed_embeddings.py"),
            cwd=str(SEEDER_DIR),
            provider_spend=True,
            required_files=("etl_seeders/seed_embeddings.py",),
            required_env=("OPENAI_API_KEY",),
            target_tables=("kg.commodity_facets.embedding", "kg.kg_edges.embedding"),
            sources=("kg.commodity_facets", "kg.kg_edges"),
            scopes=("retrieval",),
        ),
        Step(
            name="opensearch_hydrate",
            description="Hydrate the local OpenSearch commodity index from Postgres search text.",
            action="command",
            command=(_python(), "scripts/hydrate_opensearch_index.py"),
            cwd=str(APP_ROOT),
            safe_profile=True,
            required_files=("scripts/hydrate_opensearch_index.py",),
            target_tables=("opensearch:tariff_commodities",),
            sources=("uk.goods_nomenclature_self_texts",),
            scopes=("retrieval",),
        ),
    ]


def _path_for(required_file: str) -> Path:
    return SCRIPT_DIR / required_file if required_file.startswith("etl_seeders/") else APP_ROOT / required_file


def _allow_provider(allow_spend: bool) -> bool:
    return allow_spend and os.environ.get("EXTRACTION_ALLOW_PROVIDER_CALLS", "").lower() in {"1", "true", "yes", "on"}


def _allow_network(allow_network: bool) -> bool:
    return allow_network or os.environ.get("EXTRACTION_ALLOW_NETWORK", "").lower() in {"1", "true", "yes", "on"}


def _step_status(step: Step, *, allow_spend: bool = False, allow_network: bool = False) -> dict[str, Any]:
    missing_files = [_rel(_path_for(name)) for name in step.required_files if not _path_for(name).exists()]
    missing_env = [name for name in step.required_env if not os.environ.get(name)]
    blockers: list[str] = []
    warnings: list[str] = []
    if missing_files:
        blockers.append("missing required file(s)")
    if missing_env:
        if step.optional:
            warnings.append("missing optional env: " + ", ".join(missing_env))
        else:
            blockers.append("missing required env: " + ", ".join(missing_env))
    if step.provider_spend and not _allow_provider(allow_spend):
        blockers.append("provider-spend gated; requires --allow-spend and EXTRACTION_ALLOW_PROVIDER_CALLS=1")
    if step.external_network and not _allow_network(allow_network):
        blockers.append("external-network gated; requires --allow-network or EXTRACTION_ALLOW_NETWORK=1")
    available = not missing_files and not (missing_env and not step.optional)
    if step.optional and missing_env:
        available = True
    return {
        **asdict(step),
        "available": available,
        "blocked": bool(blockers),
        "blockers": blockers,
        "warnings": warnings,
    }


def _table_count(conn: Any, table: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (table,))
        exists = cur.fetchone()[0] is not None
        if not exists:
            return {"exists": False, "count": None}
        cur.execute(f"SELECT count(*) FROM {table}")
        return {"exists": True, "count": int(cur.fetchone()[0])}


def _db_counts() -> dict[str, Any]:
    try:
        import psycopg
    except Exception as exc:  # pragma: no cover - operational dependency issue
        return {"available": False, "error": f"psycopg unavailable: {type(exc).__name__}: {exc}"}
    dsn = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
    tables = [
        "kg.facet_definitions",
        "kg.commodity_facets",
        "kg.kg_edges",
        "kg.kg_edge_commodities",
        "kg.eval_gold",
        "kg.composite_search_text",
        "kg.query_qpp",
        "kg.query_descriptiveness",
        "kg.e2e_eval_runs",
    ]
    try:
        with psycopg.connect(dsn) as conn:
            return {"available": True, "tables": {table: _table_count(conn, table) for table in tables}}
    except Exception as exc:  # pragma: no cover - operational path
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        return {"error": f"manifest unreadable: {type(exc).__name__}: {exc}", "path": str(path)}


def status_payload(*, allow_spend: bool = False, allow_network: bool = False) -> dict[str, Any]:
    steps = [_step_status(step, allow_spend=allow_spend, allow_network=allow_network) for step in _steps()]
    return {
        "status": "ok",
        "generated_at": _iso(),
        "app_root": str(APP_ROOT),
        "state_dir": str(STATE_DIR),
        "manifest_path": str(MANIFEST_PATH),
        "profiles": {
            "safe": [step.name for step in _steps() if step.safe_profile],
            "all": [step.name for step in _steps()],
            "provider": [step.name for step in _steps() if step.provider_spend],
        },
        "gates": {
            "provider_calls_allowed": _allow_provider(allow_spend),
            "external_network_allowed": _allow_network(allow_network),
            "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
        },
        "steps": steps,
        "db_counts": _db_counts(),
        "last_manifest": _load_manifest(),
    }


def _selected_steps(profile: str, names: list[str]) -> list[Step]:
    steps = _steps()
    by_name = {step.name: step for step in steps}
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        raise SystemExit(f"Unknown extraction step(s): {', '.join(unknown)}")
    if names:
        return [by_name[name] for name in names]
    if profile == "safe":
        return [step for step in steps if step.safe_profile]
    if profile == "provider":
        return [step for step in steps if step.provider_spend]
    if profile == "all":
        return steps
    raise SystemExit(f"Unknown profile: {profile}")


def _tail(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _write_manifest(manifest: dict[str, Any], path: Path = MANIFEST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    tmp.replace(path)


def _apply_sql_migrations() -> dict[str, Any]:
    import psycopg

    sql_dir = SEEDER_DIR / "sql"
    files = sorted(sql_dir.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No SQL files found under {sql_dir}")
    dsn = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
    applied: list[str] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for path in files:
                cur.execute(path.read_text())
                applied.append(_rel(path))
        conn.commit()
    return {"applied_files": applied}


def _run_step(step: Step, *, dry_run: bool, allow_spend: bool, allow_network: bool, timeout_seconds: int) -> dict[str, Any]:
    started = time.time()
    state = _step_status(step, allow_spend=allow_spend, allow_network=allow_network)
    result: dict[str, Any] = {
        "name": step.name,
        "started_at": _iso(),
        "dry_run": dry_run,
        "status": "pending",
        "blockers": state["blockers"],
        "warnings": state["warnings"],
    }
    if state["blockers"]:
        result["status"] = "blocked"
        result["finished_at"] = _iso()
        result["duration_seconds"] = round(time.time() - started, 3)
        return result
    if step.optional and state["warnings"]:
        result["status"] = "skipped_optional"
        result["finished_at"] = _iso()
        result["duration_seconds"] = round(time.time() - started, 3)
        return result
    if dry_run:
        result.update(
            {
                "status": "dry_run",
                "command": list(step.command),
                "cwd": step.cwd,
                "finished_at": _iso(),
                "duration_seconds": round(time.time() - started, 3),
            }
        )
        return result

    env = _base_env()
    env.update(step.env)
    if step.provider_spend:
        env["CLASSIFICATION_ALLOW_PROVIDER_CALLS"] = "1"
    try:
        if step.action == "sql_migrations":
            detail = _apply_sql_migrations()
            result.update({"status": "succeeded", "detail": detail, "returncode": 0})
        elif step.action == "command":
            proc = subprocess.run(
                list(step.command),
                cwd=step.cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            result.update(
                {
                    "status": "succeeded" if proc.returncode == 0 else "failed",
                    "returncode": proc.returncode,
                    "command": list(step.command),
                    "cwd": step.cwd,
                    "stdout_tail": _tail(proc.stdout or ""),
                    "stderr_tail": _tail(proc.stderr or ""),
                }
            )
        else:
            result.update({"status": "failed", "error": f"unknown step action {step.action!r}"})
    except subprocess.TimeoutExpired as exc:
        result.update({"status": "failed", "error": f"timeout after {timeout_seconds}s", "stdout_tail": _tail(exc.stdout or ""), "stderr_tail": _tail(exc.stderr or "")})
    except Exception as exc:  # pragma: no cover - operational path
        result.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    result["finished_at"] = _iso()
    result["duration_seconds"] = round(time.time() - started, 3)
    return result


def _acquire_run_lock():
    """One extraction run at a time: the manual compose profile and the
    scheduler can otherwise collide on the same tables and manifest."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def execute_run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    lock_handle = _acquire_run_lock()
    if lock_handle is None:
        manifest = {
            "run_id": None,
            "started_at": _iso(),
            "status": "locked",
            "error": f"Another extraction run holds {LOCK_PATH}; skipping.",
        }
        print(json.dumps({"status": "locked", "lock": str(LOCK_PATH)}), flush=True)
        return 3, manifest
    try:
        return _execute_run_locked(args)
    finally:
        lock_handle.close()


def _execute_run_locked(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    selected = _selected_steps(args.profile, args.step or [])
    manifest: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "started_at": _iso(),
        "profile": args.profile,
        "dry_run": args.dry_run,
        "allow_spend": args.allow_spend,
        "allow_network": args.allow_network,
        "selected_steps": [step.name for step in selected],
        "db_counts_before": _db_counts(),
        "steps": [],
    }
    exit_code = 0
    for step in selected:
        result = _run_step(
            step,
            dry_run=args.dry_run,
            allow_spend=args.allow_spend,
            allow_network=args.allow_network,
            timeout_seconds=args.timeout_seconds,
        )
        manifest["steps"].append(result)
        if result["status"] in {"blocked", "failed"}:
            exit_code = 2 if result["status"] == "blocked" else 1
            if not args.continue_on_error:
                break
    manifest["db_counts_after"] = _db_counts()
    manifest["finished_at"] = _iso()
    manifest["status"] = "succeeded" if exit_code == 0 else ("blocked" if exit_code == 2 else "failed")
    if not args.no_write_manifest:
        _write_manifest(manifest)
    return exit_code, manifest


def run_schedule(args: argparse.Namespace) -> int:
    interval = max(60, args.interval_seconds)
    while True:
        code, manifest = execute_run(args)
        print(json.dumps({"scheduled_run": manifest["run_id"], "status": manifest["status"], "exit_code": code}), flush=True)
        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Print extraction pipeline status as JSON.")
    status.add_argument("--allow-spend", action="store_true", help="Evaluate provider-backed steps as spend-allowed for status only.")
    status.add_argument("--allow-network", action="store_true", help="Evaluate external-network steps as network-allowed for status only.")
    status.add_argument("--json", action="store_true", help="Kept for API callers; output is always JSON.")

    run = sub.add_parser("run", help="Run a one-shot extraction profile or specific step list.")
    run.add_argument("--profile", choices=("safe", "all", "provider"), default="safe")
    run.add_argument("--step", action="append", help="Run a named step. Can be passed more than once.")
    run.add_argument("--dry-run", action="store_true", help="Write a manifest without executing ETL steps.")
    run.add_argument("--allow-spend", action="store_true", help="Allow provider-backed steps when EXTRACTION_ALLOW_PROVIDER_CALLS=1 is also set.")
    run.add_argument("--allow-network", action="store_true", help="Allow external-network steps.")
    run.add_argument("--continue-on-error", action="store_true")
    run.add_argument("--no-write-manifest", action="store_true")
    run.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    sched = sub.add_parser("schedule", help="Run a profile repeatedly and update the manifest each cycle.")
    sched.add_argument("--profile", choices=("safe", "all", "provider"), default="safe")
    sched.add_argument("--step", action="append")
    sched.add_argument("--dry-run", action="store_true")
    sched.add_argument("--allow-spend", action="store_true")
    sched.add_argument("--allow-network", action="store_true")
    sched.add_argument("--continue-on-error", action="store_true")
    sched.add_argument("--no-write-manifest", action="store_true")
    sched.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    sched.add_argument("--interval-seconds", type=int, default=int(os.environ.get("EXTRACTION_RUN_INTERVAL_SECONDS", "86400")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "status":
        print(json.dumps(status_payload(allow_spend=args.allow_spend, allow_network=args.allow_network), indent=2, sort_keys=True))
        return 0
    if args.command == "run":
        code, manifest = execute_run(args)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return code
    if args.command == "schedule":
        return run_schedule(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
