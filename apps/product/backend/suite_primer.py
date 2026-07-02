from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path(__file__).parent.parent / "data" / "primer_runs"
DEFAULT_PREP_MODEL = "gpt-5-nano"
DEFAULT_CANDIDATE_MODEL = "gpt-5-mini"
DEFAULT_PROD_MODEL = os.environ.get("AI_SEARCH_PROD_MODEL_ID", "gpt-5.5")


MEASUREMENTS = [
    {
        "area": "corpus_coverage",
        "metrics": ["gold_code", "oracle_text", "seeded_fact_count"],
        "why": "Prefer grounded prompts so simulator and judge outputs are explainable.",
    },
    {
        "area": "retrieval_quality",
        "metrics": ["top1", "top3", "mrr", "heading_match", "top5_overlap", "hierarchical_score"],
        "why": "Measure whether the right goods code is close enough for the model loop.",
    },
    {
        "area": "question_behaviour",
        "metrics": ["rounds", "question_count", "question_efficiency", "fact_store_hit_rate"],
        "why": "Detect redundant questions and weak fact-slot handling.",
    },
    {
        "area": "judge_quality",
        "metrics": ["fact_consistency", "question_quality", "judge_error_rate"],
        "why": "Add semantic checks that deterministic code metrics cannot provide.",
    },
    {
        "area": "cost_latency",
        "metrics": ["total_cost", "cost_per_prompt", "latency_ms", "provider_errors"],
        "why": "Keep the run useful without quiet overspend.",
    },
]


def _auth_header() -> dict[str, str]:
    raw = os.environ.get("AI_SEARCH_BASIC_AUTH")
    if not raw:
        user = os.environ.get("BASIC_AUTH_USER")
        password = os.environ.get("BASIC_AUTH_PASSWORD")
        raw = f"{user}:{password}" if user and password else ""
    if not raw:
        return {}
    token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **_auth_header()}
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc

    def stream_benchmark(self, payload: dict[str, Any]):
        headers = {"Content-Type": "application/json", **_auth_header()}
        req = urllib.request.Request(
            self.base_url + "/api/benchmark/start",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3600) as res:
                event = None
                data_lines: list[str] = []
                for raw in res:
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].strip())
                    elif not line and event:
                        data_text = "\n".join(data_lines) or "{}"
                        yield event, json.loads(data_text)
                        event = None
                        data_lines = []
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"benchmark start failed: HTTP {exc.code}: {detail}") from exc


def _prompt_score(prompt: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        1 if prompt.get("has_oracle_text") else 0,
        int(prompt.get("gold_facts_count") or 0),
        1 if prompt.get("gold_code") else 0,
        int(prompt.get("result_count") or 0),
    )


