import type {
  CandidateCC,
  CandidateHydrationRun,
  ClassifyTurn,
  CommodityRequirements,
  CommodityHydration,
  Country,
  DeclarationResult,
  DutyInputInferenceResult,
  DutyResult,
  ExciseInputs,
  FilingIntentResult,
  HydrationSources,
  JourneyExample,
  JourneyPersona,
  LandedResult,
  MeursingInputs,
  ValuationGuideResult,
  ValuationResult,
} from "./types";

async function post<T>(path: string, body: unknown): Promise<T> {
  const init = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetchWithLocalBackendFallback(path, init);
  if (!res.ok) throw new Error(`${path} -> ${res.status}: ${await res.text()}`);
  return res.json();
}

async function postBlob(path: string, body: unknown): Promise<Blob> {
  const init = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetchWithLocalBackendFallback(path, init);
  if (!res.ok) throw new Error(`${path} -> ${res.status}: ${await res.text()}`);
  return res.blob();
}

async function get<T>(path: string): Promise<T> {
  const res = await fetchWithLocalBackendFallback(path);
  if (!res.ok) throw new Error(`${path} -> ${res.status}: ${await res.text()}`);
  return res.json();
}

// --- SSE streaming (backlog item 17) --------------------------------------
// The /stream classify endpoints emit milestone events (expansion_done,
// retrieval_done, candidates_ready, llm_started, turn_complete) followed by
// 'turn' (the full ClassifyTurn), then 'done'. EventSource cannot POST, so we
// parse the text/event-stream body from fetch by hand.

export type ClassifyStreamHandlers = {
  onEvent: (name: string, payload: unknown) => void;
  signal?: AbortSignal;
};

// Single error type for ANY streaming failure (non-200, network, parse,
// stream ended without a turn) so callers can catch it and fall back to the
// non-streaming endpoints.
export class ClassifyStreamError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ClassifyStreamError";
  }
}

// Parse one SSE block (lines between blank-line separators): "event:" names
// the event, "data:" lines carry JSON (joined per the SSE spec), and ":"
// comment lines (server keep-alives) are ignored.
function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).replace(/^ /, ""));
    }
  }
  if (dataLines.length === 0) return null;
  return { event, data: JSON.parse(dataLines.join("\n")) };
}

async function postSseStream(
  path: string,
  body: unknown,
  handlers: ClassifyStreamHandlers
): Promise<ClassifyTurn> {
  let res: Response;
  try {
    res = await fetchWithLocalBackendFallback(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal: handlers.signal,
    });
  } catch (err) {
    throw new ClassifyStreamError(`${path} -> ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!res.ok || !res.body) {
    throw new ClassifyStreamError(`${path} -> ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let turn: ClassifyTurn | null = null;
  try {
    let finished = false;
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
      // SSE messages are separated by a blank line.
      while (!finished) {
        const sep = buffer.indexOf("\n\n");
        if (sep === -1) break;
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const message = parseSseBlock(block);
        if (!message) continue;
        if (message.event === "turn") {
          turn = message.data as ClassifyTurn;
        } else if (message.event === "error") {
          const detail = (message.data as { detail?: string } | null)?.detail;
          throw new ClassifyStreamError(`${path} -> ${detail || "stream error event"}`);
        }
        handlers.onEvent(message.event, message.data);
        if (message.event === "done") finished = true;
      }
    }
  } catch (err) {
    throw err instanceof ClassifyStreamError
      ? err
      : new ClassifyStreamError(`${path} -> ${err instanceof Error ? err.message : String(err)}`);
  } finally {
    reader.cancel().catch(() => {});
  }
  if (!turn) throw new ClassifyStreamError(`${path} -> stream ended without a turn event`);
  return turn;
}

async function fetchWithLocalBackendFallback(path: string, init?: RequestInit): Promise<Response> {
  try {
    const res = await fetch(path, init);
    if (res.status === 404 && shouldRetryDirectBackend(path) && window.location.port !== "8000") {
      return fetch(`http://127.0.0.1:8000${path}`, init);
    }
    return res;
  } catch (err) {
    if (!shouldRetryDirectBackend(path)) throw err;
    return fetch(`http://127.0.0.1:8000${path}`, init);
  }
}

function shouldRetryDirectBackend(path: string): boolean {
  if (typeof window === "undefined") return false;
  if (!path.startsWith("/api/")) return false;
  return window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost";
}

