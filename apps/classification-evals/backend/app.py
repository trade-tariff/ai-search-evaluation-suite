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
    modes = 1
    if getattr(req, "harness", "classify") == "e2e" and req.question_modes:
        modes = max(1, len([m for m in req.question_modes.split(",") if m.strip()]))
    return len(req.personas) * per_persona * modes


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
    # harness="e2e" runs the fixed-retrieval end-to-end matrix instead of the
    # classify matrix, so any experiment-catalogue retrieval base (including
    # the top KG config) can feed the Q&A stage. Results land in
    # kg.e2e_eval_runs and surface in the E2E/Q&A matrix tabs.
    harness: Literal["classify", "e2e"] = "classify"
    retrieval_run_label: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    question_modes: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_,-]{1,200}$")


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


def _openai_api_key() -> str | None:
    """Env var first; fall back to the key saved via the Configuration tab
    (product config.json) - previously invisible to eval-native jobs."""
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    try:
        cfg_path = PRODUCT_BACKEND.parent / "data" / "config.json"
        data = json.loads(cfg_path.read_text())
        key = str((data.get("api_keys") or {}).get("openai") or "").strip()
        return key or None
    except Exception:
        return None


def _validate_request(req: JobCreate) -> None:
    unknown_personas = sorted(set(req.personas) - set(PERSONA_CHOICES))
    if unknown_personas:
        raise HTTPException(422, f"Unknown personas: {', '.join(unknown_personas)}")
    if req.harness == "e2e":
        if req.sweep:
            raise HTTPException(422, "Sweep is not supported by the e2e harness.")
        if req.retrieval_run_label:
            try:
                from experiment_retrieval import experiment_catalog

                known = {row.get("run_label") for row in experiment_catalog()}
            except Exception as exc:
                raise HTTPException(500, f"Could not load the retrieval experiment catalogue: {exc}")
            if req.retrieval_run_label not in known:
                raise HTTPException(422, f"Unknown retrieval experiment: {req.retrieval_run_label}")
    elif req.retrieval_run_label:
        raise HTTPException(
            422,
            "retrieval_run_label requires harness='e2e' (the classify harness has a fixed retrieval config).",
        )
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
    if not _openai_api_key():
        raise HTTPException(
            400,
            "No OpenAI key available: set OPENAI_API_KEY or save one in the Configuration tab.",
        )
    if not PRODUCT_BACKEND.exists():
        raise HTTPException(500, "Product backend is not available.")


