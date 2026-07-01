from __future__ import annotations

import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import asyncio
import importlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import auth_enabled, install_optional_auth


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[1]
_DEFAULT_PRODUCT_ROOT = REPO_ROOT / "apps" / "product"
PRODUCT_APP_ROOT = Path(
    os.environ.get("PRODUCT_APP_ROOT")
    or _DEFAULT_PRODUCT_ROOT
).resolve()
PRODUCT_BACKEND = PRODUCT_APP_ROOT / "backend"
if str(PRODUCT_BACKEND) not in sys.path:
    sys.path.insert(0, str(PRODUCT_BACKEND))
STATE_DIR = Path(os.environ.get("CLASSIFY_EVAL_STATE_DIR", APP_ROOT / "var")).resolve()
JOBS_DIR = STATE_DIR / "jobs"
DB_PATH = STATE_DIR / "jobs.sqlite"
MATRIX_DIR = PRODUCT_APP_ROOT / "data" / "matrix"
PRODUCT_FRONTEND_DIST = PRODUCT_APP_ROOT / "frontend" / "dist"
PRODUCT_FRONTEND_ASSETS = PRODUCT_FRONTEND_DIST / "assets"
PRODUCT_FRONTEND_INDEX = PRODUCT_FRONTEND_DIST / "index.html"

MODEL_CHOICES = ("gpt-5-nano", "gpt-5-mini", "gpt-5.5")
PERSONA_CHOICES = (
    "naive_vague",
    "naive_branded",
    "naive_specific",
    "emu_generic",
    "emu_ordinary",
    "emu_specific",
    "original",
)

app = FastAPI(
    title="AI Search Evaluation Suite",
    description="AI search evaluation suite with retrieval, classification, KG, ATaR, benchmark, and intercept tooling.",
)
install_optional_auth(app, realm="AI Search Evaluation Suite")

_PROCESS_LOCK = threading.Lock()
_CLASSIFY_TRIAL_LOCK = threading.Lock()
_PROCESSES: dict[str, subprocess.Popen] = {}
_RUNNER_INSTANCE_ID = uuid.uuid4().hex


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _allowed_models() -> list[str]:
    configured = [m.strip() for m in os.environ.get("CLASSIFY_EVAL_ALLOWED_MODELS", "").split(",") if m.strip()]
    return configured or list(MODEL_CHOICES)


def _running_process_count() -> int:
    with _PROCESS_LOCK:
        return sum(1 for process in _PROCESSES.values() if process.poll() is None)


def _estimated_sessions(req: "JobCreate") -> int:
    if req.sweep:
        return _env_int("CLASSIFY_EVAL_SWEEP_SESSION_ESTIMATE", 500)
    per_persona = req.limit if req.limit is not None else _env_int("CLASSIFY_EVAL_UNCAPPED_SESSION_ESTIMATE", 500)
    return len(req.personas) * per_persona


def _estimated_cost_usd(req: "JobCreate") -> float:
    per_session = _env_float("CLASSIFY_EVAL_EST_USD_PER_SESSION", 0.05)
    return round(_estimated_sessions(req) * per_session, 4)