export const api = {
  journeyExamples: (persona?: string) =>
    get<{
      config: Record<string, unknown>;
      examples: JourneyExample[];
      source?: string;
      note?: string;
      persona?: string | null;
      personas?: JourneyPersona[];
    }>(`/api/journey/examples${persona ? `?persona=${encodeURIComponent(persona)}` : ""}`),
  classifyStart: (query: string, config?: any) =>
    post<ClassifyTurn>("/api/classify/start", { query, config }),
  classifyAnswer: (
    query: string,
    qa_history: { question: string; answer: string }[],
    config?: any,
    fixed_candidates?: Record<string, unknown>[]
  ) =>
    post<ClassifyTurn>("/api/classify/answer", { query, qa_history, config, fixed_candidates: fixed_candidates || [] }),
  // Streaming variants of classifyStart/classifyAnswer. Same request and turn
  // semantics; milestone events are surfaced via handlers.onEvent as they
  // happen. Throws ClassifyStreamError on any failure - callers fall back to
  // the non-streaming calls above.
  classifyStartStream: (query: string, config: any, handlers: ClassifyStreamHandlers) =>
    postSseStream("/api/classify/start/stream", { query, config }, handlers),
  classifyAnswerStream: (
    query: string,
    qa_history: { question: string; answer: string }[],
    config: any,
    fixed_candidates: Record<string, unknown>[] | undefined,
    handlers: ClassifyStreamHandlers
  ) =>
    postSseStream(
      "/api/classify/answer/stream",
      { query, qa_history, config, fixed_candidates: fixed_candidates || [] },
      handlers
    ),
  classifyCompare: (
    query: string,
    panels: { label: string; config: any }[],
    qa_history?: { question: string; answer: string }[]
  ) =>
    post<{ query: string; panels: { label: string; config: any; turn: ClassifyTurn }[] }>(
      "/api/classify/compare",
      { query, panels, qa_history: qa_history || [] }
    ),
  hydrateCommodity: (
    code: string,
    req: { summarize?: boolean; allow_spend?: boolean; model?: string | null; sources?: Partial<HydrationSources> }
  ) => post<CommodityHydration>(`/api/commodity/${encodeURIComponent(code)}/hydrate`, req),
  hydrateCandidates: (req: {
    query: string;
    config?: any;
    candidates?: CandidateCC[];
    candidate_limit?: number;
    hydrate_limit?: number;
    summarize?: boolean;
    allow_spend?: boolean;
    model?: string | null;
    sources?: Partial<HydrationSources>;
  }) => post<CandidateHydrationRun>("/api/hydration/candidates", req),
  valuation: (req: {
    invoice_value?: number;
    invoice_currency?: string;
    fx_rate_to_gbp?: number;
    freight_gbp?: number;
    insurance_gbp?: number;
    other_costs_gbp?: number;
    method?: string;
    known_customs_value_gbp?: number | null;
    method_inputs?: Record<string, unknown>;
  }) => post<ValuationResult>("/api/valuation", req),
  valuationGuide: (req: {
    has_sale_for_export: boolean;
    has_usable_transaction_value: boolean;
    has_identical_goods_value: boolean;
    has_similar_goods_value: boolean;
    has_uk_resale_price: boolean;
    has_production_costs: boolean;
    try_computed_before_deductive: boolean;
    inputs: Record<string, unknown>;
  }) => post<ValuationGuideResult>("/api/valuation/guide", req),
  dutyInfer: (req: {
    commodity_code: string;
    query: string;
    qa_history: { question: string; answer: string }[];
    customs_value_gbp?: number | null;
    known_inputs?: Record<string, unknown>;
  }) => post<DutyInputInferenceResult>("/api/duty/infer", req),
  duty: (req: {
    commodity_code: string;
    country_of_origin: string;
    customs_value_gbp: number;
    import_destination: "GB" | "XI";
    import_date?: string | null;
    quantity_units?: number | null;
    quantity_unit_type?: string | null;
    has_proof_of_origin: boolean;
    excise_inputs?: ExciseInputs | null;
    meursing_inputs?: MeursingInputs | null;
    // null or omitted = backend seeds the commodity's real VAT rate (type-305
    // measure); 0 = genuine zero rate. JSON.stringify preserves both 0 and
    // null as-is, so neither is coerced or dropped.
    vat_rate?: number | null;
  }) => post<DutyResult>("/api/duty", req),
  dutyRequirements: (code: string) =>
    get<CommodityRequirements>(
      `/api/duty/requirements/${encodeURIComponent(code)}`
    ),
  dutyExplain: (duty_result: DutyResult) =>
    post<{ text: string }>("/api/duty/explain", { duty_result }),
  landed: (req: {
    customs_value_gbp: number;
    customs_duty_gbp: number;
    excise_duty_gbp: number;
    vat_rate: number;
    // Post-arrival charges (broker, onward UK haulage) - outside the VAT base.
    additional_charges_gbp: number;
    // Incidental costs to the first UK destination - inside the VAT base.
    incidental_costs_to_uk_gbp?: number;
    use_postponed_vat?: boolean;
    // GBP 135 regime override; omit to let the backend infer it.
    low_value_import?: boolean | null;
  }) => post<LandedResult>("/api/landed", req),
  declaration: (req: {
    commodity_code: string;
    description_of_goods: string;
    country_of_origin: string;
    import_date?: string | null;
    customs_value_gbp: number;
    quantity_units?: number | null;
    quantity_unit_type?: string | null;
    net_mass_kg?: number | null;
    duty_gbp: number;
    excise_gbp: number;
    vat_gbp: number;
    has_proof_of_origin: boolean;
    preference_claimed?: string | null;
    valuation_method?: string | null;
    additional_codes?: Record<string, unknown>[];
    original_query?: string | null;
    qa_history?: { question: string; answer: string }[];
    rejected_candidates?: Record<string, unknown>[];
  }) => post<DeclarationResult>("/api/declaration", req),
  declarationFileIntent: (declaration: DeclarationResult) =>
    post<FilingIntentResult>("/api/declaration/file-intent", { declaration }),
  declarationDownload: (declaration: DeclarationResult, journey_state: Record<string, unknown>) =>
    postBlob("/api/declaration/download", { declaration, journey_state }),
  countries: () => get<Country[]>("/api/countries"),
};