def _build_command(req: JobCreate) -> list[str]:
    python = os.environ.get("CLASSIFY_EVAL_PYTHON") or sys.executable
    if req.harness == "e2e":
        cmd = [
            python,
            "-m",
            "classification_core.run_hydrated_e2e_matrix",
            "--run-label", req.run_label,
            "--retrieval-run-label", req.retrieval_run_label or "baseline_fts_only",
            "--pair-limit", str(req.limit if req.limit is not None else 5),
            "--personas", ",".join(req.personas),
            "--retrieval-limit", str(req.candidate_limit),
            "--max-rounds", str(req.max_rounds),
            "--question-model", req.model,
            "--concurrency", str(req.concurrency),
            "--allow-spend",
        ]
        # The harness's own default runs ALL question modes, multiplying
        # sessions (and spend) by three - default to one mode instead.
        cmd.extend(["--question-modes", req.question_modes or "facet_rules"])
        return cmd
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
        "openai_key_present": bool(_openai_api_key()),
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
        "harnesses": ["classify", "e2e"],
        "e2e_question_modes": ["facet_rules", "facet_rules_llm_wording", "llm_generated"],
        "e2e_retrieval_bases": "any run_label from GET /api/retrieval/experiments",
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
    api_key = _openai_api_key()
    if needs_provider and not api_key:
        raise HTTPException(
            400,
            "No OpenAI key available for semantic retrieval trials: set OPENAI_API_KEY "
            "or save one in the Configuration tab.",
        )

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
                  AND active
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
              AND active
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
    if not _openai_api_key():
        raise HTTPException(
            400,
            "No OpenAI key available for Q&A emulator trials: set OPENAI_API_KEY "
            "or save one in the Configuration tab.",
        )
    if req.model not in _allowed_models():
        raise HTTPException(422, "Classifier model is not enabled for classification trials.")
    if req.simulator_model not in _allowed_models():
        raise HTTPException(422, "Trader emulator model is not enabled for classification trials.")
    per_session = _env_float("CLASSIFY_EVAL_EST_USD_PER_SESSION", 0.05)
    max_cost = _env_float("CLASSIFY_EVAL_MAX_EST_USD", 10.0)
    if per_session > max_cost:
        raise HTTPException(
            422,
            f"Estimated trial cost ${per_session:.2f} exceeds server cap CLASSIFY_EVAL_MAX_EST_USD=${max_cost:.2f}.",
        )

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
    key = _openai_api_key()
    if key and not env.get("OPENAI_API_KEY"):
        # Not setdefault: the container env carries OPENAI_API_KEY as an
        # EMPTY string, which setdefault would preserve.
        env["OPENAI_API_KEY"] = key
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

    def _wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
        if n <= 0:
            return (0.0, 0.0)
        p = successes / n
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return (max(0.0, centre - margin), min(1.0, centre + margin))

    def pct(num, den) -> str:
        try:
            den = int(den or 0)
            if den <= 0:
                return "-"
            k = int(num or 0)
            lo, hi = _wilson(k, den)
            return (
                f"<span title='95% CI {100 * lo:.0f}-{100 * hi:.0f}% (n={den})'>"
                f"{(100 * k / den):.1f}%</span>"
            )
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
        def _top1_rate(row) -> float | None:
            nn = row.get("n_inputs") or row.get("input_count") or row.get("result_rows") or 0
            el = row.get("initial_gold_in_retrieval") or 0
            dd = el if qa_only else nn
            return (int(row.get("gold_top1_after_qa") or 0) / dd) if dd else None

        baseline_label = (os.environ.get("E2E_BASELINE_RUN_LABEL") or "").strip()
        baseline_row = next((row for row in rows if row.get("run_label") == baseline_label), None)
        if baseline_row is not None:
            rows = [baseline_row] + [row for row in rows if row is not baseline_row]
        baseline_rate = _top1_rate(baseline_row) if baseline_row is not None else None

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
            is_base = baseline_row is not None and r is baseline_row
            base_badge = (
                "<span style='background:#1d4ed8;color:#dbeafe;font-size:9px;"
                "padding:1px 6px;border-radius:8px'>LIVE BASELINE</span><br>"
                if is_base else ""
            )
            top1_cell = pct(r.get('gold_top1_after_qa'), denom)
            rate = _top1_rate(r)
            if baseline_rate is not None and not is_base and rate is not None:
                delta = (rate - baseline_rate) * 100
                colour = "#bbf7d0" if delta >= 0 else "#fca5a5"
                top1_cell += (
                    f"<br><span style='color:{colour};font-size:11px'>"
                    f"{delta:+.1f}pp vs live</span>"
                )
            row_html.append(
                f"""
                <tr>
                  <td class='id'>#{esc(r.get('id'))}</td>
                  <td>
                    {base_badge}<b>{esc(r.get('run_label'))}</b>
                    <br><span>{esc(r.get('retrieval_run_label'))}</span>
                    <br><code>{esc(prompt_label(r))}</code>
                  </td>
                  <td>{esc(r.get('question_mode'))}<br><span>{esc(r.get('answerer'))}</span></td>
                  <td>{esc(r.get('question_model') or '-')}<br><span>sim: {esc(r.get('simulator_model') or '-')}</span></td>
                  <td>{esc(n)}<br><span>{esc(eligible)} eligible</span></td>
                  <td>{pct(r.get('initial_gold_in_retrieval'), n)}</td>
                  <td>{pct(r.get('gold_kept'), denom)}</td>
                  <td>{top1_cell}</td>
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
