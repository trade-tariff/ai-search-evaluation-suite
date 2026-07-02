import type { AppConfig, BenchmarkResults, PromptInfo } from "./types";

const BASE = "/api";

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, init);
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body?.detail ?? body);
    } catch {
      // non-JSON error body
    }
    throw new Error(detail ? `${res.status} ${res.statusText}: ${detail}` : `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export async function getConfig(): Promise<AppConfig> {
  return json("/config");
}

export async function updateConfig(payload: Record<string, unknown>): Promise<void> {
  await json("/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function getPrompts(): Promise<PromptInfo[]> {
  return json("/prompts");
}

export async function getPromptDetail(index: number): Promise<Record<string, unknown>> {
  return json(`/prompts/${index}`);
}

export async function getBenchmarkStatus(): Promise<Record<string, unknown>> {
  return json("/benchmark/status");
}

export async function getBenchmarkResults(): Promise<BenchmarkResults> {
  return json("/benchmark/results");
}

export function startBenchmarkSSE(
  promptIndices: number[],
  modelIds: string[],
  onEvent: (event: string, data: Record<string, unknown>) => void,
  onDone: () => void,
  onError: (err: string) => void,
  opensearchLimit: number = 80,
): AbortController {
  const ctrl = new AbortController();

  fetch(`${BASE}/benchmark/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt_indices: promptIndices,
      model_ids: modelIds,
      opensearch_limit: opensearchLimit,
      allow_spend: true,
    }),
    signal: ctrl.signal,
  })
    .then(async (res) => {
      if (!res.ok || !res.body) {
        onError(`HTTP ${res.status}`);
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        let currentEvent = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            try {
              const data = JSON.parse(line.slice(6));
              onEvent(currentEvent, data);
            } catch {
              // skip malformed
            }
          }
        }
      }
      onDone();
    })
    .catch((err) => {
      if (err.name !== "AbortError") onError(String(err));
    });

  return ctrl;
}

export interface RunListItem {
  id: string;
  timestamp: string;
  status: string;
  opensearch_limit: number;
  baseline_model_id: string | null;
  panel_model_ids: string[];
  prompt_count: number;
  model_count: number;
  summary_count: number;
  filename: string;
}

export async function listRuns(): Promise<RunListItem[]> {
  return json("/benchmark/runs");
}

export async function cancelBenchmark(): Promise<{ cancelled: boolean }> {
  return json("/benchmark/cancel", { method: "POST" });
}

export async function getRunResults(runId: string): Promise<BenchmarkResults> {
  return json(`/benchmark/runs/${runId}`);
}

export interface EvalCostSummary {
  totals: {
    calls: number;
    ok: number;
    failed: number;
    runs: number;
    models: number;
    prompt_versions: number;
    cost_usd: number;
    prompt_tokens: number;
    completion_tokens: number;
    first_write?: string | null;
    last_write?: string | null;
  };
  runs: Array<{
    run_id: string;
    calls: number;
    ok: number;
    failed: number;
    models: number;
    prompt_versions: number;
    commodity_codes: number;
    cost_usd: number;
    prompt_tokens: number;
    completion_tokens: number;
    avg_score?: number | null;
    first_write?: string | null;
    last_write?: string | null;
    duration_seconds?: number | null;
  }>;
  model_totals: Array<{
    model: string;
    calls: number;
    ok: number;
    cost_usd: number;
    prompt_tokens: number;
    completion_tokens: number;
    avg_score?: number | null;
  }>;
  prompt_totals: Array<{
    prompt_version: string;
    calls: number;
    ok: number;
    cost_usd: number;
    avg_score?: number | null;
  }>;
  spend_totals?: {
    fact_eval_cost_usd: number;
    retrieval_embedding_est_cost_usd: number;
    e2e_est_cost_usd: number;
    classification_est_cost_usd: number;
    estimated_total_usd: number;
    embedding_cost_per_million_tokens: number;
    e2e_provider_call_est_usd: number;
  };
  retrieval?: {
    totals: {
      runs: number;
      calls: number;
      estimated_embedding_tokens: number;
      estimated_cost_usd: number;
      last_write?: string | null;
    };
    runs: Array<{
      id: number;
      run_label: string;
      n_queries: number;
      retrieval_limit: number;
      calls: number;
      use_vector: boolean;
      use_facts_vec: boolean;
      use_kg_vec: boolean;
      estimated_embedding_tokens: number;
      estimated_cost_usd: number;
      first_write?: string | null;
      last_write?: string | null;
      duration_seconds?: number | null;
    }>;
  };
  e2e?: {
    totals: {
      runs: number;
      provider_calls: number;
      estimated_embedding_tokens: number;
      estimated_cost_usd: number;
      last_write?: string | null;
    };
    runs: Array<{
      id: number;
      run_label: string;
      retrieval_run_label: string;
      question_mode: string;
      answerer: string;
      input_count: number;
      provider_calls_used: number;
      estimated_embedding_tokens: number;
      estimated_cost_usd: number;
      first_write?: string | null;
      last_write?: string | null;
      duration_seconds?: number | null;
    }>;
  };
  classification?: {
    totals: {
      runs: number;
      sessions: number;
      estimated_cost_usd: number;
      last_write?: string | null;
    };
    runs: Array<{
      run_label: string;
      model: string;
      strategy: string;
      prompt_mode: string;
      augmentation: string;
      sessions: number;
      estimated_cost_usd: number;
      first_write?: string | null;
      last_write?: string | null;
    }>;
  };
}