class JobCreate(BaseModel):
    run_label: str = Field(default="demo_classify_eval", pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    strategy: Literal["converge", "eliminate"] = "converge"
    prompt_mode: Literal["baseline", "rule_reasoning", "exclusion_aware", "gir_citation", "self_verify"] = "baseline"
    augmentation: Literal["none", "facts", "kg", "facts+kg"] = "facts+kg"
    model: str = Field(default="gpt-5-mini", pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    simulator_model: str = Field(default="gpt-5-mini", pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    candidate_limit: int = Field(default=40, ge=5, le=500)
    personas: list[str] = Field(default_factory=lambda: ["naive_vague"], min_length=1, max_length=7)
    limit: int | None = Field(default=5, ge=1, le=500)
    concurrency: int = Field(default=2, ge=1, le=32)
    max_rounds: int = Field(default=5, ge=1, le=12)
    sweep: bool = False
    allow_spend: bool = False


class RetrievalSearch(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    expected_code: str | None = Field(default=None, max_length=32)
    run_label: str | None = Field(default=None, max_length=120)
    retrieval_limit: int = Field(default=100, ge=10, le=500)
    allow_spend: bool = False


class InputScoreRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    run_label: str | None = Field(default="baseline_fts_only", max_length=120)
    retrieval_limit: int = Field(default=100, ge=10, le=500)
    include_candidates: bool = False
    allow_spend: bool = False


class ClassifyTrial(BaseModel):
    gold_id: int | None = None
    query: str | None = Field(default=None, max_length=500)
    expected_code: str | None = Field(default=None, max_length=32)
    oracle_text: str | None = Field(default=None, max_length=12000)
    strategy: Literal["converge", "eliminate"] = "converge"
    prompt_mode: Literal["baseline", "rule_reasoning", "exclusion_aware", "gir_citation", "self_verify"] = "baseline"
    augmentation: Literal["none", "facts", "kg", "facts+kg"] = "facts+kg"
    model: str = Field(default="gpt-5-mini", pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    simulator_model: str = Field(default="gpt-5-mini", pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    candidate_limit: int = Field(default=40, ge=5, le=200)
    max_rounds: int = Field(default=5, ge=1, le=12)
    allow_spend: bool = False


@contextmanager
def _temporary_env(updates: dict[str, str | None]):
    old_values = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, old in old_values.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _tariff_dsn() -> str:
    return os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")


def _extraction_pipeline_script() -> Path:
    return APP_ROOT / "scripts" / "extraction_pipeline.py"


def _extraction_status_payload() -> dict[str, Any]:
    script = _extraction_pipeline_script()
    if not script.exists():
        return {
            "status": "missing",
            "available": False,
            "script": str(script),
            "detail": "scripts/extraction_pipeline.py is not present in the deployed app bundle.",
        }
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(PRODUCT_BACKEND))
    try:
        result = subprocess.run(
            [sys.executable, str(script), "status", "--json"],
            cwd=APP_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "available": True,
            "script": str(script),
            "detail": "extraction status command timed out after 20 seconds",
        }
    except Exception as exc:
        return {
            "status": "error",
            "available": True,
            "script": str(script),
            "detail": f"{type(exc).__name__}: {exc}",
        }
    if result.returncode != 0:
        return {
            "status": "error",
            "available": True,
            "script": str(script),
            "returncode": result.returncode,
            "stderr": (result.stderr or "")[-2000:],
            "stdout": (result.stdout or "")[-2000:],
        }
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "available": True,
            "script": str(script),
            "detail": f"status output was not JSON: {exc}",
            "stdout": (result.stdout or "")[-2000:],
        }
    payload["available"] = True
    payload["script"] = str(script)
    return payload


def _init_db() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id text PRIMARY KEY,
              created_at real NOT NULL,
              updated_at real NOT NULL,
              status text NOT NULL,
              pid integer,
              returncode integer,
              command_json text NOT NULL,
              request_json text NOT NULL,
              log_path text NOT NULL,
              error text
            )
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for column, ddl in {
            "runner_instance": "ALTER TABLE jobs ADD COLUMN runner_instance text",
            "estimated_sessions": "ALTER TABLE jobs ADD COLUMN estimated_sessions integer",
            "estimated_cost_usd": "ALTER TABLE jobs ADD COLUMN estimated_cost_usd real",
        }.items():
            if column not in existing:
                conn.execute(ddl)


def _connect() -> sqlite3.Connection:
    _init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row, *, include_internal: bool = False) -> dict:
    data = dict(row)
    data["command"] = json.loads(data.pop("command_json"))
    data["request"] = json.loads(data.pop("request_json"))
    log_path = data.pop("log_path")
    if include_internal:
        data["log_path"] = log_path
    else:
        data["log_url"] = f"/api/jobs/{data['id']}/log"
    return data


def _refresh_job(row: sqlite3.Row, *, include_internal: bool = False) -> dict:
    job = _row_to_dict(row, include_internal=include_internal)
    if job["status"] == "running" and job["pid"]:
        with _PROCESS_LOCK:
            process = _PROCESSES.get(job["id"])
        if process is not None:
            code = process.poll()
            if code is not None:
                _record_exit(job["id"], code)
                job["status"] = "succeeded" if code == 0 else "failed"
                job["returncode"] = code
        else:
            _record_exit(job["id"], -1, status="unknown_exit")
            job["status"] = "unknown_exit"
            job["returncode"] = -1
    return job


def _record_exit(job_id: str, returncode: int, status: str | None = None) -> None:
    final_status = status or ("succeeded" if returncode == 0 else "failed")
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, returncode=?, updated_at=? WHERE id=?",
            (final_status, returncode, time.time(), job_id),
        )
    with _PROCESS_LOCK:
        _PROCESSES.pop(job_id, None)


def _watch_process(job_id: str, process: subprocess.Popen) -> None:
    returncode = process.wait()
    _record_exit(job_id, returncode)


def _validate_request(req: JobCreate) -> None:
    unknown_personas = sorted(set(req.personas) - set(PERSONA_CHOICES))
    if unknown_personas:
        raise HTTPException(422, f"Unknown personas: {', '.join(unknown_personas)}")
    if req.model not in _allowed_models():
        raise HTTPException(422, "Model is not enabled for classification eval jobs.")
    if req.simulator_model not in _allowed_models():
        raise HTTPException(422, "Trader emulator model is not enabled for classification eval jobs.")
    if req.sweep and not _env_bool("CLASSIFY_EVAL_ALLOW_SWEEP"):
        raise HTTPException(403, "Sweep jobs are disabled. Set CLASSIFY_EVAL_ALLOW_SWEEP=1 to enable them.")
    max_running = _env_int("CLASSIFY_EVAL_MAX_RUNNING_JOBS", 1)
    if _running_process_count() >= max_running:
        raise HTTPException(429, f"Runner already has {max_running} active job(s).")
    max_concurrency = _env_int("CLASSIFY_EVAL_MAX_CONCURRENCY", 4)
    if req.concurrency > max_concurrency:
        raise HTTPException(422, f"Concurrency exceeds server cap CLASSIFY_EVAL_MAX_CONCURRENCY={max_concurrency}.")
    max_rounds = _env_int("CLASSIFY_EVAL_MAX_ROUNDS", 8)
    if req.max_rounds > max_rounds:
        raise HTTPException(422, f"Max rounds exceeds server cap CLASSIFY_EVAL_MAX_ROUNDS={max_rounds}.")
    max_candidate_limit = _env_int("CLASSIFY_EVAL_MAX_CANDIDATE_LIMIT", 200)
    if req.candidate_limit > max_candidate_limit:
        raise HTTPException(422, f"Candidate limit exceeds server cap CLASSIFY_EVAL_MAX_CANDIDATE_LIMIT={max_candidate_limit}.")
    sessions = _estimated_sessions(req)
    max_sessions = _env_int("CLASSIFY_EVAL_MAX_SESSIONS", 50)
    if sessions > max_sessions:
        raise HTTPException(422, f"Estimated sessions {sessions} exceeds server cap CLASSIFY_EVAL_MAX_SESSIONS={max_sessions}.")
    estimated_cost = _estimated_cost_usd(req)
    max_cost = _env_float("CLASSIFY_EVAL_MAX_EST_USD", 10.0)
    if estimated_cost > max_cost:
        raise HTTPException(422, f"Estimated cost ${estimated_cost:.2f} exceeds server cap CLASSIFY_EVAL_MAX_EST_USD=${max_cost:.2f}.")
    if not req.allow_spend:
        raise HTTPException(
            403,
            "Classification evals call provider models. Set allow_spend=true for this job.",
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(400, "OPENAI_API_KEY is required for classification eval jobs.")
    if not PRODUCT_BACKEND.exists():
        raise HTTPException(500, "Product backend is not available.")


def _build_command(req: JobCreate) -> list[str]:
    python = os.environ.get("CLASSIFY_EVAL_PYTHON") or sys.executable
    cmd = [
        python,
        "-m",
        "classification_core.run_classify_matrix",
        "--strategy",
        req.strategy,
        "--prompt-mode",
        req.prompt_mode,
        "--augmentation",
        req.augmentation,
        "--model",
        req.model,
        "--candidate-limit",
        str(req.candidate_limit),
        "--personas",
        ",".join(req.personas),
        "--concurrency",
        str(req.concurrency),
        "--max-rounds",
        str(req.max_rounds),
    ]
    if req.sweep:
        cmd.append("--sweep")
    else:
        cmd.extend(["--run-label", req.run_label])
    if req.limit is not None:
        cmd.extend(["--limit", str(req.limit)])
    return cmd


@app.get("/api/health")
def health() -> dict:
    _init_db()
    return {
        "status": "ok",
        "auth_enabled": auth_enabled(),
        "product_backend_present": PRODUCT_BACKEND.exists(),
        "kg_label_profile": os.environ.get("AI_FAN_OUT_KG_LABEL_PROFILE", "full"),
        "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
        "state_ready": STATE_DIR.exists(),
        "extraction_pipeline_present": _extraction_pipeline_script().exists(),
        "limits": {
            "allowed_models": _allowed_models(),
            "max_running_jobs": _env_int("CLASSIFY_EVAL_MAX_RUNNING_JOBS", 1),
            "max_sessions": _env_int("CLASSIFY_EVAL_MAX_SESSIONS", 50),
            "max_est_usd": _env_float("CLASSIFY_EVAL_MAX_EST_USD", 10.0),
        },
    }


@app.get("/api/live")
def live() -> dict:
    return {"status": "ok"}


@app.get("/api/extraction/status")
def extraction_status() -> dict:
    return _extraction_status_payload()


@app.get("/api/options")
def options() -> dict:
    return {
        "models": _allowed_models(),
        "personas": list(PERSONA_CHOICES),
        "strategies": ["converge", "eliminate"],
        "prompt_modes": ["baseline", "rule_reasoning", "exclusion_aware", "gir_citation", "self_verify"],
        "augmentations": ["none", "facts", "kg", "facts+kg"],
        "limits": {
            "max_running_jobs": _env_int("CLASSIFY_EVAL_MAX_RUNNING_JOBS", 1),
            "max_concurrency": _env_int("CLASSIFY_EVAL_MAX_CONCURRENCY", 4),
            "max_rounds": _env_int("CLASSIFY_EVAL_MAX_ROUNDS", 8),
            "max_candidate_limit": _env_int("CLASSIFY_EVAL_MAX_CANDIDATE_LIMIT", 200),
            "max_sessions": _env_int("CLASSIFY_EVAL_MAX_SESSIONS", 50),
            "max_est_usd": _env_float("CLASSIFY_EVAL_MAX_EST_USD", 10.0),
            "allow_sweep": _env_bool("CLASSIFY_EVAL_ALLOW_SWEEP"),
        },
    }


@app.get("/api/eval-cost/summary")
def eval_cost_summary(limit: int = 20) -> dict:
    limit = max(1, min(int(limit or 20), 100))
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(_tariff_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('kg.commodity_fact_model_eval') AS table_name")
            if not cur.fetchone()["table_name"]:
                return {
                    "totals": {
                        "calls": 0,
                        "ok": 0,
                        "failed": 0,
                        "runs": 0,
                        "models": 0,
                        "prompt_versions": 0,
                        "cost_usd": 0.0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                    },
                    "runs": [],
                    "model_totals": [],
                    "prompt_totals": [],
                }

            cur.execute(
                """
                SELECT count(*)::int AS calls,
                       count(*) FILTER (WHERE error IS NULL)::int AS ok,
                       count(*) FILTER (WHERE error IS NOT NULL)::int AS failed,
                       count(DISTINCT run_id)::int AS runs,
                       count(DISTINCT model)::int AS models,
                       count(DISTINCT prompt_version)::int AS prompt_versions,
                       coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                       coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,
                       min(created_at) AS first_write,
                       max(created_at) AS last_write
                FROM kg.commodity_fact_model_eval
                """
            )
            totals = dict(cur.fetchone())

            cur.execute(
                """
                SELECT run_id,
                       count(*)::int AS calls,
                       count(*) FILTER (WHERE error IS NULL)::int AS ok,
                       count(*) FILTER (WHERE error IS NOT NULL)::int AS failed,
                       count(DISTINCT model)::int AS models,
                       count(DISTINCT prompt_version)::int AS prompt_versions,
                       count(DISTINCT commodity_code)::int AS commodity_codes,
                       coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                       coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,
                       avg(nullif(quality->>'score', '')::float8)::float8 AS avg_score,
                       min(created_at) AS first_write,
                       max(created_at) AS last_write,
                       extract(epoch FROM max(created_at) - min(created_at))::float8 AS duration_seconds
                FROM kg.commodity_fact_model_eval
                GROUP BY run_id
                ORDER BY max(created_at) DESC
                LIMIT %s
                """,
                (limit,),
            )
            runs = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT model,
                       count(*)::int AS calls,
                       count(*) FILTER (WHERE error IS NULL)::int AS ok,
                       coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                       coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,
                       avg(nullif(quality->>'score', '')::float8)::float8 AS avg_score
                FROM kg.commodity_fact_model_eval
                GROUP BY model
                ORDER BY cost_usd DESC
                """
            )
            model_totals = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT prompt_version,
                       count(*)::int AS calls,
                       count(*) FILTER (WHERE error IS NULL)::int AS ok,
                       coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                       avg(nullif(quality->>'score', '')::float8)::float8 AS avg_score
                FROM kg.commodity_fact_model_eval
                GROUP BY prompt_version
                ORDER BY cost_usd DESC
                LIMIT 50
                """
            )
            prompt_totals = [dict(row) for row in cur.fetchall()]

            embedding_cost_per_million = float(os.environ.get("COST_EMBEDDING_USD_PER_1M_TOKENS", "0.02"))
            e2e_provider_call_est_usd = float(os.environ.get("COST_E2E_PROVIDER_CALL_USD", "0.002"))

            retrieval_runs = []
            retrieval_totals = {
                "runs": 0,
                "calls": 0,
                "estimated_embedding_tokens": 0,
                "estimated_cost_usd": 0.0,
                "last_write": None,
            }
            cur.execute("SELECT to_regclass('kg.eval_runs') AS table_name")
            if cur.fetchone()["table_name"]:
                cur.execute(
                    """
                    SELECT er.id,
                           er.run_label,
                           er.config_json,
                           er.n_queries,
                           er.retrieval_limit,
                           count(rr.id)::int AS calls,
                           coalesce(sum(greatest(1, ceil(length(coalesce(g.query, '')) / 4.0))), 0)::bigint AS estimated_embedding_tokens,
                           er.started_at AS first_write,
                           er.finished_at AS last_write,
                           extract(epoch FROM er.finished_at - er.started_at)::float8 AS duration_seconds
                    FROM kg.eval_runs er
                    LEFT JOIN kg.eval_run_results rr ON rr.run_id = er.id
                    LEFT JOIN kg.eval_gold g ON g.id = rr.gold_id
                    GROUP BY er.id
                    ORDER BY er.started_at DESC
                    """
                )
                for row in cur.fetchall():
                    item = dict(row)
                    cfg = item.pop("config_json") or {}
                    item["use_vector"] = bool(cfg.get("use_vector"))
                    item["use_facts_vec"] = bool(cfg.get("use_facts_vec"))
                    item["use_kg_vec"] = bool(cfg.get("use_kg_vec"))
                    vector_enabled = item["use_vector"] or item["use_facts_vec"] or item["use_kg_vec"]
                    tokens = int(item.get("estimated_embedding_tokens") or 0) if vector_enabled else 0
                    item["estimated_embedding_tokens"] = tokens
                    item["estimated_cost_usd"] = tokens * embedding_cost_per_million / 1_000_000.0
                    retrieval_runs.append(item)
                retrieval_totals = {
                    "runs": len(retrieval_runs),
                    "calls": sum(int(row.get("calls") or 0) for row in retrieval_runs),
                    "estimated_embedding_tokens": sum(int(row.get("estimated_embedding_tokens") or 0) for row in retrieval_runs),
                    "estimated_cost_usd": sum(float(row.get("estimated_cost_usd") or 0.0) for row in retrieval_runs),
                    "last_write": max((row.get("last_write") for row in retrieval_runs if row.get("last_write")), default=None),
                }
                retrieval_runs = retrieval_runs[:limit]

            e2e_runs = []
            e2e_totals = {
                "runs": 0,
                "provider_calls": 0,
                "estimated_embedding_tokens": 0,
                "estimated_cost_usd": 0.0,
                "last_write": None,
            }
            cur.execute("SELECT to_regclass('kg.e2e_eval_runs') AS table_name")
            if cur.fetchone()["table_name"]:
                cur.execute(
                    """
                    SELECT r.id,
                           r.run_label,
                           r.retrieval_run_label,
                           r.question_mode,
                           r.answerer,
                           r.config_json,
                           r.input_count,
                           r.provider_calls_used,
                           coalesce(sum(greatest(1, ceil(length(coalesce(res.query, '')) / 4.0))), 0)::bigint AS estimated_embedding_tokens,
                           r.started_at AS first_write,
                           r.finished_at AS last_write,
                           extract(epoch FROM r.finished_at - r.started_at)::float8 AS duration_seconds
                    FROM kg.e2e_eval_runs r
                    LEFT JOIN kg.e2e_eval_results res ON res.run_id = r.id
                    GROUP BY r.id
                    ORDER BY r.started_at DESC
                    """
                )
                for row in cur.fetchall():
                    item = dict(row)
                    cfg = item.pop("config_json") or {}
                    retrieval_cfg = cfg.get("retrieval_config") or {}
                    vector_enabled = bool(
                        retrieval_cfg.get("use_vector")
                        or retrieval_cfg.get("use_facts_vec")
                        or retrieval_cfg.get("use_kg_vec")
                    )
                    tokens = int(item.get("estimated_embedding_tokens") or 0) if vector_enabled else 0
                    provider_calls = int(item.get("provider_calls_used") or 0)
                    item["estimated_embedding_tokens"] = tokens
                    item["estimated_cost_usd"] = (
                        provider_calls * e2e_provider_call_est_usd
                        + tokens * embedding_cost_per_million / 1_000_000.0
                    )
                    e2e_runs.append(item)
                e2e_totals = {
                    "runs": len(e2e_runs),
                    "provider_calls": sum(int(row.get("provider_calls_used") or 0) for row in e2e_runs),
                    "estimated_embedding_tokens": sum(int(row.get("estimated_embedding_tokens") or 0) for row in e2e_runs),
                    "estimated_cost_usd": sum(float(row.get("estimated_cost_usd") or 0.0) for row in e2e_runs),
                    "last_write": max((row.get("last_write") for row in e2e_runs if row.get("last_write")), default=None),
                }
                e2e_runs = e2e_runs[:limit]

            classification_runs = []
            classification_totals = {
                "runs": 0,
                "sessions": 0,
                "estimated_cost_usd": 0.0,
                "last_write": None,
            }
            cur.execute("SELECT to_regclass('kg.classify_runs') AS table_name")
            if cur.fetchone()["table_name"]:
                cur.execute(
                    """
                    SELECT run_label,
                           model,
                           strategy,
                           prompt_mode,
                           augmentation,
                           count(*)::int AS sessions,
                           coalesce(sum(est_cost_usd), 0)::float8 AS estimated_cost_usd,
                           min(started_at) AS first_write,
                           max(started_at) AS last_write
                    FROM kg.classify_runs
                    GROUP BY run_label, model, strategy, prompt_mode, augmentation
                    ORDER BY max(started_at) DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                classification_runs = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT count(DISTINCT run_label)::int AS runs,
                           count(*)::int AS sessions,
                           coalesce(sum(est_cost_usd), 0)::float8 AS estimated_cost_usd,
                           max(started_at) AS last_write
                    FROM kg.classify_runs
                    """
                )
                classification_totals = dict(cur.fetchone())

    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    def encode(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in ("first_write", "last_write"):
            if out.get(key) is not None:
                out[key] = out[key].isoformat()
        return out

    fact_cost = float((totals or {}).get("cost_usd") or 0.0)
    retrieval_cost = float((retrieval_totals or {}).get("estimated_cost_usd") or 0.0)
    e2e_cost = float((e2e_totals or {}).get("estimated_cost_usd") or 0.0)
    classification_cost = float((classification_totals or {}).get("estimated_cost_usd") or 0.0)

    return {
        "totals": encode(totals),
        "runs": [encode(row) for row in runs],
        "model_totals": [encode(row) for row in model_totals],
        "prompt_totals": [encode(row) for row in prompt_totals],
        "spend_totals": {
            "fact_eval_cost_usd": fact_cost,
            "retrieval_embedding_est_cost_usd": retrieval_cost,
            "e2e_est_cost_usd": e2e_cost,
            "classification_est_cost_usd": classification_cost,
            "estimated_total_usd": fact_cost + retrieval_cost + e2e_cost + classification_cost,
            "embedding_cost_per_million_tokens": embedding_cost_per_million,
            "e2e_provider_call_est_usd": e2e_provider_call_est_usd,
        },
        "retrieval": {
            "totals": encode(retrieval_totals),
            "runs": [encode(row) for row in retrieval_runs],
        },
        "e2e": {
            "totals": encode(e2e_totals),
            "runs": [encode(row) for row in e2e_runs],
        },
        "classification": {
            "totals": encode(classification_totals),
            "runs": [encode(row) for row in classification_runs],
        },
    }


@app.get("/api/retrieval/experiments")
def retrieval_experiments() -> dict:
    from experiment_retrieval import experiment_catalog

    return {"experiments": [_decorate_retrieval_experiment(row) for row in experiment_catalog()]}


@app.get("/api/retrieval/top-experiment")
def retrieval_top_experiment() -> dict:
    from experiment_retrieval import top_experiment_info

    return _decorate_retrieval_experiment(top_experiment_info())


def _retrieval_needs_provider(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("triage") or cfg.get("use_vector") or cfg.get("use_facts_vec") or cfg.get("use_kg_vec"))


def _decorate_retrieval_experiment(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    cfg = out.get("config") or {}
    triage = bool(cfg.get("triage"))
    out["matrix_runnable"] = bool(out.get("runnable"))
    out["deploy_runnable"] = bool(out.get("runnable") or triage)
    out["runnable"] = out["deploy_runnable"]
    out["deploy_requires_rewrite"] = triage
    out["deploy_provider_steps"] = [
        step
        for step, enabled in {
            "rewrite": triage,
            "embedding": bool(cfg.get("use_vector") or cfg.get("use_facts_vec") or cfg.get("use_kg_vec")),
        }.items()
        if enabled
    ]
    if triage:
        caveats = [
            c for c in out.get("caveats", [])
            if "not runnable in the local trial form" not in c
        ]
        caveats.append(
            "Runnable here via the bundled query rewrite harness before retrieval."
        )
        out["caveats"] = caveats
    return out


@app.post("/api/retrieval/search")
def retrieval_search(req: RetrievalSearch) -> dict:
    from experiment_retrieval import (
        DEFAULT_LIMIT,
        DISPLAY_LIMIT,
        _flat_code,
        experiment_catalog,
        query_difficulty_from_candidates,
        query_lexical_specificity,
        retrieve_for_config,
    )

    catalog = [_decorate_retrieval_experiment(row) for row in experiment_catalog()]
    selected = next(
        (row for row in catalog if row.get("run_label") == (req.run_label or "baseline_fts_only")),
        None,
    )
    if selected is None:
        raise HTTPException(404, f"Unknown experiment: {req.run_label}")
    cfg = selected.get("config") or {}
    needs_provider = _retrieval_needs_provider(cfg)
    if needs_provider and not req.allow_spend:
        raise HTTPException(
            403,
            "Selected experiment uses provider-backed rewrite and/or embeddings. Enable provider calls or choose the keyword baseline.",
        )
    api_key = os.environ.get("OPENAI_API_KEY")
    if needs_provider and not api_key:
        raise HTTPException(400, "OPENAI_API_KEY is required for semantic retrieval trials.")

    limit = max(10, min(int(req.retrieval_limit or 100), DEFAULT_LIMIT))
    processed_query = req.query
    rewrite_info: dict[str, Any] | None = None
    runnable_cfg = dict(cfg)
    try:
        if cfg.get("triage"):
            from classification_core import triage as query_triage

            triage_model = str(cfg.get("triage_model") or os.environ.get("TRIAGE_MODEL") or "gpt-5-mini")
            triage_prompt = str(cfg.get("triage_prompt") or "mine")
            processed_query = query_triage.expand_query(
                req.query,
                model=triage_model,
                prompt_variant=triage_prompt,
            )
            rewrite_info = {
                "model": triage_model,
                "prompt_variant": triage_prompt,
                "expanded_query": processed_query,
                "changed": processed_query.strip() != req.query.strip(),
            }
            runnable_cfg["triage"] = False
        candidates, leg_counts = retrieve_for_config(processed_query, runnable_cfg, api_key, limit)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    expected_flat = _flat_code(req.expected_code or "")
    rank = None
    if expected_flat:
        for idx, row in enumerate(candidates, start=1):
            if _flat_code(str(row.get("commodity_code") or "")) == expected_flat:
                rank = idx
                break

    ranked_candidates = []
    for idx, row in enumerate(candidates, start=1):
        item = dict(row)
        item["rank"] = idx
        ranked_candidates.append(item)
    top_candidates = ranked_candidates[:DISPLAY_LIMIT]

    return {
        "query": req.query,
        "processed_query": processed_query,
        "rewrite": rewrite_info,
        "expected_code": req.expected_code,
        "expected_code_normalized": expected_flat,
        "evaluated": bool(expected_flat),
        "experiment": selected,
        "retrieval_limit": limit,
        "provider_calls_used": needs_provider,
        "provider_call_types": selected.get("deploy_provider_steps", []),
        "rank": rank,
        "hit_at_10": bool(rank and rank <= 10),
        "hit_at_100": bool(rank and rank <= 100),
        "hit_within_limit": rank is not None,
        "leg_counts": leg_counts,
        "lexical_specificity": query_lexical_specificity(req.query),
        "query_difficulty": query_difficulty_from_candidates(processed_query, ranked_candidates, k=limit),
        "top_candidates": top_candidates,
        "candidates": ranked_candidates,
    }


@app.post("/api/retrieval/try")
def retrieval_try(payload: dict) -> dict:
    """Compatibility route for the full React Experiments tab.

    It intentionally delegates to this app's spend-aware retrieval path.
    """
    query = str(payload.get("query") or "").strip()
    expected_code = str(payload.get("expected_code") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    try:
        retrieval_limit = int(payload.get("retrieval_limit") or 100)
    except (TypeError, ValueError):
        raise HTTPException(400, "retrieval_limit must be an integer")
    return retrieval_search(
        RetrievalSearch(
            query=query,
            expected_code=expected_code,
            run_label=str(payload["run_label"]) if payload.get("run_label") else None,
            retrieval_limit=retrieval_limit,
            allow_spend=payload.get("allow_spend") is True,
        )
    )


def _score_flat_code(code: str) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits.ljust(10, "0")[:10] if digits else ""


def _score_weight(row: dict[str, Any], rank: int) -> float:
    for key in ("score", "rrf_score", "cosine_score"):
        try:
            value = row.get(key)
            if value is not None:
                value_f = float(value)
                if value_f > 0:
                    return value_f
        except Exception:
            pass
    return 1.0 / (rank + 60.0)


def _shannon_bits(values: list[float]) -> float:
    total = sum(v for v in values if v > 0)
    if total <= 0:
        return 0.0
    entropy = 0.0
    for value in values:
        if value <= 0:
            continue
        p = value / total
        entropy -= p * math.log2(p)
    return entropy


def _input_code_prefix_entropy(candidates: list[dict[str, Any]], k: int) -> dict[str, Any]:
    subset = list(candidates or [])[: max(1, int(k or 1))]
    levels = {"chapter": 2, "heading": 4, "subheading": 6, "cn8": 8, "commodity": 10}
    distributions: dict[str, dict[str, float]] = {name: {} for name in levels}
    candidate_count = 0
    for rank, row in enumerate(subset, start=1):
        code = _score_flat_code(str(row.get("commodity_code") or row.get("code") or ""))
        if not code:
            continue
        candidate_count += 1
        weight = _score_weight(row, rank)
        for name, length in levels.items():
            key = code[:length]
            distributions[name][key] = distributions[name].get(key, 0.0) + weight
    by_level: dict[str, Any] = {}
    for name, dist in distributions.items():
        entropy = _shannon_bits(list(dist.values()))
        max_entropy = math.log2(len(dist)) if len(dist) > 1 else 0.0
        by_level[name] = {
            "distinct": len(dist),
            "entropy_bits": round(entropy, 4),
            "normalized_entropy": round(entropy / max_entropy, 4) if max_entropy else 0.0,
            "top_values": [
                {"value": key, "weight": round(value, 6)}
                for key, value in sorted(dist.items(), key=lambda item: item[1], reverse=True)[:8]
            ],
        }
    return {
        "basis": "post_retrieval_code_prefix_shannon_entropy",
        "candidate_count": candidate_count,
        "k": len(subset),
        "levels": by_level,
    }


def _input_facet_entropy(candidates: list[dict[str, Any]], k: int = 100, max_facets: int = 12) -> dict[str, Any]:
    codes: list[str] = []
    for row in list(candidates or [])[: max(1, int(k or 1))]:
        code = _score_flat_code(str(row.get("commodity_code") or row.get("code") or ""))
        if code and code not in codes:
            codes.append(code)
    if not codes:
        return {"available": True, "basis": "kg.commodity_facets", "candidate_count": 0, "facets": []}
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(_tariff_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT commodity_code, facet_key, facet_value
                FROM kg.commodity_facets
                WHERE commodity_code = ANY(%s)
                  AND facet_key IS NOT NULL
                  AND facet_value IS NOT NULL
                  AND (use_scopes IS NULL
                       OR 'qa' = ANY(use_scopes)
                       OR 'classification' = ANY(use_scopes)
                       OR 'retrieval' = ANY(use_scopes))
                """,
                (codes,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        return {
            "available": False,
            "basis": "kg.commodity_facets",
            "candidate_count": len(codes),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "facets": [],
        }

    per_facet: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        code = _score_flat_code(str(row.get("commodity_code") or ""))
        key = str(row.get("facet_key") or "").strip()
        value = str(row.get("facet_value") or "").strip()
        if not code or not key or not value:
            continue
        per_facet.setdefault(key, {}).setdefault(value, set()).add(code)

    summaries: list[dict[str, Any]] = []
    total_candidates = max(len(codes), 1)
    for key, values in per_facet.items():
        covered = set().union(*values.values()) if values else set()
        if len(values) < 2 or len(covered) < 2:
            continue
        counts = {value: len(value_codes) for value, value_codes in values.items()}
        entropy = _shannon_bits([float(v) for v in counts.values()])
        max_entropy = math.log2(len(counts)) if len(counts) > 1 else 0.0
        coverage = len(covered) / total_candidates
        summaries.append({
            "facet_key": key,
            "coverage": round(coverage, 4),
            "covered_candidates": len(covered),
            "distinct_values": len(counts),
            "entropy_bits": round(entropy, 4),
            "normalized_entropy": round(entropy / max_entropy, 4) if max_entropy else 0.0,
            "values": [
                {"value": value, "candidate_count": count}
                for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]
            ],
        })
    summaries.sort(
        key=lambda row: (row["normalized_entropy"] * row["coverage"], row["entropy_bits"], row["covered_candidates"]),
        reverse=True,
    )
    return {
        "available": True,
        "basis": "kg.commodity_facets_candidate_distribution",
        "candidate_count": len(codes),
        "facets_considered": len(per_facet),
        "facets": summaries[:max_facets],
    }


@app.post("/api/scoring/input")
@app.post("/api/input/score")
def input_score(req: InputScoreRequest) -> dict:
    search = retrieval_search(
        RetrievalSearch(
            query=req.query,
            run_label=req.run_label,
            retrieval_limit=req.retrieval_limit,
            allow_spend=req.allow_spend,
        )
    )
    candidates = list(search.get("candidates") or [])
    limit = int(search.get("retrieval_limit") or req.retrieval_limit)
    response: dict[str, Any] = {
        "query": req.query,
        "processed_query": search.get("processed_query"),
        "rewrite": search.get("rewrite"),
        "experiment": search.get("experiment"),
        "retrieval_limit": limit,
        "provider_calls_used": bool(search.get("provider_calls_used")),
        "provider_call_types": search.get("provider_call_types") or [],
        "pre_retrieval": {
            "qpp_lexical_specificity": search.get("lexical_specificity"),
            "descriptiveness": {
                "available": False,
                "score": None,
                "basis": "kg.query_descriptiveness",
                "note": "LLM descriptiveness is batch/eval-gold oriented and is not run for arbitrary input by this no-spend endpoint.",
            },
        },
        "retrieval": {
            "candidate_count": len(candidates),
            "leg_counts": search.get("leg_counts") or {},
            "top_candidates": candidates[:10],
        },
        "post_retrieval": {
            "query_difficulty": search.get("query_difficulty"),
            "code_prefix_entropy": _input_code_prefix_entropy(candidates, limit),
            "facet_entropy": _input_facet_entropy(candidates, k=min(limit, 100)),
        },
    }
    if req.include_candidates:
        response["retrieval"]["candidates"] = candidates
    return response


@app.get("/api/evals/classification/gold-examples")
def classify_gold_examples(persona: str = "emu_ordinary", limit: int = 20) -> dict:
    if persona not in PERSONA_CHOICES:
        raise HTTPException(422, f"Unknown persona: {persona}")
    limit = max(1, min(int(limit or 20), 100))
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(_tariff_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_id, persona, query, expected_code
                FROM kg.eval_gold
                WHERE source_type = 'atar'
                  AND persona = %s
                ORDER BY id
                LIMIT %s
                """,
                (persona, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"persona": persona, "examples": rows}


def _load_gold_example(gold_id: int) -> tuple[dict[str, Any], str]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(_tariff_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_id, source_type, persona, query, expected_code
            FROM kg.eval_gold
            WHERE id = %s
            LIMIT 1
            """,
            (gold_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Gold example not found")
        gold = dict(row)
        cur.execute("SELECT body FROM kg.kg_edges WHERE id = %s LIMIT 1", (gold["source_id"],))
        oracle_row = cur.fetchone()
        oracle = str((oracle_row or {}).get("body") or "")
    return gold, oracle


@app.post("/api/evals/classification/trial")
def classify_trial(req: ClassifyTrial) -> dict:
    if not req.allow_spend:
        raise HTTPException(
            403,
            "Q&A emulator trials call classifier and trader-emulator models. Set allow_spend=true.",
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(400, "OPENAI_API_KEY is required for Q&A emulator trials.")
    if req.model not in _allowed_models():
        raise HTTPException(422, "Classifier model is not enabled for classification trials.")
    if req.simulator_model not in _allowed_models():
        raise HTTPException(422, "Trader emulator model is not enabled for classification trials.")

    if req.gold_id is not None:
        gold, oracle = _load_gold_example(req.gold_id)
    else:
        query = (req.query or "").strip()
        expected = (req.expected_code or "").strip()
        oracle = (req.oracle_text or "").strip()
        if not query or not expected:
            raise HTTPException(422, "gold_id or query+expected_code is required.")
        gold = {
            "id": 0,
            "source_id": "ad_hoc",
            "source_type": "manual",
            "persona": "ad_hoc",
            "query": query,
            "expected_code": expected,
        }

    if not oracle:
        raise HTTPException(422, "Q&A emulator trial requires an oracle ATAR/body text.")

    with _CLASSIFY_TRIAL_LOCK:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from classification_core import qa_loop
            from classification_core.run_classify_matrix import run_one_session
            from classification_core.run_eval import build_loo_map

            with psycopg.connect(_tariff_dsn(), row_factory=dict_row) as conn:
                loo_map = build_loo_map(conn, [gold]) if gold.get("source_type") == "atar" else {}

            old_simulator_model = qa_loop.SIMULATOR_MODEL
            with _temporary_env(
                {
                    "CLASSIFICATION_ALLOW_PROVIDER_CALLS": "1",
                    "CLASSIFY_LLM_MODEL": req.model,
                    "QA_SIMULATOR_MODEL": req.simulator_model,
                }
            ):
                try:
                    qa_loop.SIMULATOR_MODEL = req.simulator_model
                    result = asyncio.run(
                        run_one_session(
                            gold,
                            oracle,
                            strategy=req.strategy,
                            prompt_mode=req.prompt_mode,
                            augmentation=req.augmentation,
                            model=req.model,
                            candidate_limit=req.candidate_limit,
                            max_rounds=req.max_rounds,
                            loo_map=loo_map,
                        )
                    )
                finally:
                    qa_loop.SIMULATOR_MODEL = old_simulator_model
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    final_set = result.get("final_set") or []
    trace = result.get("trace_json") or {}
    trace_rounds = trace.get("rounds") if isinstance(trace, dict) else []
    return {
        "gold": gold,
        "oracle_present": bool(oracle),
        "strategy": req.strategy,
        "prompt_mode": req.prompt_mode,
        "augmentation": req.augmentation,
        "model": req.model,
        "simulator_model": req.simulator_model,
        "candidate_limit": req.candidate_limit,
        "max_rounds": req.max_rounds,
        "summary": {
            "final_mode": result.get("final_mode"),
            "round_count": result.get("rounds"),
            "rounds": trace_rounds if isinstance(trace_rounds, list) else [],
            "final_top1": result.get("final_top1"),
            "final_set": final_set,
            "gold_in_final_set": result.get("gold_in_final_set"),
            "gold_rank": result.get("gold_rank"),
            "survivor_set_size": result.get("survivor_set_size"),
            "classify_calls": result.get("classify_calls"),
            "simulator_calls": result.get("simulator_calls"),
            "est_cost_usd": result.get("est_cost_usd"),
            "latency_seconds": result.get("latency_seconds"),
            "persisted_session_facts": result.get("persisted_session_facts") or (trace.get("persisted_session_facts") if isinstance(trace, dict) else None),
        },
        "trace": trace,
    }


@app.post("/api/jobs")
def create_job(req: JobCreate) -> dict:
    _validate_request(req)
    job_id = uuid.uuid4().hex[:12]
    log_path = JOBS_DIR / f"{job_id}.log"
    cmd = _build_command(req)
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(PRODUCT_BACKEND))
    env["CLASSIFICATION_ALLOW_PROVIDER_CALLS"] = "1"
    env["CLASSIFY_LLM_MODEL"] = req.model
    env["QA_SIMULATOR_MODEL"] = req.simulator_model
    estimated_sessions = _estimated_sessions(req)
    estimated_cost = _estimated_cost_usd(req)

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=PRODUCT_BACKEND,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    with _PROCESS_LOCK:
        _PROCESSES[job_id] = process
    threading.Thread(target=_watch_process, args=(job_id, process), daemon=True).start()

    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs
              (id, created_at, updated_at, status, pid, returncode, command_json, request_json,
               log_path, error, runner_instance, estimated_sessions, estimated_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                now,
                now,
                "running",
                process.pid,
                None,
                json.dumps(cmd),
                json.dumps(req.model_dump(mode="json")),
                str(log_path),
                None,
                _RUNNER_INSTANCE_ID,
                estimated_sessions,
                estimated_cost,
            ),
        )
    return {
        "job_id": job_id,
        "status": "running",
        "pid": process.pid,
        "log_url": f"/api/jobs/{job_id}/log",
        "estimated_sessions": estimated_sessions,
        "estimated_cost_usd": estimated_cost,
    }


@app.get("/api/jobs")
def list_jobs() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"jobs": [_refresh_job(row) for row in rows]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    return _refresh_job(row)


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job["status"] != "running" or not job["pid"]:
        return job
    with _PROCESS_LOCK:
        process = _PROCESSES.get(job_id)
    if process is None:
        _record_exit(job_id, -1, status="unknown_exit")
        return get_job(job_id)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
            ("stopping", time.time(), job_id),
        )
    return get_job(job_id)


@app.get("/api/jobs/{job_id}/log")
def get_log(job_id: str, tail: int = 4000) -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    job = _refresh_job(row, include_internal=True)
    path = Path(job["log_path"])
    if not path.exists():
        return {"job_id": job_id, "log": ""}
    max_bytes = max(1, min(tail, 50000))
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        text = fh.read(max_bytes).decode("utf-8", errors="replace")
    return {"job_id": job_id, "log": text}


@app.get("/eval/classify-matrix", response_class=HTMLResponse)
def classify_matrix() -> str:
    from classification_core.classify_matrix_view import eval_classify_matrix

    return eval_classify_matrix()




@app.get("/eval/e2e-matrix", response_class=HTMLResponse)
def e2e_matrix() -> str:
    return _render_live_e2e_matrix(qa_only=False)


@app.get("/eval/qa-matrix", response_class=HTMLResponse)
def qa_matrix() -> str:
    return _render_live_e2e_matrix(qa_only=True)


def _render_live_e2e_matrix(*, qa_only: bool) -> str:
    from html import escape as _html_escape

    def esc(value) -> str:
        return _html_escape(str(value if value is not None else ""), quote=True)

    def pct(num, den) -> str:
        try:
            den = int(den or 0)
            return "-" if den <= 0 else f"{(100 * int(num or 0) / den):.1f}%"
        except Exception:
            return "-"

    def num(value) -> str:
        try:
            return f"{float(value):.2f}"
        except Exception:
            return "-"

    def prompt_label(row: dict) -> str:
        cfg = row.get("config_json") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        prompt = cfg.get("staging_prompt_mode") or cfg.get("prompt_mode") or "-"
        effort = cfg.get("classify_reasoning_effort") or "-"
        policy = ", ".join(cfg.get("policy_eval") or []) if isinstance(cfg.get("policy_eval"), list) else ""
        parts = [f"prompt: {prompt}", f"reasoning: {effort}"]
        if policy:
            parts.append(f"policy: {policy}")
        return " | ".join(parts)

    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(_tariff_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('kg.e2e_eval_runs') AS runs_table")
            if not (cur.fetchone() or {}).get("runs_table"):
                rows = []
            else:
                cur.execute(
                    """
                    SELECT r.id,
                           r.run_label,
                           r.retrieval_run_label,
                           r.question_mode,
                           r.answerer,
                           r.question_model,
                           r.simulator_model,
                           r.pair_limit,
                           r.persona_count,
                           r.input_count,
                           r.retrieval_limit,
                           r.hydrate_limit,
                           r.max_rounds,
                           r.allow_spend,
                           r.config_json,
                           r.started_at,
                           r.finished_at,
                           r.n_inputs,
                           r.initial_gold_in_retrieval,
                           r.gold_kept,
                           r.gold_top1_after_qa,
                           r.avg_initial_rank,
                           r.avg_post_qa_rank,
                           r.avg_rounds,
                           r.avg_active_count,
                           r.provider_calls_used,
                           r.errors,
                           count(res.id)::int AS result_rows,
                           count(res.id) FILTER (WHERE res.final_state::text LIKE '%From retrieval%')::int AS fallback_rows,
                           coalesce(sum(
                               CASE
                                 WHEN jsonb_typeof(res.final_state->'fallback_to_retrieval_rounds') = 'number'
                                 THEN (res.final_state->>'fallback_to_retrieval_rounds')::int
                                 ELSE 0
                               END
                           ), 0)::int AS fallback_rounds
                    FROM kg.e2e_eval_runs r
                    LEFT JOIN kg.e2e_eval_results res ON res.run_id = r.id
                    GROUP BY r.id
                    ORDER BY r.id DESC
                    LIMIT 160
                    """
                )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        rows = []
        error = f"{type(exc).__name__}: {exc}"
    else:
        error = ""

    title = "Q&A Matrix" if qa_only else "End-to-End Journey Matrix"
    sub = (
        "Question-mode comparison after retrieval. Keep/top1 rates are conditioned on gold being present in the retrieved shortlist."
        if qa_only
        else "Full journey view from retrieval through Q&A and final result-list preservation/rank metrics."
    )
    if error:
        body = f"<div class='empty'>Could not load matrix: {esc(error)}</div>"
    elif not rows:
        body = "<div class='empty'>No E2E/Q&A runs yet.</div>"
    else:
        row_html = []
        for r in rows:
            n = r.get("n_inputs") or r.get("input_count") or r.get("result_rows") or 0
            eligible = r.get("initial_gold_in_retrieval") or 0
            denom = eligible if qa_only else n
            fallback = int(r.get("fallback_rows") or 0)
            fallback_rounds = int(r.get("fallback_rounds") or 0)
            fallback_cls = "bad" if fallback else "muted"
            done = bool(r.get("finished_at"))
            status_cls = "good" if done and not int(r.get("errors") or 0) else ("warn" if not done else "bad")
            row_html.append(
                f"""
                <tr>
                  <td class='id'>#{esc(r.get('id'))}</td>
                  <td>
                    <b>{esc(r.get('run_label'))}</b>
                    <br><span>{esc(r.get('retrieval_run_label'))}</span>
                    <br><code>{esc(prompt_label(r))}</code>
                  </td>
                  <td>{esc(r.get('question_mode'))}<br><span>{esc(r.get('answerer'))}</span></td>
                  <td>{esc(r.get('question_model') or '-')}<br><span>sim: {esc(r.get('simulator_model') or '-')}</span></td>
                  <td>{esc(n)}<br><span>{esc(eligible)} eligible</span></td>
                  <td>{pct(r.get('initial_gold_in_retrieval'), n)}</td>
                  <td>{pct(r.get('gold_kept'), denom)}</td>
                  <td>{pct(r.get('gold_top1_after_qa'), denom)}</td>
                  <td>{num(r.get('avg_initial_rank'))} -> {num(r.get('avg_post_qa_rank'))}</td>
                  <td>{num(r.get('avg_rounds'))}<br><span>active {num(r.get('avg_active_count'))}</span></td>
                  <td>{esc(r.get('provider_calls_used') or 0)}</td>
                  <td class='{fallback_cls}'>{esc(fallback)} rows<br><span>{esc(fallback_rounds)} rounds</span></td>
                  <td class='{status_cls}'>{'done' if done else 'running'}<br><span>{esc(r.get('errors') or 0)} errors</span></td>
                </tr>
                """
            )
        body = f"""
        <table>
          <thead><tr>
            <th>Run</th><th>Config</th><th>Question mode</th><th>Models</th><th>N</th>
            <th>Gold in retrieval</th><th>Gold kept</th><th>Gold top1</th>
            <th>Avg rank</th><th>Rounds / active</th><th>Calls</th><th>Fallback</th><th>Status</th>
          </tr></thead>
          <tbody>{''.join(row_html)}</tbody>
        </table>
        """
    return f"""
    <!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title>
    <style>
      body {{ margin:0; background:#070b13; color:#e5e7eb; font-family:Inter,system-ui,sans-serif; padding:24px; }}
      h1 {{ margin:0 0 6px; font-size:24px; }}
      .sub, span {{ color:#94a3b8; }}
      table {{ width:100%; border-collapse:separate; border-spacing:0; margin-top:18px; font-size:13px; }}
      th,td {{ border-bottom:1px solid #243044; padding:10px; text-align:left; vertical-align:top; }}
      th {{ background:#101827; color:#bfdbfe; position:sticky; top:0; z-index:1; }}
      tbody tr:nth-child(even) td {{ background:#0b1220; }}
      tbody tr:hover td {{ background:#111a2b; }}
      .id {{ color:#93c5fd; font-weight:800; white-space:nowrap; }}
      code {{ color:#c4b5fd; font-size:11px; }}
      .good {{ color:#bbf7d0; }}
      .warn {{ color:#fde68a; }}
      .bad {{ color:#fca5a5; }}
      .muted {{ color:#94a3b8; }}
      .empty {{ border:1px solid #263243; background:#0b1220; padding:18px; margin-top:18px; }}
    </style></head><body>
      <h1>{esc(title)}</h1>
      <div class='sub'>{esc(sub)}</div>
      {body}
    </body></html>
    """


@app.get("/eval/matrix")
def retrieval_matrix() -> Response:
    matrix_path = MATRIX_DIR / "retrieval_matrix.html"
    if not matrix_path.exists():
        raise HTTPException(404, "Exported retrieval matrix snapshot is missing.")
    html_text = matrix_path.read_text(encoding="utf-8")
    try:
        from experiment_retrieval import matrix_input_quality_html
        if "matrix-input-quality" not in html_text:
            html_text = html_text.replace("</body>", matrix_input_quality_html() + "</body>")
    except Exception as exc:
        html_text = html_text.replace("</body>", f"<section id='matrix-input-quality' style='margin-top:28px;color:#fca5a5'>Input-quality strip unavailable: {type(exc).__name__}</section></body>")
    return Response(html_text, media_type="text/html")


@app.get("/eval/matrix.csv")
def retrieval_matrix_csv() -> FileResponse:
    csv_path = MATRIX_DIR / "retrieval_matrix.csv"
    if not csv_path.exists():
        raise HTTPException(404, "Exported retrieval matrix CSV is missing.")
    return FileResponse(csv_path, media_type="text/csv", filename="retrieval_matrix.csv")


def _react_index() -> FileResponse:
    if not PRODUCT_FRONTEND_INDEX.exists():
        raise HTTPException(
            404,
            "React app bundle is missing. Build the frontend or rebuild the deployable image.",
        )
    return FileResponse(PRODUCT_FRONTEND_INDEX, media_type="text/html")


def index() -> str:
    return r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Search Evaluation Suite</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #070b13;
      color: #e5e7eb;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: #070b13; color: #e5e7eb; }
    main { width: min(1880px, calc(100vw - 32px)); margin: 0 auto; padding: 24px 0 40px; }
    header {
      display: flex; justify-content: space-between; gap: 20px; align-items: flex-start;
      border: 1px solid #263243; background: #0e1624; padding: 22px;
    }
    h1, h2, h3 { margin: 0; letter-spacing: 0; }
    h1 { font-size: 28px; }
    h2 { font-size: 20px; }
    h3 { font-size: 15px; }
    p { line-height: 1.5; }
    a { color: #60a5fa; text-decoration: none; }
    code { color: #93c5fd; }
    .muted { color: #94a3b8; }
    .sub { margin: 8px 0 0; color: #a7b1c2; max-width: 980px; }
    .cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; min-width: 560px; }
    .card { border: 1px solid #334155; background: #0b1220; padding: 14px; min-height: 86px; }
    .label { color: #94a3b8; font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: 1.4px; }
    .value { margin-top: 8px; font-size: 22px; font-weight: 900; }
    .tabs { display: flex; gap: 8px; margin: 16px 0; flex-wrap: wrap; }
    .tab-button {
      border: 1px solid #334155; background: #0b1220; color: #dbeafe;
      padding: 10px 13px; font-weight: 900; cursor: pointer;
    }
    .tab-button.active { background: #2563eb; border-color: #60a5fa; color: #fff; }
    section.panel { display: none; border: 1px solid #263243; background: #0e1624; padding: 18px; }
    section.panel.active { display: block; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .grid.two { grid-template-columns: minmax(360px, 0.7fr) minmax(520px, 1.3fr); align-items: start; }
    label { display: grid; gap: 6px; font-weight: 800; color: #cbd5e1; }
    input, select, textarea {
      width: 100%; background: #050913; color: #f8fafc; border: 1px solid #475569;
      padding: 10px; font: inherit; min-height: 42px;
    }
    textarea { min-height: 86px; resize: vertical; }
    button {
      background: #2563eb; color: white; border: 1px solid #60a5fa; padding: 11px 16px;
      font-weight: 900; cursor: pointer; min-height: 42px;
    }
    button.secondary { background: #334155; border-color: #475569; }
    button.danger { background: #991b1b; border-color: #ef4444; }
    details { border: 1px solid #334155; padding: 12px; background: #0b1220; }
    summary { cursor: pointer; font-weight: 900; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .stack { display: grid; gap: 12px; }
    .result-list { display: grid; gap: 10px; }
    .candidate, .job, .experiment {
      border: 1px solid #334155; padding: 12px; background: #0b1220;
    }
    .candidate.good { border-color: #10b981; box-shadow: inset 4px 0 0 #10b981; }
    .code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 18px; font-weight: 900; color: #93c5fd; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px;
      background: #1f2937; color: #e5e7eb; font-size: 12px; font-weight: 900;
      text-transform: uppercase; letter-spacing: 0.8px;
    }
    .badge.ok { background: #064e3b; color: #a7f3d0; }
    .badge.warn { background: #713f12; color: #fde68a; }
    .badge.bad { background: #7f1d1d; color: #fecaca; }
    iframe {
      width: 100%; height: calc(100vh - 190px); min-height: 680px;
      border: 1px solid #334155; background: #070b13;
    }
    pre {
      white-space: pre-wrap; max-height: 520px; overflow: auto; background: #020617;
      border: 1px solid #1e293b; padding: 12px;
    }
    .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
    @media (max-width: 1100px) {
      header, .grid.two { display: grid; }
      .cards, .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); min-width: 0; }
    }
    @media (max-width: 700px) {
      main { width: calc(100vw - 20px); padding-top: 10px; }
      .cards, .grid, .grid.two { grid-template-columns: 1fr; }
      iframe { min-height: 560px; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <div class="label">Experiment workspace</div>
      <h1>AI Search Evaluation Suite</h1>
      <p class="sub">Product suite for tariff knowledge, retrieval trials, classification Q&amp;A eval jobs, job logs, and live classification matrices.</p>
    </div>
    <div class="cards">
      <div class="card"><div class="label">Runner</div><div id="runner-status" class="value">...</div></div>
      <div class="card"><div class="label">OpenAI key</div><div id="key-status" class="value">...</div></div>
      <div class="card"><div class="label">Experiments</div><div id="experiment-count" class="value">...</div></div>
      <div class="card"><div class="label">Jobs</div><div id="job-count" class="value">...</div></div>
    </div>
  </header>

  <nav class="tabs">
    <button class="tab-button active" data-tab="overview" onclick="showTab('overview')">Overview</button>
    <button class="tab-button" data-tab="retrieval-matrix" onclick="showTab('retrieval-matrix')">Retrieval Matrix</button>
    <button class="tab-button" data-tab="try-search" onclick="showTab('try-search')">Try Search</button>
    <button class="tab-button" data-tab="qa-harness" onclick="showTab('qa-harness')">Q&amp;A Harness</button>
    <button class="tab-button" data-tab="classification-matrix" onclick="showTab('classification-matrix')">Q&amp;A Matrix</button>
    <button class="tab-button" data-tab="e2e-matrix" onclick="showTab('e2e-matrix')">E2E Matrix</button>
    <button class="tab-button" data-tab="jobs" onclick="showTab('jobs')">Jobs &amp; Logs</button>
  </nav>

  <section id="overview" class="panel active">
    <div class="grid two">
      <div class="stack">
        <h2>Current shape</h2>
        <p class="muted">Paid jobs and provider-backed actions remain gated. Operator tools, logs, matrices, and product modules are served by this app.</p>
        <div id="top-experiment" class="experiment muted">Loading top experiment...</div>
        <div class="row">
          <a href="/eval/matrix" target="_blank">Open retrieval matrix</a>
          <a href="/eval/classify-matrix" target="_blank">Open Q&amp;A matrix</a>
          <a href="/eval/e2e-matrix" target="_blank">Open E2E matrix</a>
          <a href="/eval/matrix.csv">Download matrix CSV</a>
        </div>
      </div>
      <div class="stack">
        <h2>Experiment catalogue</h2>
        <div id="catalog" class="stack"></div>
      </div>
    </div>
  </section>

  <section id="retrieval-matrix" class="panel">
    <div class="toolbar">
      <div>
        <h2>Retrieval Experiment Matrix</h2>
        <p class="muted">Static matrix snapshot bundled with the deploy app. Rows are ranked experiment configs; cells are code-macro recall by persona.</p>
      </div>
      <div class="row"><a href="/eval/matrix" target="_blank">Open full tab</a><a href="/eval/matrix.csv">CSV</a></div>
    </div>
    <iframe src="/eval/matrix" title="Retrieval experiment matrix"></iframe>
  </section>

  <section id="try-search" class="panel">
    <div class="grid two">
      <div class="stack">
        <h2>Try A Fresh Search</h2>
        <label>Goods description<textarea id="search-query">footwear with rubber soles</textarea></label>
        <label>Expected commodity code (optional)<input id="expected-code" placeholder="10 digit code, for hit/rank checks"></label>
        <label>Experiment<select id="experiment-select"></select></label>
        <label>Retrieval limit<input id="retrieval-limit" type="number" min="10" max="500" value="100"></label>
        <label>Provider calls<select id="allow-search-spend"><option value="false">No - deterministic/non-vector only</option><option value="true">Yes - allow embedding call</option></select></label>
        <p id="search-mode-hint" class="muted"></p>
        <div class="row"><button onclick="runSearch()">Run search</button><span id="search-status" class="muted"></span></div>
      </div>
      <div class="stack">
        <h2>Search Result</h2>
        <div id="search-summary" class="muted">Run a search to see ranked candidates and hit metrics.</div>
        <div id="search-results" class="result-list"></div>
      </div>
    </div>
  </section>

  <section id="qa-harness" class="panel">
    <div class="grid two">
      <div class="stack">
        <h2>Run One Q&amp;A Emulator Trial</h2>
        <p class="muted">Uses <code>kg.eval_gold</code> ATAR persona queries and the ATAR body as the oracle for the trader LLM emulator. This is the same harness used by long-running matrix jobs.</p>
        <label>Persona<select id="qa-persona" onchange="loadClassifyExamples()">
          <option value="emu_ordinary">L2 emulator ordinary</option>
          <option value="emu_generic">L1 emulator generic</option>
          <option value="emu_specific">L3 emulator specific</option>
          <option value="naive_vague">naive vague</option>
          <option value="naive_branded">naive branded</option>
          <option value="naive_specific">naive specific</option>
          <option value="original">ATAR original</option>
        </select></label>
        <label>Gold query<select id="qa-gold"></select></label>
        <label>Strategy<select id="qa-strategy"><option>converge</option><option>eliminate</option></select></label>
        <label>Prompt mode<select id="qa-prompt-mode"><option>baseline</option><option>rule_reasoning</option><option>exclusion_aware</option><option>gir_citation</option><option>self_verify</option></select></label>
        <label>Augmentation<select id="qa-augmentation"><option>facts+kg</option><option>facts</option><option>kg</option><option>none</option></select></label>
        <label>Classifier model<select id="qa-model"><option>gpt-5-mini</option><option>gpt-5-nano</option><option>gpt-5.5</option></select></label>
        <label>Trader emulator model<select id="qa-simulator-model"><option>gpt-5-mini</option><option>gpt-5-nano</option><option>gpt-5.5</option></select></label>
        <label>Candidate limit<input id="qa-candidate-limit" type="number" min="5" max="200" value="40"></label>
        <label>Max rounds<input id="qa-max-rounds" type="number" min="1" max="12" value="5"></label>
        <label>Provider calls<select id="qa-allow-spend"><option value="false">No - block paid emulator run</option><option value="true">Yes - run classifier + trader emulator</option></select></label>
        <div class="row"><button onclick="runClassifyTrial()">Run Q&amp;A trial</button><span id="qa-status" class="muted"></span></div>
      </div>
      <div class="stack">
        <h2>Q&amp;A Trial Result</h2>
        <div id="qa-summary" class="muted">Pick a gold query and run one emulator session.</div>
        <div id="qa-rounds" class="result-list"></div>
      </div>
    </div>
  </section>

  <section id="classification-matrix" class="panel">
    <div class="toolbar">
      <div>
        <h2>Classification Q&amp;A Matrix</h2>
        <p class="muted">Live from <code>kg.classify_runs</code>. Long-running jobs populate this matrix.</p>
      </div>
      <a href="/eval/classify-matrix" target="_blank">Open full tab</a>
    </div>
    <iframe src="/eval/classify-matrix" title="Classification matrix"></iframe>
  </section>


  <section id="e2e-matrix" class="panel">
    <div class="toolbar">
      <div>
        <h2>End-to-End Journey Matrix</h2>
        <p class="muted">Live from <code>kg.e2e_eval_runs</code>. Rows are fixed-set input -&gt; retrieval shortlist -&gt; hydrated Q&amp;A -&gt; final result-list evaluations.</p>
      </div>
      <a href="/eval/e2e-matrix" target="_blank">Open full tab</a>
    </div>
    <iframe src="/eval/e2e-matrix" title="E2E retrieval and Q&amp;A matrix"></iframe>
  </section>

  <section id="jobs" class="panel">
    <div class="toolbar">
      <div>
        <h2>Long-Running Q&amp;A Eval Jobs</h2>
        <p class="muted">Paid jobs require <code>OPENAI_API_KEY</code> and explicit allow-spend. Server-side caps still apply.</p>
      </div>
      <button class="secondary" onclick="loadJobs()">Refresh</button>
    </div>
    <details open>
      <summary>Start job</summary>
      <form id="job-form" class="grid" style="margin-top:12px">
        <label>Run label<input name="run_label" value="demo_classify_eval"></label>
        <label>Strategy<select name="strategy"><option>converge</option><option>eliminate</option></select></label>
        <label>Prompt mode<select name="prompt_mode"><option>baseline</option><option>rule_reasoning</option><option>exclusion_aware</option><option>gir_citation</option><option>self_verify</option></select></label>
        <label>Augmentation<select name="augmentation"><option>facts+kg</option><option>facts</option><option>kg</option><option>none</option></select></label>
        <label>Classifier model<input name="model" value="gpt-5-mini"></label>
        <label>Trader emulator model<input name="simulator_model" value="gpt-5-mini"></label>
        <label>Candidate limit<input name="candidate_limit" type="number" value="40"></label>
        <label>Personas<input name="personas" value="naive_vague"></label>
        <label>Limit per persona<input name="limit" type="number" value="5"></label>
        <label>Concurrency<input name="concurrency" type="number" value="2"></label>
        <label>Max rounds<input name="max_rounds" type="number" value="5"></label>
        <label>Sweep<select name="sweep"><option value="false">No</option><option value="true">Yes</option></select></label>
        <label>Allow spend<select name="allow_spend"><option value="false">No</option><option value="true">Yes</option></select></label>
      </form>
      <div class="row" style="margin-top:12px"><button onclick="startJob()">Start classification job</button><span id="start-status" class="muted"></span></div>
    </details>
    <div class="grid two" style="margin-top:14px">
      <div class="stack">
        <h3>Jobs</h3>
        <div id="job-list" class="stack"></div>
      </div>
      <div class="stack">
        <h3>Log</h3>
        <pre id="log">Select a job log.</pre>
      </div>
    </div>
  </section>
</main>
<script>
let experiments = [];
let goldExamples = [];
let health = {};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function experimentStatus(row) {
  if (row.deploy_requires_rewrite) return {text: 'live rewrite', cls: 'warn'};
  if (row.deploy_runnable || row.runnable) return {text: 'runnable', cls: 'ok'};
  return {text: 'catalog only', cls: 'warn'};
}

function showTab(id) {
  document.querySelectorAll('.panel').forEach(panel => panel.classList.toggle('active', panel.id === id));
  document.querySelectorAll('.tab-button').forEach(button => button.classList.toggle('active', button.dataset.tab === id));
}

async function api(path, init) {
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

async function loadHealth() {
  health = await api('/api/health');
  document.getElementById('runner-status').textContent = health.status || 'unknown';
  document.getElementById('key-status').textContent = health.openai_key_present ? 'loaded' : 'missing';
  document.getElementById('key-status').className = health.openai_key_present ? 'value' : 'value muted';
}

async function loadExperiments() {
  const data = await api('/api/retrieval/experiments');
  experiments = data.experiments || [];
  document.getElementById('experiment-count').textContent = experiments.length;
  const select = document.getElementById('experiment-select');
  const providerSelect = document.getElementById('allow-search-spend');
  const liveExperiments = experiments.filter(row => row.deploy_runnable || row.runnable);
  select.innerHTML = liveExperiments.map(row => {
    const recall = Number(row.headline_recall_pct || 0).toFixed(1);
    const suffix = row.deploy_requires_rewrite ? ' · rewrite' : '';
    return `<option value="${esc(row.run_label)}">${esc(row.title)} - ${recall}%${suffix}</option>`;
  }).join('');
  const baseline = liveExperiments.find(row => row.run_label === 'baseline_fts_only');
  const topRunnable = liveExperiments[0];
  if (baseline) {
    select.value = baseline.run_label;
    providerSelect.value = 'false';
    document.getElementById('search-mode-hint').textContent = health.openai_key_present
      ? 'Defaulting to the deterministic keyword baseline. To spend on rewrite or semantic retrieval, choose that experiment and set provider calls to Yes.'
      : 'No provider key is loaded, so fresh search defaults to the weak deterministic keyword baseline.';
  } else if (topRunnable) {
    select.value = topRunnable.run_label;
    providerSelect.value = 'false';
    document.getElementById('search-mode-hint').textContent = 'Provider calls are off by default. Enable them only when you intend to run rewrite or semantic retrieval.';
  }

  const top = await api('/api/retrieval/top-experiment').catch(() => null);
  if (top) {
    document.getElementById('top-experiment').innerHTML = `
      <div class="label">Top matrix config</div>
      <h3>${esc(top.title)}</h3>
      <p>${esc(top.description)}</p>
      <div class="row">
        <span class="badge ok">${Number(top.headline_recall_pct || 0).toFixed(1)}% recall@100</span>
        <span class="badge">${esc(top.run_label)}</span>
        <span class="badge ${experimentStatus(top).cls}">${experimentStatus(top).text}</span>
      </div>`;
  }
  document.getElementById('catalog').innerHTML = experiments.slice(0, 8).map(row => `
    <div class="experiment">
      <div class="row">
        <strong>${esc(row.title)}</strong>
        <span class="badge">${Number(row.headline_recall_pct || 0).toFixed(1)}%</span>
        <span class="badge ${experimentStatus(row).cls}">${experimentStatus(row).text}</span>
      </div>
      <div class="muted">${esc(row.run_label)} · ${esc(row.description)}</div>
    </div>`).join('');
}

function formPayload() {
  const form = new FormData(document.getElementById('job-form'));
  return {
    run_label: form.get('run_label'),
    strategy: form.get('strategy'),
    prompt_mode: form.get('prompt_mode'),
    augmentation: form.get('augmentation'),
    model: form.get('model'),
    simulator_model: form.get('simulator_model'),
    candidate_limit: Number(form.get('candidate_limit')),
    personas: String(form.get('personas')).split(',').map(s => s.trim()).filter(Boolean),
    limit: form.get('limit') ? Number(form.get('limit')) : null,
    concurrency: Number(form.get('concurrency')),
    max_rounds: Number(form.get('max_rounds')),
    sweep: form.get('sweep') === 'true',
    allow_spend: form.get('allow_spend') === 'true',
  };
}
async function startJob() {
  const status = document.getElementById('start-status');
  status.textContent = 'Starting...';
  try {
    const data = await api('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(formPayload())});
    status.textContent = `Started ${data.job_id}`;
    await loadJobs();
  } catch (err) {
    status.textContent = err.message;
  }
}
async function loadJobs() {
  const data = await api('/api/jobs');
  document.getElementById('job-count').textContent = data.jobs.length;
  document.getElementById('job-list').innerHTML = data.jobs.map(job => `
    <div class="job">
      <div class="row"><strong>${esc(job.id)}</strong><span class="badge">${esc(job.status)}</span><span class="muted">pid ${esc(job.pid || '-')}</span></div>
      <div class="muted">${esc(job.request.run_label)} · ${esc(job.request.strategy)} · ${esc(job.request.prompt_mode)} · ${esc(job.request.augmentation)} · classifier ${esc(job.request.model)} · trader ${esc(job.request.simulator_model || 'gpt-5-mini')}</div>
      <div class="row">
        <button class="secondary" onclick="loadLog('${job.id}')">Log</button>
        <button class="danger" onclick="stopJob('${job.id}')">Stop</button>
      </div>
    </div>
  `).join('') || '<p class="muted">No jobs yet.</p>';
}

async function loadLog(id) {
  const data = await api(`/api/jobs/${id}/log`);
  document.getElementById('log').textContent = data.log || 'No log output yet.';
}
async function stopJob(id) {
  await api(`/api/jobs/${id}/stop`, {method:'POST'});
  await loadJobs();
}

async function runSearch() {
  const status = document.getElementById('search-status');
  const summary = document.getElementById('search-summary');
  const results = document.getElementById('search-results');
  status.textContent = 'Running...';
  results.innerHTML = '';
  try {
    const payload = {
      query: document.getElementById('search-query').value,
      expected_code: document.getElementById('expected-code').value || null,
      run_label: document.getElementById('experiment-select').value,
      retrieval_limit: Number(document.getElementById('retrieval-limit').value || 100),
      allow_spend: document.getElementById('allow-search-spend').value === 'true',
    };
    const data = await api('/api/retrieval/search', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    status.textContent = 'Done';
    const rankText = data.expected_code_normalized
      ? (data.rank ? `Expected code rank ${data.rank}` : 'Expected code not found')
      : 'No expected code supplied';
    summary.innerHTML = `
      <div class="row">
        <span class="badge ${data.hit_at_100 ? 'ok' : (data.expected_code_normalized ? 'bad' : '')}">${esc(rankText)}</span>
        <span class="badge">${esc(data.experiment.run_label)}</span>
        <span class="badge ${data.provider_calls_used ? 'warn' : 'ok'}">${data.provider_calls_used ? 'provider call' : 'local DB only'}</span>
      </div>
      ${data.rewrite ? `<p class="muted">Rewrite (${esc(data.rewrite.model)} / ${esc(data.rewrite.prompt_variant)}): ${esc(data.processed_query)}</p>` : ''}
      <p class="muted">Leg counts: ${esc(JSON.stringify(data.leg_counts))}</p>`;
    results.innerHTML = data.top_candidates.length ? data.top_candidates.map(row => `
      <div class="candidate ${data.expected_code_normalized && String(row.commodity_code).padEnd(10, '0').slice(0,10) === data.expected_code_normalized ? 'good' : ''}">
        <div class="row">
          <span class="badge">#${esc(row.rank)}</span>
          <span class="code">${esc(row.commodity_code)}</span>
          <span class="badge">${esc((row.sources || [row.source || 'unknown']).join(' + '))}</span>
        </div>
        <p>${esc(row.description || '')}</p>
      </div>`).join('') : '<p class="muted">No candidates returned for this setup.</p>';
  } catch (err) {
    status.textContent = err.message;
    summary.textContent = err.message;
  }
}

async function loadClassifyExamples() {
  const persona = document.getElementById('qa-persona').value;
  const select = document.getElementById('qa-gold');
  select.innerHTML = '<option>Loading...</option>';
  try {
    const data = await api(`/api/evals/classification/gold-examples?persona=${encodeURIComponent(persona)}&limit=50`);
    goldExamples = data.examples || [];
    select.innerHTML = goldExamples.map(row => {
      const label = `${row.id} · ${row.expected_code} · ${row.query}`;
      return `<option value="${esc(row.id)}">${esc(label.slice(0, 180))}</option>`;
    }).join('') || '<option value="">No gold examples found</option>';
  } catch (err) {
    select.innerHTML = '<option value="">Could not load examples</option>';
    document.getElementById('qa-summary').textContent = err.message;
  }
}

async function runClassifyTrial() {
  const status = document.getElementById('qa-status');
  const summary = document.getElementById('qa-summary');
  const rounds = document.getElementById('qa-rounds');
  status.textContent = 'Running...';
  rounds.innerHTML = '';
  try {
    const payload = {
      gold_id: Number(document.getElementById('qa-gold').value),
      strategy: document.getElementById('qa-strategy').value,
      prompt_mode: document.getElementById('qa-prompt-mode').value,
      augmentation: document.getElementById('qa-augmentation').value,
      model: document.getElementById('qa-model').value,
      simulator_model: document.getElementById('qa-simulator-model').value,
      candidate_limit: Number(document.getElementById('qa-candidate-limit').value || 40),
      max_rounds: Number(document.getElementById('qa-max-rounds').value || 5),
      allow_spend: document.getElementById('qa-allow-spend').value === 'true',
    };
    if (!payload.gold_id) throw new Error('Pick a gold query first.');
    const data = await api('/api/evals/classification/trial', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    status.textContent = 'Done';
    const s = data.summary || {};
    const traceRounds = Array.isArray(s.rounds)
      ? s.rounds
      : (Array.isArray((data.trace || {}).rounds) ? data.trace.rounds : []);
    summary.innerHTML = `
      <div class="row">
        <span class="badge ${s.gold_in_final_set ? 'ok' : 'bad'}">${s.gold_in_final_set ? 'gold retained' : 'gold missed'}</span>
        <span class="badge">rank ${esc(s.gold_rank || '-')}</span>
        <span class="badge">rounds ${esc(s.round_count ?? traceRounds.length)}</span>
        <span class="badge">mode ${esc(s.final_mode || '-')}</span>
        <span class="badge warn">$${esc(s.est_cost_usd || 0)} est</span>
      </div>
      <p class="muted">Expected ${esc(data.gold.expected_code)} for: ${esc(data.gold.query)}</p>
      <p class="muted">Final set: ${esc((s.final_set || []).join(', ') || 'none')}</p>`;
    rounds.innerHTML = traceRounds.map(r => `
      <div class="candidate">
        <div class="row"><span class="badge">Round ${esc(r.round_number)}</span><span class="badge">${esc(r.mode)}</span><span class="badge">${esc(r.answer_source || '')}</span></div>
        ${r.question ? `<p><strong>Q:</strong> ${esc(r.question)}</p>` : ''}
        ${r.chosen ? `<p><strong>A:</strong> ${esc(r.chosen)}</p>` : ''}
        ${Array.isArray(r.options) ? `<p class="muted">Options: ${esc(r.options.join(' · '))}</p>` : ''}
        ${Array.isArray(r.answers) ? `<p class="muted">Answers: ${esc(r.answers.map(a => a.commodity_code).join(', '))}</p>` : ''}
        ${Array.isArray(r.candidates_top5) ? `<p class="muted">Top candidates: ${esc(r.candidates_top5.map(c => c.code).join(', '))}</p>` : ''}
      </div>`).join('') || '<p class="muted">No rounds returned.</p>';
  } catch (err) {
    status.textContent = err.message;
    summary.textContent = err.message;
  }
}

async function boot() {
  await loadHealth().catch(err => document.getElementById('runner-status').textContent = err.message);
  await loadExperiments().catch(err => document.getElementById('catalog').textContent = err.message);
  await loadClassifyExamples().catch(err => document.getElementById('qa-summary').textContent = err.message);
  await loadJobs().catch(err => document.getElementById('job-list').textContent = err.message);
}

boot();
setInterval(loadJobs, 5000);
</script>
</body>
</html>
"""


def _registered_route_methods() -> set[tuple[str, str]]:
    registered: set[tuple[str, str]] = set()
    for route in app.router.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or set():
                registered.add((method, route.path))
    return registered


_DEPLOYABLE_WORKBENCH_EXACT_PATHS = {
    "/api/config",
    "/api/sections",
    "/api/retrieval/experiments",
    "/api/retrieval/top-experiment",
    "/api/retrieval/try",
    "/api/hydration/candidates",
    "/eval/matrix",
    "/eval/matrix.csv",
    "/eval/e2e-matrix",
}
_DEPLOYABLE_WORKBENCH_PREFIXES = (
    "/api/prompts",
    "/api/search/",
    "/api/intercepts/",
    "/api/complexity/",
    "/api/atar/",
    "/api/benchmark/",
    "/api/kg/",
)


def _deployable_workbench_route(path: str) -> bool:
    return path in _DEPLOYABLE_WORKBENCH_EXACT_PATHS or any(
        path.startswith(prefix) for prefix in _DEPLOYABLE_WORKBENCH_PREFIXES
    )


def _install_full_workbench_routes() -> None:
    """Mount the product API surface into this deployable app.

    The app owns health, eval-job control, spend-aware retrieval trials, and
    matrix routes. Product routes are copied in one process, with non-deployed
    product surfaces filtered out at registration time.
    """
    installed = _registered_route_methods()
    route_sources = ("main",)
    for module_name in route_sources:
        module = importlib.import_module(module_name)
        source_app = getattr(module, "app")
        for route in source_app.router.routes:
            if not isinstance(route, APIRoute):
                continue
            if not _deployable_workbench_route(route.path):
                continue
            methods = route.methods or set()
            if any((method, route.path) in installed for method in methods):
                continue
            app.router.routes.append(route)
            for method in methods:
                installed.add((method, route.path))


_install_full_workbench_routes()

app.mount(
    "/assets",
    StaticFiles(directory=str(PRODUCT_FRONTEND_ASSETS), check_dir=False),
    name="workbench-assets",
)


@app.get("/", include_in_schema=False)
def deployable_app() -> FileResponse:
    return _react_index()


@app.get("/workbench", include_in_schema=False)
@app.get("/workbench/", include_in_schema=False)
def workbench_redirect() -> RedirectResponse:
    return RedirectResponse("/", status_code=308)


@app.get("/workbench/{path:path}", include_in_schema=False)
def workbench_spa_redirect(path: str) -> RedirectResponse:
    return workbench_redirect()