def select_prompts(prompts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seeded = [
        p for p in prompts
        if p.get("gold_code") and (p.get("has_oracle_text") or int(p.get("gold_facts_count") or 0) > 0)
    ]
    ranked = sorted(seeded, key=_prompt_score, reverse=True)
    return ranked[:limit]


def model_ids(config: dict[str, Any]) -> set[str]:
    return {str(m.get("id")) for m in config.get("models", []) if m.get("id")}


def build_plan(args: argparse.Namespace, prompts: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    available_models = model_ids(config)
    prep_prompts = select_prompts(prompts, args.prep_prompts)
    prod_prompts = prep_prompts[: args.prod_prompts]
    stages = [
        {
            "name": "prep",
            "reference_model": args.prep_model,
            "candidate_models": [args.candidate_model],
            "prompt_indices": [p["index"] for p in prep_prompts],
            "purpose": "broad cheap screening",
        },
        {
            "name": "prod_check",
            "reference_model": args.prod_model,
            "candidate_models": [args.candidate_model],
            "prompt_indices": [p["index"] for p in prod_prompts],
            "purpose": "small production-reference confirmation",
        },
    ]
    for stage in stages:
        stage["missing_models"] = [
            m for m in [stage["reference_model"], *stage["candidate_models"]]
            if m not in available_models
        ]
        stage["prompt_count"] = len(stage["prompt_indices"])

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execute": bool(args.execute),
        "max_usd": float(args.max_usd or 0),
        "opensearch_limit": args.opensearch_limit,
        "measurements": MEASUREMENTS,
        "selection": {
            "available_prompts": len(prompts),
            "seeded_selected": len(prep_prompts),
            "prep_prompt_limit": args.prep_prompts,
            "prod_prompt_limit": args.prod_prompts,
        },
        "expectations": {
            "prep": "Find malformed examples and weak retrieval/prompt cases cheaply; do not use for production claims.",
            "prod_check": "Confirm a small sample against the production reference model; use this for behaviour claims.",
            "failure_list": "Any missing model, provider error, low judge coverage, excessive questions, or cap stop becomes follow-up work.",
        },
        "stages": stages,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def configure_stage(client: ApiClient, original: dict[str, Any], reference_model: str, sim_model: str) -> None:
    payload = {
        "reference_config": {
            **original.get("reference_config", {}),
            "mode": "single",
            "model_id": reference_model,
            "passes": 1,
        },
        "judge_config": {
            **original.get("judge_config", {}),
            "enabled": True,
            "model": sim_model,
            "reasoning_effort": "low",
            "temperature": 0.0,
        },
        "simulator_config": {
            **original.get("simulator_config", {}),
            "enabled": True,
            "model": sim_model,
            "reasoning_effort": "low",
            "temperature": 0.0,
        },
    }
    client.request_json("PUT", "/api/config", payload)


def restore_config(client: ApiClient, original: dict[str, Any]) -> None:
    client.request_json(
        "PUT",
        "/api/config",
        {
            "reference_config": original.get("reference_config", {}),
            "judge_config": original.get("judge_config", {}),
            "simulator_config": original.get("simulator_config", {}),
            "default_selected_model_ids": original.get("default_selected_model_ids", []),
        },
    )


def observed_cost(event: str, data: dict[str, Any]) -> float:
    if event in {"panel:complete", "model:complete"}:
        return float(data.get("total_cost") or 0)
    return 0.0


def reported_results_cost(results: Any) -> float:
    if not isinstance(results, dict):
        return 0.0
    summary_total = 0.0
    total = 0.0
    for summary in results.get("summaries", []) or []:
        if not isinstance(summary, dict):
            continue
        summary_total += float(summary.get("total_cost") or 0)
        summary_total += float(summary.get("total_judge_cost") or 0)
        summary_total += float(summary.get("total_simulator_cost") or 0)
    if summary_total:
        return summary_total
    for result in (results.get("baseline_results", []) or []) + (results.get("model_results", []) or []):
        if not isinstance(result, dict):
            continue
        total += float(result.get("total_cost") or 0)
        total += float(result.get("total_simulator_cost") or 0)
    for evaluation in results.get("evaluations", []) or []:
        if isinstance(evaluation, dict):
            total += float(evaluation.get("judge_cost") or 0)
    return total


def cancel_and_wait(client: ApiClient, seconds: int = 30) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    try:
        outcome["cancel"] = client.request_json("POST", "/api/benchmark/cancel", {})
    except Exception as exc:
        outcome["cancel_error"] = str(exc)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status = client.request_json("GET", "/api/benchmark/status")
            outcome["last_status"] = status
            if status.get("status") != "running":
                break
        except Exception as exc:
            outcome["status_error"] = str(exc)
            break
        time.sleep(1)
    return outcome


def fetch_stage_results(client: ApiClient, run_id: str | None) -> Any:
    paths = []
    if run_id:
        paths.append(f"/api/benchmark/runs/{run_id}")
    paths.append("/api/benchmark/results")
    last_exc: Exception | None = None
    for path in paths:
        try:
            return client.request_json("GET", path)
        except Exception as exc:
            last_exc = exc
    return {"error": str(last_exc) if last_exc else "No benchmark results available"}


def run_stage(
    client: ApiClient,
    run_dir: Path,
    stage: dict[str, Any],
    args: argparse.Namespace,
    original_config: dict[str, Any],
    stage_cap_usd: float,
) -> dict[str, Any]:
    configure_stage(client, original_config, stage["reference_model"], args.prep_model)
    payload = {
        "prompt_indices": stage["prompt_indices"],
        "model_ids": stage["candidate_models"],
        "opensearch_limit": args.opensearch_limit,
        "allow_spend": True,
    }
    events_path = run_dir / f"{stage['name']}.events.jsonl"
    observed = 0.0
    status = "running"
    run_id = None
    terminal_event = None
    error_message = None
    cancel_outcome = None
    try:
        with events_path.open("w") as f:
            for event, data in client.stream_benchmark(payload):
                record = {"event": event, "data": data, "at": datetime.now(timezone.utc).isoformat()}
                f.write(json.dumps(record, sort_keys=True) + "\n")
                f.flush()
                run_id = data.get("run_id") or run_id
                observed += observed_cost(event, data)
                if event == "benchmark:complete":
                    terminal_event = event
                    status = "complete"
                elif event == "benchmark:cancelled":
                    terminal_event = event
                    status = "cancelled"
                elif event == "error":
                    terminal_event = event
                    status = "error"
                    error_message = str(data.get("message") or data)
                    cancel_outcome = cancel_and_wait(client)
                    break
                if observed > stage_cap_usd:
                    terminal_event = "cap"
                    status = "cancelled_cap"
                    cancel_outcome = cancel_and_wait(client)
                    break
    except Exception as exc:
        status = "stream_error_cancelled"
        error_message = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            current = client.request_json("GET", "/api/benchmark/status")
            run_id = run_id or current.get("id")
        except Exception:
            pass
        cancel_outcome = cancel_and_wait(client)

    if status == "running":
        status = "stream_closed_without_terminal_event"
        cancel_outcome = cancel_and_wait(client)

    results = fetch_stage_results(client, run_id)
    if isinstance(results, dict):
        write_json(run_dir / f"{stage['name']}.results.json", results)
    reported_cost = reported_results_cost(results)
    if reported_cost > stage_cap_usd and status == "complete":
        status = "over_cap_after_stage"
    summary = {
        "name": stage["name"],
        "status": status,
        "run_id": run_id,
        "terminal_event": terminal_event,
        "observed_stream_cost_usd": round(observed, 6),
        "reported_results_cost_usd": round(reported_cost, 6),
        "events_file": str(events_path),
        "results_summary_count": len(results.get("summaries", [])) if isinstance(results, dict) else 0,
    }
    if error_message:
        summary["error"] = error_message
    if cancel_outcome:
        summary["cancel_outcome"] = cancel_outcome
    return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare or run a bounded suite primer benchmark.")
    parser.add_argument("--url", default=os.environ.get("AI_SEARCH_SUITE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--execute", action="store_true", help="Actually start provider-backed benchmark runs.")
    parser.add_argument("--dry-run", action="store_true", help="Alias for the default no-spend mode.")
    parser.add_argument("--max-usd", type=float, default=0.0, help="Required positive spend guard when --execute is set.")
    parser.add_argument("--prep-prompts", type=int, default=24)
    parser.add_argument("--prod-prompts", type=int, default=6)
    parser.add_argument("--opensearch-limit", type=int, default=80)
    parser.add_argument("--prep-model", default=DEFAULT_PREP_MODEL)
    parser.add_argument("--candidate-model", default=DEFAULT_CANDIDATE_MODEL)
    parser.add_argument("--prod-model", default=DEFAULT_PROD_MODEL)
    parser.add_argument("--output-dir", default=str(DATA_DIR))
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.execute and args.max_usd <= 0:
        print("--execute requires --max-usd > 0", file=sys.stderr)
        return 2

    client = ApiClient(args.url)
    config = client.request_json("GET", "/api/config")
    prompts = client.request_json("GET", "/api/prompts")
    plan = build_plan(args, prompts, config)

    run_dir = Path(args.output_dir) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    write_json(run_dir / "manifest.json", plan)

    if not args.execute:
        print(json.dumps({"mode": "dry_run", "manifest": str(run_dir / "manifest.json"), "plan": plan}, indent=2))
        return 0

    original_config = {
        "reference_config": config.get("reference_config", {}),
        "judge_config": config.get("judge_config", {}),
        "simulator_config": config.get("simulator_config", {}),
        "default_selected_model_ids": config.get("default_selected_model_ids", []),
    }
    stage_summaries: list[dict[str, Any]] = []
    cumulative_cost = 0.0
    restore_error = None
    try:
        for stage in plan["stages"]:
            if not stage["prompt_indices"]:
                stage_summaries.append({"name": stage["name"], "status": "skipped_no_prompts"})
                continue
            if stage.get("missing_models"):
                stage_summaries.append({
                    "name": stage["name"],
                    "status": "skipped_missing_models",
                    "missing_models": stage["missing_models"],
                })
                continue
            remaining = args.max_usd - cumulative_cost
            if remaining <= 0:
                stage_summaries.append({"name": stage["name"], "status": "skipped_cap_consumed"})
                continue
            summary = run_stage(client, run_dir, stage, args, original_config, remaining)
            cumulative_cost += max(
                float(summary.get("observed_stream_cost_usd") or 0),
                float(summary.get("reported_results_cost_usd") or 0),
            )
            stage_summaries.append(summary)
            if cumulative_cost >= args.max_usd or summary.get("status") != "complete":
                break
    finally:
        try:
            restore_config(client, original_config)
        except Exception as exc:
            restore_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    summary = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(run_dir / "manifest.json"),
        "cumulative_reported_or_observed_usd": round(cumulative_cost, 6),
        "stages": stage_summaries,
    }
    if restore_error:
        summary["restore_error"] = restore_error
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