export async function getEvalCostSummary(limit = 20): Promise<EvalCostSummary> {
  return json(`/eval-cost/summary?limit=${limit}`);
}

export function exportJsonUrl(): string {
  return `${BASE}/benchmark/export/json`;
}

export function exportCsvUrl(): string {
  return `${BASE}/benchmark/export/csv`;
}

// --- Prompt authoring ---

export interface SearchProbe {
  ok: boolean;
  total?: number;
  embedded?: number;
  opensearch?: {
    configured: boolean;
    ok: boolean;
    status?: string | null;
    index?: string;
    count?: number;
    error?: string;
  };
  error?: string;
}

export interface PreviewCandidate {
  commodity_code: string;
  description: string;
  score: number;
}

export interface PreviewResponse {
  raw_query: string;
  processed_query: string;
  formatted_results: PreviewCandidate[];
  result_count: number;
}

export async function searchProbe(): Promise<SearchProbe> {
  return json("/search/probe");
}

export async function searchPreview(
  raw_query: string,
  limit: number = 80,
): Promise<PreviewResponse> {
  return json("/search/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ raw_query, limit, allow_spend: true }),
  });
}

export async function savePrompt(
  payload: PreviewResponse,
): Promise<{ index: number; total: number }> {
  return json("/prompts/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// --- ATaR ingestion + approval ---

export interface AtarRulingMeta {
  ref: string;
  commodity_code: string;
  description: string;
  justification: string;
  keywords: string[];
  start_date: string;
  expiry_date: string;
}

export interface AtarDraft {
  ref: string;
  ruling: AtarRulingMeta;
  raw_query: string;
  gold_code: string;
  oracle_text: string;
  gold_facts: Array<{ slot: string; answer: string; source_question?: string }>;
  formatted_results: PreviewCandidate[];
  status: "pending" | "approved" | "discarded";
  approved_prompt_index?: number | null;
}

export async function listAtarDrafts(): Promise<{ drafts: AtarDraft[] }> {
  return json("/atar/drafts");
}

export async function getAtarDraft(ref: string): Promise<AtarDraft> {
  return json(`/atar/drafts/${ref}`);
}

export async function ingestAtarBatch(
  count: number,
  refs?: string[],
  opensearch_limit: number = 80,
): Promise<{ ingested: AtarDraft[]; ingested_count: number; skipped_existing: string[] }> {
  return json("/atar/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count, refs, opensearch_limit, allow_spend: true }),
  });
}

export async function regenerateAtarFacts(
  ref: string,
): Promise<{ ref: string; gold_facts: AtarDraft["gold_facts"] }> {
  return json(`/atar/drafts/${ref}/regenerate-facts`, { method: "POST" });
}

export async function patchAtarDraft(
  ref: string,
  patch: Partial<Pick<AtarDraft, "gold_facts" | "raw_query" | "oracle_text" | "gold_code">>,
): Promise<AtarDraft> {
  return json(`/atar/drafts/${ref}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export async function approveAtarDraft(
  ref: string,
  override_facts?: AtarDraft["gold_facts"],
): Promise<{ prompt_index: number; total: number; ref: string }> {
  return json(`/atar/drafts/${ref}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(override_facts ? { gold_facts: override_facts } : {}),
  });
}

export async function discardAtarDraft(ref: string): Promise<{ ref: string; status: string }> {
  return json(`/atar/drafts/${ref}`, { method: "DELETE" });
}
