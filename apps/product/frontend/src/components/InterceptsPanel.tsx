/**
 * Intercepts panel — visualise per-term complexity for the HMRC intercept
 * list. Live retrieval + KPI computation via /api/intercepts/*.
 *
 * Layout:
 *   - Top:    Run controls (K, over-fetch, weights, "Analyze selected" /
 *             "Analyze all 728" buttons) + saved runs dropdown
 *   - Left:   Searchable, sortable, filterable table of analysed terms
 *             with composite + top-line KPIs.
 *   - Right:  Per-term detail panel (top candidates, per-level entropy
 *             bar chart, section→chapter→heading treemap).
 *   - Bottom: Scatter plots for term and commodity complexity.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ScatterChart,
  Scatter,
  ZAxis,
  Legend,
  ReferenceLine,
  ReferenceArea,
} from "recharts";

export const TEMPLATE_COLORS: Record<string, string> = {
  Generic: "#3b82f6",
  "Hard-to-classify": "#f59e0b",
  Escalate: "#ef4444",
};

// Action-bucket overlay — only `description.guidance` carries real meaning:
// the walked path has legal/lab/expert predicates, so the trader genuinely
// can't answer without external context (notes / regulation / lab analysis).
//
// Removed: `annotate_ai166_fix` — it measured `other_leaf_share` (% of top-K
// with generation_type='ai'). AI-166 already gave those leaves real
// contextualised descriptions, so landing on them is not a quality signal,
// just a content-source signal.
// Removed: `description.exclude` / `description.filter` — heuristic
// vagueness-spread names with no real remediation playbook.
const ACTION_COLORS: Record<string, string> = {
  "description.guidance": "#a855f7",   // purple
};
const ACTION_LABELS: Record<string, string> = {
  "description.guidance": "Context dependant",
};
const ACTION_KEYS = [
  "description.guidance",
] as const;

const LEVELS = ["section", "chapter", "heading", "subheading", "eight_digit", "declarable"] as const;
type Level = (typeof LEVELS)[number];

type Term = {
  index: number;
  source: string;
  count: number;
  term: string;
  related_words: string | null;
  template: string;
  guidance_page: string | null;
  chapter: string | null;
  decision: string;
  notes: string | null;
};

export type KPIRow = {
  index?: number;
  term: string;
  k: number;
  n_results: number;
  composite: number;
  count?: number;
  template?: string;
  source?: string;
  questions_max: number;
  questions_expected: number;
  inflexion_levels: number;
  decision_points: number;
  widest_branch: number;
  worst_case_questions: number;
  worst_case_bits?: number;
  lca_digits: number;
  unresolved_digits: number;
  mean_indent_depth: number;
  score_flatness: number;
  other_leaf_share: number;
  top_cosine?: number;
  vagueness?: number;
  // Per-row classifier output (Day 0.5 action-bucket proxy):
  bucket?: "A" | "B";
  lane?: "intercept" | "annotate_ai166_fix" | null;
  intercept_type?: string | null;  // e.g. "description.guidance", "description.exclude"
  bucket_reason?: string;
  query?: string;                  // the actual retrieval query used
  query_strategy?: "self_text" | "paraphrase";
  source_self_text?: string;
  top_section: string;
  top_chapter: string;
  top_chapter_share: number;
  section_chain: string;
  top_score: number;
  bottom_score: number;
  n_section: number;
  n_chapter: number;
  n_heading: number;
  n_subheading: number;
  n_eight_digit: number;
  n_declarable: number;
  entropy_section: number;
  entropy_chapter: number;
  entropy_heading: number;
  entropy_subheading: number;
  entropy_eight_digit: number;
  entropy_declarable: number;
};

type Candidate = {
  goods_nomenclature_sid: number;
  goods_nomenclature_item_id: string;
  self_text: string | null;
  search_text: string | null;
  score: number;
  cosine_score: number | null;
  bm25_rank: number | null;
  vector_rank: number | null;
  declarable: boolean | null;
  generation_type: string | null;
  section: string | null;
  section_title: string | null;
  chapter_title: string | null;
  heading_title: string | null;
  subheading_title: string | null;
  eight_digit_title: string | null;
  declarable_title: string | null;
  contextualised_other?: boolean | null;
  // AI-166: per-level flag set when the title above came from a contextualised
  // self_text rather than the raw "Other" description. Lets the tree badge
  // intermediate boxes (heading/subheading) whose label is contextualised.
  chapter_contextualised?: boolean;
  heading_contextualised?: boolean;
  subheading_contextualised?: boolean;
  eight_digit_contextualised?: boolean;
};

type BelowThresholdCandidate = {
  goods_nomenclature_item_id: string;
  search_text: string | null;
  self_text: string | null;
  cosine_score: number;
  vector_rank: number;
};

type Detail = {
  row?: KPIRow;
  retrieval_meta?: {
    vector_count: number;
    keyword_count: number;
    fused_count: number;
    declarable_count: number;
    vector_threshold_used?: number;
  };
  top_candidates?: Candidate[];
  below_threshold_candidates?: BelowThresholdCandidate[];
  error?: string;
};

type AnalyzeResponse = {
  k: number;
  over_fetch: number;
  weights: Record<string, number>;
  n_terms: number;
  elapsed_seconds: number;
  rows: KPIRow[];
  details: Record<string, Detail>;
  completed_at: string;
};

type SavedRun = { id: string; name: string; saved_at: string; n_terms: number; k: number };

// Composite weights — biased toward the "trader-facing difficulty" signals:
// cross-section spread, cross-chapter spread, expected Q&A rounds, and
// query-side vagueness. The "shape" signals (unresolved digits, score
// flatness, n.e.s. destinations) are light tiebreakers. Sliders auto-
// normalise to sum=1.0.
//
// retrieval_failure is NOT part of this weighted sum — it's a blend
// override that pulls composite toward 1.0 when retrieval gave up (≤1
// candidate). See recomputeCompositeRows for the formula.
//
// MUST stay equal to the canonical backend set
// (apps/product/backend/intercept_kpis.py DEFAULT_WEIGHTS) so the same term
// shows the same composite in this table, the Complexity charts and every
// API response.
export const DEFAULT_WEIGHTS: Record<string, number> = {
  section_spread: 0.25,
  chapter_spread: 0.25,
  questions_expected: 0.25,
  vagueness: 0.20,
  score_flatness: 0.02,
  unresolved_digits: 0.02,
  other_leaf_share: 0.01,
};

export function recomputeCompositeRows(rows: KPIRow[], weights: Record<string, number>): KPIRow[] {
  return rows.map((row) => {
    // Scatter-companion rows ship only the saved composite (raw KPIs were
    // dropped to keep the file ~3MB instead of ~800MB). Live-reweighting
    // would zero them out — keep the saved score.
    if ((row as any)._scatter_companion) return row;
    const n = row.n_results;
    const sectionSpreadNorm = row.n_section > 0 ? (row.n_section - 1) / Math.max(Math.min(n, 21) - 1, 1) : 0;
    const chapterSpreadNorm = row.n_chapter > 0 ? (row.n_chapter - 1) / Math.max(Math.min(n, 99) - 1, 1) : 0;
    const questionsNorm = n > 1 ? row.questions_expected / Math.log2(n) : 0;
    const unresolvedNorm = row.unresolved_digits / 10;
    // Vagueness: live-recomputed from row.vagueness if backend provided it
    // (old saved runs may lack the field — treat as 0 so loading a v1 run
    // doesn't shift scores unexpectedly).
    const vagueness = row.vagueness ?? 0;
    // Retrieval-failure blend: n=0 → 1.0, n=1 → 0.8, n≥5 → 0. When retrieval
    // gives up, the spread/entropy signals can't fire — so we pull composite
    // toward 1.0 by the failure fraction.
    //
    // ONLY applied for trader-query strategies (paraphrase or undefined).
    // For "self_text" runs (a code's own self_text retrieving itself), a low
    // n is legitimate — niche commodities are genuinely unique and shouldn't
    // be flagged as broken retrieval.
    const isTraderQuery = row.query_strategy !== "self_text";
    const retrievalFailure = isTraderQuery ? Math.max(0, (5 - Math.min(n, 5)) / 5) : 0;
    const weightedSum =
      weights.section_spread * sectionSpreadNorm +
      weights.chapter_spread * chapterSpreadNorm +
      weights.questions_expected * questionsNorm +
      weights.unresolved_digits * unresolvedNorm +
      weights.score_flatness * row.score_flatness +
      weights.other_leaf_share * row.other_leaf_share +
      (weights.vagueness ?? 0) * vagueness;
    const composite = (1 - retrievalFailure) * weightedSum + retrievalFailure * 1.0;
    return { ...row, composite };
  });
}

export function weightsFromRun(saved?: Record<string, number>): Record<string, number> {
  const next = { ...DEFAULT_WEIGHTS };
  if (!saved) return next;
  for (const key of Object.keys(DEFAULT_WEIGHTS)) {
    const value = saved[key];
    if (Number.isFinite(value) && value >= 0) next[key] = value;
  }
  return next;
}

export function stableJitter(key: string, width = 0.6) {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) & 0xffff;
  return ((h / 0xffff) - 0.5) * width;
}

function snapshotWithRows(
  analysis: AnalyzeResponse,
  rows: KPIRow[],
  weights: Record<string, number>,
): AnalyzeResponse {
  const rowByTerm = new Map(rows.map((row) => [row.term, row]));
  const details = Object.fromEntries(
    Object.entries(analysis.details).map(([term, detail]) => [
      term,
      { ...detail, row: rowByTerm.get(term) ?? detail.row },
    ]),
  ) as Record<string, Detail>;
  return { ...analysis, rows, weights, details };
}

type SignalTone = "red" | "amber" | "blue" | "green" | "gray";

type ReviewSignal = {
  id: string;
  label: string;
  detail: string;
  tone: SignalTone;
};

type OneQuestionSignal = {
  label: string;
  detail: string;
  tone: SignalTone;
};

const SIGNAL_TONE_CLASSES: Record<SignalTone, string> = {
  red: "border-red-700/60 bg-red-900/30 text-red-200",
  amber: "border-amber-700/60 bg-amber-900/30 text-amber-200",
  blue: "border-blue-700/60 bg-blue-900/30 text-blue-200",
  green: "border-emerald-700/60 bg-emerald-900/30 text-emerald-200",
  gray: "border-gray-700 bg-gray-800 text-gray-300",
};

function reviewSignalsFor(row: KPIRow): ReviewSignal[] {
  const signals: ReviewSignal[] = [];

  if (row.n_results === 0) {
    signals.push({
      id: "no_hits",
      label: "no hits",
      detail: "Retrieval returned no declarable candidates. This is a strong intercept signal because the AI has no useful candidate set to reason over.",
      tone: "red",
    });
  } else {
    const lowHitCutoff = Math.min(row.k, 30) / 2;
    // Two-tier "few hits": red when n_results AND top_cosine are both weak
    // (retrieval failure → strong intercept), amber when only n_results is low
    // but cosines are healthy (narrow well-defined product → NOT intercept,
    // e.g. honey). Threshold 0.5 on top_cosine = "definitely above the 0.35
    // floor with some margin"; below that the match is suspect.
    if (row.n_results < lowHitCutoff) {
      const topCos = row.top_cosine ?? 0;
      const weakCosines = topCos > 0 && topCos < 0.5;
      if (weakCosines) {
        signals.push({
          id: "few_hits_weak_cosines",
          label: "few hits + weak cosines",
          detail: `Only ${row.n_results} hits vs K=${row.k}, and the strongest cosine is ${topCos.toFixed(3)} (barely above the threshold). Both signals point to retrieval failure — strong intercept candidate.`,
          tone: "red",
        });
      } else {
        signals.push({
          id: "few_hits",
          label: "few hits",
          detail: `Only ${row.n_results} hits vs K=${row.k}${topCos > 0 ? ` (top cosine ${topCos.toFixed(2)} — strong match)` : ""}. Low hit count with a strong top cosine usually means a narrow well-defined product (e.g. honey), NOT an intercept candidate.`,
          tone: "amber",
        });
      }
    }
    if (row.n_section >= 3) {
      signals.push({
        id: "cross_section",
        label: "cross-section",
        detail: `${row.n_section} sections represented. The term is spanning unrelated tariff domains.`,
        tone: "red",
      });
    } else if (row.n_section > 1) {
      signals.push({
        id: "section_split",
        label: "section split",
        detail: `${row.n_section} sections represented. The first decision may be broad but still reviewable.`,
        tone: "amber",
      });
    }
    if (row.n_chapter >= 6) {
      signals.push({
        id: "many_chapters",
        label: "many chapters",
        detail: `${row.n_chapter} chapters represented in top-K. This is hard for the AI to narrow without a strong product attribute.`,
        tone: "red",
      });
    } else if (row.n_chapter >= 3) {
      signals.push({
        id: "chapter_split",
        label: "chapter split",
        detail: `${row.n_chapter} chapters represented. This needs at least a chapter-level differentiator.`,
        tone: "amber",
      });
    }
    if (row.score_flatness >= 0.85 && row.n_results >= 5) {
      signals.push({
        id: "flat_scores",
        label: "flat scores",
        detail: "RRF scores are very close together, so retrieval has no clear anchor candidate.",
        tone: "amber",
      });
    }
    if (row.n_declarable >= 20) {
      signals.push({
        id: "many_leaves",
        label: "many leaves",
        detail: `${row.n_declarable} declarable codes represented. The candidate set is broad even after leaf filtering.`,
        tone: "amber",
      });
    }
    if (row.other_leaf_share >= 0.4) {
      signals.push({
        id: "nes_heavy",
        label: "n.e.s. heavy",
        detail: `${(row.other_leaf_share * 100).toFixed(0)}% of candidates land on n.e.s. destinations. Even a correct path parks the trader in a soft bucket — classification-outcome signal, not retrieval-quality.`,
        tone: "amber",
      });
    }
    if (row.worst_case_questions >= 4) {
      signals.push({
        id: "long_q_path",
        label: "long Q path",
        detail: `${row.worst_case_questions} turns of multi-option Q&A on the worst candidate path (4-option cap per turn). Exceeds a practical Q&A flow.`,
        tone: "red",
      });
    }
  }

  if (signals.length === 0) {
    signals.push({
      id: "concentrated",
      label: "concentrated",
      detail: "Candidates are relatively concentrated. This is a weak intercept signal unless manual review finds legal or product nuance.",
      tone: "green",
    });
  }

  return signals;
}

function oneQuestionSignalFor(row: KPIRow): OneQuestionSignal {
  if (row.n_results === 0) {
    return {
      label: "not assessable",
      detail: "There is no candidate tree to split. Treat this as retrieval failure rather than Q&A complexity.",
      tone: "gray",
    };
  }
  // Retrieval-failure guard: when hits are dramatically below K, the
  // structural tree isn't a meaningful object to "split". A worst_case<=1
  // tree built from 2 hits is technically one fork, but the fork is between
  // two noisy candidates — saying "one question would resolve it" implies
  // the question would be useful, which it isn't. Surface this as its own
  // signal so users don't read green "likely" next to a red complexity score.
  const lowHitCutoff = Math.min(row.k, 30) / 2;
  if (row.n_results < lowHitCutoff) {
    return {
      label: "n/a (vague)",
      detail: `Only ${row.n_results} hits vs K=${row.k}. The tree is too sparse for a one-question split to be meaningful — fix retrieval coverage (or intercept) before reasoning about Q&A burden.`,
      tone: "gray",
    };
  }
  if (row.n_results === 1) {
    return {
      label: "already narrow",
      detail: "Only one declarable candidate came back, so the issue is not tree disambiguation.",
      tone: "green",
    };
  }
  // Q-worst is now width-aware (4-option cap per turn). worst_case=1 ALREADY
  // implies the only fork on the walked path has width <= 4 — no need for a
  // separate "broad picklist" branch on widest_branch <= 12.
  if (row.worst_case_questions <= 1) {
    return {
      label: "likely",
      detail: "Worst walked path resolves in one multi-option turn (≤ 4 candidates at the fork). Strong candidate for a single attribute question.",
      tone: "green",
    };
  }
  if (
    row.worst_case_questions === 2 &&
    row.n_section <= 2 &&
    row.widest_branch <= 16
  ) {
    return {
      label: "maybe",
      detail: `Two turns on the worst path (4-option cap). Plausible to collapse with one good product-attribute question if it cuts across both forks. Widest fork anywhere in the tree: ${row.widest_branch}.`,
      tone: "amber",
    };
  }
  if (row.questions_expected <= 1.5 && row.n_section <= 2 && row.widest_branch <= 16) {
    return {
      label: "maybe",
      detail: "RRF-weighted entropy is low, so retrieval is fairly confident in the top candidate. One discriminating question may be enough despite multiple structural turns.",
      tone: "amber",
    };
  }
  return {
    label: "unlikely",
    detail: `${row.worst_case_questions} turns of multi-option Q&A worst-case (4-option cap). Too many turns, too much spread, or too wide a picklist for one question to be a reliable splitter.`,
    tone: "red",
  };
}

// Short, human-readable labels for the weight sliders. Keys match the
// composite weights so internal math is unchanged.
const WEIGHT_LABELS: Record<string, string> = {
  section_spread: "Section spread",
  chapter_spread: "Chapter spread",
  questions_expected: "Q rounds",
  unresolved_digits: "Unresolved",
  score_flatness: "Flatness",
  other_leaf_share: "n.e.s. dest",
  vagueness: "Vagueness",
};

const TOOLTIPS: Record<string, string> = {
  k: "How many candidates we keep AFTER fusion to compute KPIs over. Production HybridRetrievalService defaults to 30 — match prod to see what a real user gets. Lower = stricter view; higher = look deeper.",
  over_fetch:
    "How many candidates each leg (vector + BM25) fetches BEFORE RRF fusion. Over-fetching ensures the fusion has enough to work with. 200 is plenty; raising it costs DB time.",
  section_spread:
    "Penalises candidates spread across many Sections (Roman numerals, e.g. VII = Plastics, XVI = Machinery). Cross-section spread = worst retrieval failure (AI doesn't even know the domain).",
  chapter_spread:
    "Penalises spread across Chapters (2-digit). 'Steel' splits Ch 72/73 (raw vs articles) — same section but two chapters. Captures fine-grained 'right area, wrong chapter'.",
  questions_expected:
    "Expected number of yes/no questions to pin down a single code, weighted by how confident retrieval is in each candidate. THE direct measure of Q&A burden — weighted highest.",
  unresolved_digits:
    "10 - LCA (longest common prefix). If all candidates share '3926...' then 4 digits are locked in, 6 unresolved. Higher = more code structure to disambiguate.",
  score_flatness:
    "RRF score concentration. 1.0 = flat soup (no anchor); 0.0 = top result dominates. Secondary diagnostic — can be high for many close-but-correct candidates too.",
  other_leaf_share:
    "Fraction of top-K that land on n.e.s. ('not elsewhere specified') destination codes. CLASSIFICATION-OUTCOME signal, not retrieval-quality — AI-166 contextualised these codes so retrieval can match them. The penalty is only that the trader ends up in a soft bucket. Weighted lowest (0.05) accordingly.",
  vagueness:
    "RETRIEVAL-FAILURE signal: fires when n_results is low AND top cosine is weak. Catches 'gift set' / 'baby pink' (n=1, cos≈0.353 = barely passed threshold) without flagging narrow well-defined products like 'honey' (n=4, cos≈0.78). Formula: hits_norm × cosine_norm where hits_norm = (1 - n/K) and cosine_norm = (1 - (top_cos - threshold)/(1 - threshold)). Both factors must fire — multiplied, not summed.",
  analyze_selected:
    "Sync batch (≤30 terms typical). Calls POST /api/intercepts/analyze, waits for the full response.",
  analyze_all:
    "SSE-streamed bulk run via POST /api/intercepts/analyze/stream. Shows live progress; table fills in chunks of 10. ~9 min for all 728 terms at ~0.74s/term.",
  save_run:
    "Persist current analysis to data/intercept_runs/ so you can reload later without re-running retrieval. Useful for sharing snapshots or A/B-comparing weight schemes.",
  load_run:
    "Reload a previously saved analysis run from data/intercept_runs/. Retrieval doesn't re-execute — fast.",
};

export default function InterceptsPanel() {
  const [terms, setTerms] = useState<Term[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [k, setK] = useState(30);
  const [overFetch, setOverFetch] = useState(200);
  // Production default cosine threshold from AdminConfiguration.vector_score_threshold/100 = 0.35.
  // Lower → retrieval returns more (looser semantic match). 0.0 disables.
  const [vectorThreshold, setVectorThreshold] = useState(0.35);
  // Max options per multi-option question. Controls the worst_case_questions
  // metric: a fork of width W costs ceil(log_{maxOptions}(W)) turns. Default 4
  // reflects what the team treats as the realistic UX cap, but production has
  // no hard cap in the prompt — tunable.
  const [maxOptions, setMaxOptions] = useState(4);
  // Separate state for the all-commodities sweep — independent of the
  // intercept-list analysis above so both can be inspected simultaneously.
  const [commoditySweep, setCommoditySweep] = useState<{
    rows: KPIRow[];
    n_items: number;
    elapsed_seconds: number;
    sample_size: number | null;
    completed_at: string;
  } | null>(null);
  const [commoditySweeping, setCommoditySweeping] = useState(false);
  const [commodityProgress, setCommodityProgress] = useState<{ done: number; total: number; currentCode?: string } | null>(null);
  const [commodityDetails, setCommodityDetails] = useState<Record<string, Detail>>({});
  const [activeCommodityCode, setActiveCommodityCode] = useState<string | null>(null);

  // Lazy-fetch candidates when the user clicks a commodity row from a
  // loaded (rather than live-streamed) saved run. We don't pre-load
  // details for big runs because they're 800MB+.
  useEffect(() => {
    if (!activeCommodityCode) return;
    if (commodityDetails[activeCommodityCode]) return;  // already have it
    const runId = (window as any).__active_commodity_run_id;
    if (!runId) return;  // live sweep — details should already be present
    fetch(`/api/intercepts/runs/${runId}/candidates/${activeCommodityCode}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        setCommodityDetails((m) => ({
          ...m,
          [activeCommodityCode]: {
            row: data.row,
            top_candidates: data.top_candidates,
          },
        }));
      })
      .catch(() => {});
  }, [activeCommodityCode, commodityDetails]);
  const [weights, setWeights] = useState(DEFAULT_WEIGHTS);
  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number; currentTerm?: string } | null>(null);
  const [activeTerm, setActiveTerm] = useState<string | null>(null);
  const [savedRuns, setSavedRuns] = useState<SavedRun[]>([]);
  const [search, setSearch] = useState("");
  const [templateFilter, setTemplateFilter] = useState<string>("all");
  const [sortBy, setSortBy] = useState<keyof KPIRow>("composite");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // Hydrate terms + saved runs on mount.
  useEffect(() => {
    fetch("/api/intercepts/terms")
      .then((r) => r.json())
      .then(setTerms)
      .catch(() => {});
    refreshSavedRuns();
  }, []);

  function refreshSavedRuns() {
    fetch("/api/intercepts/runs")
      .then((r) => r.json())
      .then(setSavedRuns)
      .catch(() => {});
  }

  async function runAnalysis(indices: number[] | null) {
    setRunning(true);
    setProgress(null);
    setAnalysis(null);

    const body = JSON.stringify({
      indices,
      k,
      over_fetch: overFetch,
      weights,
      vector_threshold: vectorThreshold,
      max_options_per_question: maxOptions,
      allow_spend: true,
    });

    // Always stream rows over SSE so the progress bar works for any batch size.
    try {
      const ctrl = new AbortController();
      const res = await fetch("/api/intercepts/analyze/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      const liveRows: KPIRow[] = [];
      const liveDetails: Record<string, Detail> = {};
      const meta: { k: number; over_fetch: number; n_terms: number; weights: Record<string, number> } = {
        k,
        over_fetch: overFetch,
        n_terms: 0,
        weights,
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const chunks = buf.split("\n\n");
        buf = chunks.pop() || "";
        for (const chunk of chunks) {
          const lines = chunk.split("\n");
          const ev = lines.find((l) => l.startsWith("event:"))?.slice(6).trim() || "";
          const data = lines.find((l) => l.startsWith("data:"))?.slice(5).trim() || "{}";
          const parsed = JSON.parse(data);
          if (ev === "intercept:start") {
            meta.n_terms = parsed.n_terms;
            setProgress({ done: 0, total: parsed.n_terms });
          } else if (ev === "intercept:row") {
            liveRows.push(parsed.row);
            liveDetails[parsed.term] = {
              row: parsed.row,
              top_candidates: parsed.top_candidates,
              below_threshold_candidates: parsed.below_threshold_candidates,
              retrieval_meta: parsed.retrieval_meta,
            };
            setProgress((p) =>
              p ? { done: p.done + 1, total: p.total, currentTerm: parsed.term } : null,
            );
            // Stream UI update every term (small batches) or every 5 (large)
            const updateEvery = meta.n_terms > 50 ? 5 : 1;
            if (liveRows.length % updateEvery === 0 || liveRows.length === meta.n_terms) {
              setAnalysis({
                k: meta.k,
                over_fetch: meta.over_fetch,
                weights: meta.weights,
                n_terms: meta.n_terms,
                elapsed_seconds: 0,
                rows: [...liveRows],
                details: { ...liveDetails },
                completed_at: "",
              });
            }
          } else if (ev === "intercept:error") {
            console.warn("intercept error", parsed);
          } else if (ev === "intercept:done") {
            setAnalysis({
              k: meta.k,
              over_fetch: meta.over_fetch,
              weights: meta.weights,
              n_terms: meta.n_terms,
              elapsed_seconds: parsed.elapsed_seconds,
              rows: [...liveRows],
              details: { ...liveDetails },
              completed_at: new Date().toLocaleString(),
            });
            if (liveRows.length > 0 && !activeTerm) setActiveTerm(liveRows[0].term);
          }
        }
      }
    } catch (err) {
      alert(`Analyze failed: ${err}`);
    } finally {
      setRunning(false);
      setProgress(null);
    }
  }

  async function runCommoditySweep(sampleSize: number | null, queryStrategy: "self_text" | "paraphrase" = "self_text") {
    if (commoditySweeping) return;
    // Paraphrase strategy adds an LLM call per commodity, ~0.5s extra. self_text is retrieval-only.
    const perItemSec = queryStrategy === "paraphrase" ? 1.2 : 0.7;
    const n = sampleSize ?? 14300;
    const estMinutes = (n * perItemSec) / 60;
    const cost =
      queryStrategy === "paraphrase"
        ? n * 0.00012  // embedding + small LLM paraphrase
        : n * 0.00002; // embedding only
    const modeDesc =
      queryStrategy === "paraphrase"
        ? "CLASSIFICATION DIFFICULTY (LLM paraphrases each commodity's self_text into a realistic user query, then retrieves)"
        : "NEIGHBOUR DENSITY (feeds the commodity's own self_text into retrieval — measures sibling crowding, NOT user-facing difficulty)";
    const confirmMsg =
      sampleSize == null
        ? `Sweep ALL ~14.3k currently-in-force declarable commodities. Mode: ${modeDesc}. Estimated ~${estMinutes.toFixed(0)} min and ~$${cost.toFixed(2)} in API costs. Continue?`
        : `Sweep ${sampleSize} sampled commodities. Mode: ${modeDesc}. Estimated ~${estMinutes.toFixed(1)} min, ~$${cost.toFixed(2)}. Continue?`;
    if (!window.confirm(confirmMsg)) return;

    setCommoditySweeping(true);
    setCommodityProgress(null);
    setCommoditySweep(null);
    setCommodityDetails({});
    setActiveCommodityCode(null);

    const body = JSON.stringify({
      k,
      over_fetch: overFetch,
      weights,
      vector_threshold: vectorThreshold,
      max_options_per_question: maxOptions,
      sample_size: sampleSize,
      query_strategy: queryStrategy,
      allow_spend: true,
    });

    try {
      const res = await fetch("/api/intercepts/analyze_commodities/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      const rows: KPIRow[] = [];
      let nItems = 0;
      let sampleSizeFromStart: number | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const chunks = buf.split("\n\n");
        buf = chunks.pop() || "";
        for (const chunk of chunks) {
          const lines = chunk.split("\n");
          const ev = lines.find((l) => l.startsWith("event:"))?.slice(6).trim() || "";
          const data = lines.find((l) => l.startsWith("data:"))?.slice(5).trim() || "{}";
          const parsed = JSON.parse(data);
          if (ev === "commodity:start") {
            nItems = parsed.n_items;
            sampleSizeFromStart = parsed.sample_size ?? null;
            setCommodityProgress({ done: 0, total: nItems });
            // Seed an empty sweep object so the panel + chart render immediately
            // (dots appear as rows stream in, instead of waiting for the first
            // setCommoditySweep call after row 1).
            setCommoditySweep({
              rows: [],
              n_items: nItems,
              elapsed_seconds: 0,
              sample_size: sampleSizeFromStart,
              completed_at: "",
            });
          } else if (ev === "commodity:row") {
            rows.push(parsed.row);
            // Capture per-commodity detail so the user can click a dot and
            // get the same tree + top-candidates + reason chips as the 728
            // term view.
            setCommodityDetails((m) => ({
              ...m,
              [parsed.code]: {
                row: parsed.row,
                top_candidates: parsed.top_candidates,
                retrieval_meta: parsed.retrieval_meta,
                below_threshold_candidates: parsed.below_threshold_candidates,
              },
            }));
            setCommodityProgress((p) =>
              p ? { done: p.done + 1, total: p.total, currentCode: parsed.code } : null,
            );
            // Per-row state update. With backend emitting ~1 row every 700ms
            // and Recharts layout pass at ~50ms for 14k points, this leaves
            // plenty of frame headroom. React keys make existing <circle>s
            // stable across renders — we only add one DOM node per row,
            // not 14k.
            setCommoditySweep({
              rows: [...rows],
              n_items: nItems,
              elapsed_seconds: 0,
              sample_size: sampleSizeFromStart,
              completed_at: "",
            });
          } else if (ev === "commodity:error") {
            console.warn("commodity error", parsed);
          } else if (ev === "commodity:done") {
            setCommoditySweep({
              rows: [...rows],
              n_items: nItems,
              elapsed_seconds: parsed.elapsed_seconds,
              sample_size: sampleSizeFromStart,
              completed_at: new Date().toLocaleString(),
            });
          }
        }
      }
    } catch (err) {
      alert(`Commodity sweep failed: ${err}`);
    } finally {
      setCommoditySweeping(false);
      setCommodityProgress(null);
    }
  }

  async function saveCurrentRun() {
    if (!analysis) return;
    const name = prompt("Run name?", `intercept-${analysis.n_terms}terms-${new Date().toISOString().slice(0, 10)}`);
    if (!name) return;
    const snapshot = snapshotWithRows(analysis, reweightedRows, normalisedWeights);
    await fetch("/api/intercepts/runs/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, result: snapshot }),
    });
    refreshSavedRuns();
  }

  async function loadSavedRun(id: string) {
    if (!id) return;
    // Peek metadata first to decide if we need the lite endpoint (commodity
    // runs are 800MB+; full JSON crashes the browser).
    const meta = await fetch("/api/intercepts/runs").then((r) => r.json()).catch(() => []);
    const stub = meta.find((r: any) => r.id === id);
    const kind: string = stub?.kind || "term_analysis";
    const isCommodityKind = kind === "commodity_classification"
      || kind === "commodity_classification_gold_recall"
      || kind === "structural_complexity";
    if (isCommodityKind) {
      // Use the tiny scatter companion (~3MB) instead of the full run (~800MB).
      const scatterRes = await fetch(`/api/intercepts/runs/${id}/scatter`);
      if (!scatterRes.ok) {
        alert("This run has no scatter companion. Run make_scatter_companion.py to generate one.");
        return;
      }
      const scatter = await scatterRes.json();
      // Map scatter points to row shape so the existing components work
      const rows = (scatter.points || []).map((p: any) => ({
        term: p.term || p.code,
        code: p.code,
        chapter: p.chapter,
        composite: p.composite ?? 0,
        bucket: p.bucket,
        lane: p.lane,
        intercept_type: p.intercept_type,
        gold_rank: p.gold_rank,
        recall_pass: p.recall_pass,
        query_strategy: p.query_strategy,
        // Companion files don't ship the raw KPI fields (would 3x the size).
        // This flag tells recomputeCompositeRows to skip live-reweighting for
        // these rows — keep the saved composite instead of zeroing it out
        // when the scaffold's zeroed KPIs go through the formula.
        _scatter_companion: true,
        // Default scaffolding so the table/scatter don't crash on missing fields
        k: 30, n_results: 30,
        questions_max: 0, questions_expected: 0, inflexion_levels: 0,
        decision_points: 0, widest_branch: 0, worst_case_questions: 0,
        lca_digits: 0, unresolved_digits: 0, mean_indent_depth: 0,
        score_flatness: 0, other_leaf_share: 0,
        top_section: "", top_chapter: p.chapter, top_chapter_share: 1,
        section_chain: "", top_score: 0, bottom_score: 0,
        n_section: 1, n_chapter: 1, n_heading: 1, n_subheading: 1, n_eight_digit: 1, n_declarable: 1,
        entropy_section: 0, entropy_chapter: 0, entropy_heading: 0, entropy_subheading: 0, entropy_eight_digit: 0, entropy_declarable: 0,
      }));
      setCommoditySweep({
        rows,
        n_items: rows.length,
        elapsed_seconds: 0,
        sample_size: null,
        completed_at: "",
      });
      // Details are NOT pre-loaded for commodity runs (would be 800MB).
      // The CommoditySweepView will lazy-fetch one commodity's candidates
      // via /candidates/{code} when the user clicks a row.
      setCommodityDetails({});
      setActiveCommodityCode(null);
      setCommoditySweeping(false);
      setCommodityProgress(null);
      // Stash the run id so click-to-detail knows where to fetch candidates from.
      (window as any).__active_commodity_run_id = id;
      // Auto-load the most-recent 728-term analysis as an overlay so the
      // coloured intercept-template dots can be compared against the commodity
      // complexity distribution.
      if (!analysis) {
        try {
          const runsList = await fetch("/api/intercepts/runs").then((r) => r.json());
          const termRun = runsList.find((r: any) =>
            !r.kind ||
            r.kind === "term_analysis" ||
            (r.name && /728|terms/i.test(r.name) && !r.kind?.startsWith("commodity") && !r.kind?.startsWith("structural")),
          );
          if (termRun) {
            const tres = await fetch(`/api/intercepts/runs/${termRun.id}`);
            if (tres.ok) {
              const tdata = (await tres.json()) as AnalyzeResponse;
              setWeights(weightsFromRun(tdata.weights));
              setAnalysis(tdata);
            }
          }
        } catch {}
      }
      return;
    }
    // Default: term-analysis run (the 728-term flow — full run is small)
    const res = await fetch(`/api/intercepts/runs/${id}`);
    if (!res.ok) return;
    const data = (await res.json()) as AnalyzeResponse;
    setWeights(weightsFromRun(data.weights));
    setAnalysis(data);
    if (data.rows && data.rows.length > 0) setActiveTerm(data.rows[0].term);
  }

  // Auto-normalise weights so they always sum to 1.0 (sliders are ratios, not
  // absolutes — otherwise cranking one to 1.0 lets the composite exceed 1.0
  // and the [0,1] interpretation breaks).
  const normalisedWeights = useMemo(() => {
    const sum = Object.values(weights).reduce((a, b) => a + b, 0);
    if (sum <= 0) return { ...DEFAULT_WEIGHTS };
    return Object.fromEntries(
      Object.entries(weights).map(([k, v]) => [k, v / sum]),
    ) as typeof weights;
  }, [weights]);

  // Re-compute composite live as weights change (without re-querying retrieval).
  // This is also the saved snapshot source, so persisted runs match the visible
  // weight-tuned scores rather than the original backend scores.
  const reweightedRows = useMemo(() => {
    if (!analysis) return [];
    return recomputeCompositeRows(analysis.rows, normalisedWeights);
  }, [analysis, normalisedWeights]);

  const ranked = useMemo(() => {
    const filtered = reweightedRows.filter((r) => {
      if (templateFilter !== "all" && r.template !== templateFilter) return false;
      if (search && !r.term.toLowerCase().includes(search.toLowerCase())) return false;
      return true;
    });
    // Template gets severity-based ordering: Escalate (highest) >
    // Hard-to-classify > Generic > anything else. Numeric/string fields use
    // natural comparison.
    const templateRank: Record<string, number> = {
      Escalate: 3,
      "Hard-to-classify": 2,
      Generic: 1,
    };
    const sorted = [...filtered].sort((a, b) => {
      const sign = sortDir === "desc" ? -1 : 1;
      if (sortBy === "template") {
        const ra = templateRank[a.template || ""] ?? 0;
        const rb = templateRank[b.template || ""] ?? 0;
        return sign * (ra - rb);
      }
      const va = (a[sortBy] as number | string | undefined) ?? 0;
      const vb = (b[sortBy] as number | string | undefined) ?? 0;
      if (typeof va === "string" || typeof vb === "string") {
        return sign * String(va).localeCompare(String(vb));
      }
      return sign * ((va as number) - (vb as number));
    });
    return sorted;
  }, [reweightedRows, search, templateFilter, sortBy, sortDir]);

  const detail = activeTerm && analysis ? analysis.details[activeTerm] : null;
  const activeRow = ranked.find((r) => r.term === activeTerm);

  const scatterData = useMemo(() => {
    return ranked
      .filter((r) => Number.isFinite(r.composite))
      .map((r) => ({
        // Clamp to 1 so log scale doesn't reject the point. Terms with no
        // recorded search volume cluster at x=1 (left edge); terms with real
        // volume spread along the log axis. The tooltip shows the real count.
        x: Math.max(r.count || 0, 1),
        rawCount: r.count || 0,
        y: r.composite,
        term: r.term,
        template: r.template,
      }));
  }, [ranked]);

  const entropyChartData = useMemo(() => {
    if (!activeRow) return [];
    return LEVELS.map((lvl) => ({
      level: lvl,
      entropy: (activeRow as any)[`entropy_${lvl}`] as number,
      count: (activeRow as any)[`n_${lvl}`] as number,
    }));
  }, [activeRow]);

  const candidateTreeRoot = useMemo(() => {
    if (!detail?.top_candidates || detail.top_candidates.length === 0) return null;
    return buildCandidateTree(detail.top_candidates);
  }, [detail]);

  return (
    <div className="space-y-4 max-w-[120rem] mx-auto">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1 max-w-3xl">
          <h2 className="text-lg font-semibold">Intercept-list complexity analysis</h2>
          <p className="text-sm text-gray-400 leading-relaxed">
            {terms.length} terms loaded. Production-mirroring hybrid retrieval per term, with per-level
            spread + inflection counts to quantify how many Q&amp;A rounds AI search would need.
          </p>
        </div>
        <div className="flex gap-2 items-center shrink-0">
          <select
            className="bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm max-w-xs"
            defaultValue=""
            title={TOOLTIPS.load_run}
            onChange={(e) => loadSavedRun(e.target.value)}
          >
            <option value="">Load saved run…</option>
            {savedRuns.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name} ({r.n_terms} terms, {r.saved_at})
              </option>
            ))}
          </select>
          <button
            className="px-3 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 rounded disabled:opacity-40 whitespace-nowrap"
            onClick={saveCurrentRun}
            disabled={!analysis}
            title={TOOLTIPS.save_run}
          >
            Save run
          </button>
        </div>
      </header>

      {/* Run controls */}
      <div className="bg-gray-900 border border-gray-800 rounded">
        {/* Row 1: Retrieval params (left) + Actions (right) */}
        <div className="border-b border-gray-800 p-4 flex flex-col lg:flex-row gap-4 lg:items-end">
          <div className="flex-1 grid grid-cols-2 sm:grid-cols-4 gap-4 max-w-3xl">
            <div>
              <HelpLabel label="K (top-K)" tip={TOOLTIPS.k} />
              <input
                type="number"
                className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm font-mono"
                value={k}
                min={5}
                max={500}
                onChange={(e) => setK(Math.max(5, Math.min(500, parseInt(e.target.value) || 30)))}
              />
            </div>
            <div>
              <HelpLabel
                label="Cosine threshold"
                tip="Production AI search filters vector-leg candidates below this cosine similarity. Default 0.35 (matches prod admin config 'vector_score_threshold'). Lower it to see semantic matches AI would otherwise miss (useful when retrieval returns 0 results). 0.0 disables the filter entirely."
              />
              <div className="flex items-center gap-2">
                <input
                  type="range"
                  min={0}
                  max={0.7}
                  step={0.05}
                  value={vectorThreshold}
                  onChange={(e) => setVectorThreshold(parseFloat(e.target.value))}
                  className="flex-1"
                />
                <span className="text-sm font-mono text-gray-200 w-10 text-right">
                  {vectorThreshold.toFixed(2)}
                </span>
              </div>
              <div className="h-3 mt-0.5">
                {vectorThreshold !== 0.35 && (
                  <button
                    className="text-[10px] text-gray-500 hover:text-gray-300 underline"
                    onClick={() => setVectorThreshold(0.35)}
                  >
                    reset to prod (0.35)
                  </button>
                )}
              </div>
            </div>
            <div>
              <HelpLabel label="Over-fetch" tip={TOOLTIPS.over_fetch} />
              <input
                type="number"
                className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm font-mono"
                value={overFetch}
                min={50}
                max={500}
                onChange={(e) => setOverFetch(Math.max(50, Math.min(500, parseInt(e.target.value) || 200)))}
              />
            </div>
            <div>
              <HelpLabel
                label="Max options / Q"
                tip="Multi-option prompt cap. Each fork of width W on the walked path costs ceil(log_M(W)) turns, where M is this value. 4 = 'realistic UX cap' (default). 2 = binary splits only (worst case). 8/16 = relaxed picklists. Drives the 'Q worst' column."
              />
              <input
                type="number"
                className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm font-mono"
                value={maxOptions}
                min={2}
                max={32}
                onChange={(e) => setMaxOptions(Math.max(2, Math.min(32, parseInt(e.target.value) || 4)))}
              />
            </div>
          </div>
          <div className="flex gap-2 lg:ml-auto">
            <button
              className="px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-500 rounded disabled:opacity-40"
              disabled={running || selected.size === 0}
              title={TOOLTIPS.analyze_selected}
              onClick={() => runAnalysis([...selected])}
            >
              Analyze selected ({selected.size})
            </button>
            <button
              className="px-4 py-2 text-sm font-medium bg-purple-600 hover:bg-purple-500 rounded disabled:opacity-40"
              disabled={running}
              title={TOOLTIPS.analyze_all}
              onClick={() => runAnalysis(null)}
            >
              Analyze all 728
            </button>
            <div className="flex flex-col gap-1">
              <div className="text-[10px] uppercase text-gray-500 tracking-wide">Neighbour density (self_text → retrieval)</div>
              <div className="flex gap-2">
                <button
                  className="px-3 py-1.5 text-xs font-medium bg-teal-700 hover:bg-teal-600 rounded disabled:opacity-40"
                  disabled={running || commoditySweeping}
                  title="200-sample preview of sibling crowding. NOT classification difficulty."
                  onClick={() => runCommoditySweep(200, "self_text")}
                >
                  Preview 200
                </button>
                <button
                  className="px-3 py-1.5 text-xs font-medium bg-teal-800 hover:bg-teal-700 rounded disabled:opacity-40"
                  disabled={running || commoditySweeping}
                  title="Full 14k sibling-crowding sweep. ~2.5h, ~$0.30."
                  onClick={() => runCommoditySweep(null, "self_text")}
                >
                  All ~14k
                </button>
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <div className="text-[10px] uppercase text-amber-300 tracking-wide">Classification difficulty (paraphrase → retrieval)</div>
              <div className="flex gap-2">
                <button
                  className="px-3 py-1.5 text-xs font-medium bg-amber-700 hover:bg-amber-600 rounded disabled:opacity-40"
                  disabled={running || commoditySweeping}
                  title="LLM-paraphrases each commodity's self_text into an ordinary trader query, then retrieves. Applies the action-bucket classifier (A/B + recommended intercept type)."
                  onClick={() => runCommoditySweep(200, "paraphrase")}
                >
                  Preview 200
                </button>
                <button
                  className="px-3 py-1.5 text-xs font-medium bg-amber-800 hover:bg-amber-700 rounded disabled:opacity-40"
                  disabled={running || commoditySweeping}
                  title="Full 14k classification-difficulty sweep with paraphrased queries + action-bucket classifier. ~4-5h, ~$1.50."
                  onClick={() => runCommoditySweep(null, "paraphrase")}
                >
                  All ~14k
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Row 2: Composite weights */}
        <div className="p-4">
          <div className="flex items-baseline justify-between mb-3">
            <div>
              <div className="text-xs font-medium text-gray-300">Composite weights</div>
              <div className="text-[10px] text-gray-500">
                auto-normalised to 100% · sliders are ratios, not absolutes · re-rank is live
              </div>
            </div>
            <button
              className="text-[10px] text-gray-400 hover:text-gray-200 underline"
              onClick={() => setWeights(DEFAULT_WEIGHTS)}
              title="Reset weights to the defaults shown above"
            >
              Reset to defaults
            </button>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-x-4 gap-y-3">
            {Object.entries(weights).map(([key, val]) => {
              const normPct = (normalisedWeights[key as keyof typeof weights] || 0) * 100;
              return (
                <div key={key} className="min-w-0">
                  <HelpLabel label={WEIGHT_LABELS[key] || key} tip={TOOLTIPS[key] || ""} />
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={val}
                    className="w-full"
                    onChange={(e) =>
                      setWeights((w) => ({ ...w, [key]: parseFloat(e.target.value) }))
                    }
                  />
                  <div className="text-center font-mono text-sm text-gray-100 mt-0.5">
                    {normPct.toFixed(0)}%
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {running && progress && (
        <div className="bg-gray-900 border border-gray-800 rounded p-3">
          <div className="text-sm text-gray-400 mb-2">
            Running: {progress.done}/{progress.total} terms ({((progress.done / progress.total) * 100).toFixed(1)}%)
          </div>
          <div className="w-full h-2 bg-gray-800 rounded overflow-hidden">
            <div className="h-full bg-blue-500" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
          </div>
        </div>
      )}

      {commoditySweeping && (
        <div className="bg-teal-950 border border-teal-800 rounded p-3">
          <div className="text-sm text-teal-200 mb-2">
            {commodityProgress
              ? `Sweeping commodities: ${commodityProgress.done}/${commodityProgress.total} (${((commodityProgress.done / commodityProgress.total) * 100).toFixed(1)}%)${commodityProgress.currentCode ? ` · ${commodityProgress.currentCode}` : ""}`
              : "Sweep starting — fetching declarable commodity list from DB (this can take ~5–30s for full set)…"}
          </div>
          <div className="w-full h-2 bg-gray-800 rounded overflow-hidden">
            <div
              className={`h-full bg-teal-500 ${commodityProgress ? "" : "animate-pulse"}`}
              style={{ width: commodityProgress ? `${(commodityProgress.done / commodityProgress.total) * 100}%` : "100%" }}
            />
          </div>
        </div>
      )}

      {/* Filters */}
      {analysis && (
        <div className="flex items-center gap-3">
          <input
            type="text"
            placeholder="Search terms…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-gray-900 border border-gray-700 rounded px-3 py-1 text-sm w-64"
          />
          <select
            value={templateFilter}
            onChange={(e) => setTemplateFilter(e.target.value)}
            className="bg-gray-900 border border-gray-700 rounded px-3 py-1 text-sm"
          >
            <option value="all">All templates</option>
            <option value="Generic">Generic</option>
            <option value="Hard-to-classify">Hard-to-classify</option>
            <option value="Escalate">Escalate</option>
          </select>
          <div className="text-sm text-gray-400">
            {ranked.length} of {analysis.rows.length} rows shown
          </div>
        </div>
      )}

      {/* Term picker — always available. Auto-open if no analysis, collapsed otherwise. */}
      <details className="bg-gray-900 border border-gray-800 rounded" open={!analysis}>
        <summary className="px-4 py-3 cursor-pointer select-none text-sm font-medium text-gray-200 hover:bg-gray-800/40">
          {analysis ? `Pick more terms (${selected.size} selected)` : "Select terms to analyze"}
        </summary>
        <div className="border-t border-gray-800">
          <TermPickerEmptyState
            terms={terms}
            selected={selected}
            setSelected={setSelected}
            hasAnalysis={!!analysis}
          />
        </div>
      </details>

      {/* Main grid: table | detail */}
      {analysis && (
        <div className="grid grid-cols-12 gap-4">
          <div className="col-span-7">
            <RankedTable
              rows={ranked}
              activeTerm={activeTerm}
              onPick={setActiveTerm}
              sortBy={sortBy}
              sortDir={sortDir}
              onSort={(k) => {
                if (sortBy === k) setSortDir(sortDir === "desc" ? "asc" : "desc");
                else {
                  setSortBy(k);
                  setSortDir("desc");
                }
              }}
            />
          </div>
          <div className="col-span-5 space-y-3">
            {activeRow ? (
              <DetailPanel row={activeRow} detail={detail} entropyData={entropyChartData} candidateTree={candidateTreeRoot} />
            ) : (
              <div className="text-sm text-gray-500 p-6 border border-gray-800 rounded">
                Select a term from the table to see its KPIs and candidate breakdown.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Full-width tree visualisation (separate from the cramped detail
          column so it can stretch across the page). */}
      {analysis && activeRow && candidateTreeRoot && (
        <div className="bg-gray-900 border border-gray-800 rounded p-4">
          <div className="flex items-baseline justify-between mb-2">
            <h3 className="text-sm font-semibold">
              Candidate decision tree — <span className="font-mono">{activeRow.term}</span>
            </h3>
            <span className="text-[10px] text-gray-500">
              root at top → declarable codes at the bottom · orange-ringed boxes = decision points · click a node to lock + generate the real LLM question · scroll horizontally
            </span>
          </div>
          <TopDownTree root={candidateTreeRoot} term={activeRow.term} />
        </div>
      )}

      {/* Scatter */}
      {analysis && (
        <div className="bg-gray-900 border border-gray-800 rounded p-4">
          <h3 className="text-sm font-semibold mb-2">Complexity vs search volume</h3>
          <div className="text-xs text-gray-400 mb-3">
            High-volume + high-complexity = strong intercept candidates. Low-complexity = AI might handle without intercept.
          </div>
          <ResponsiveContainer width="100%" height={360}>
            <ScatterChart margin={{ top: 10, right: 30, left: 40, bottom: 50 }}>
              <CartesianGrid stroke="#1f2937" />
              <XAxis
                type="number"
                dataKey="x"
                name="Search volume"
                scale="log"
                domain={[1, "auto"]}
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                label={{ value: "Search volume (log)", position: "insideBottom", offset: -20, fill: "#9ca3af" }}
              />
              <YAxis
                type="number"
                dataKey="y"
                name="Complexity"
                domain={[0, 1]}
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                label={{ value: "Composite complexity", angle: -90, position: "insideLeft", offset: 10, fill: "#9ca3af" }}
              />
              <ZAxis range={[40, 40]} />
              <Tooltip
                contentStyle={{ background: "#111827", border: "1px solid #374151" }}
                content={({ payload }) => {
                  if (!payload || !payload.length) return null;
                  const p = payload[0].payload;
                  return (
                    <div className="text-xs bg-gray-900 border border-gray-700 p-2 rounded">
                      <div className="font-semibold">{p.term}</div>
                      <div className="text-gray-400">{p.template}</div>
                      <div>
                        volume {(p.rawCount ?? p.x).toLocaleString()}
                        {p.rawCount === 0 && <span className="text-gray-500"> (no recorded searches)</span>}
                        , complexity {p.y.toFixed(3)}
                      </div>
                    </div>
                  );
                }}
              />
              <Legend verticalAlign="top" align="right" wrapperStyle={{ paddingBottom: 8 }} />
              {["Generic", "Hard-to-classify", "Escalate"].map((tmpl) => (
                <Scatter
                  key={tmpl}
                  name={tmpl}
                  data={scatterData.filter((d) => d.template === tmpl)}
                  fill={TEMPLATE_COLORS[tmpl]}
                  onClick={(d: any) => setActiveTerm(d.term)}
                />
              ))}
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Commodity sweep + gold-recall audit moved to the Complexity tab. */}
    </div>
  );
}

// ---- Commodity sweep chart ---------------------------------------------
//
// Shows classification complexity across the commodity-code distribution, with
// intercept-list terms overlaid and coloured by template.

// ---- Gold-recall panel -------------------------------------------------
//
// Reads the most-recent gold-recall run (saved by gold_recall_audit.py) and
// renders three panels: aggregate recall stats, per-chapter fail-rate bar
// chart, and per-rank distribution. Doesn't load the full multi-MB run —
// uses the lightweight /recall_summary endpoint.

type GoldRecallSummary = {
  id: string;
  name: string;
  n_terms: number;
  total_pass: number;
  total_fail: number;
  rank_distribution: Record<string, number>;
  recall_metrics: { recall_at_1: number; recall_at_5: number; recall_at_10: number; recall_at_30: number } | null;
  chapters: { chapter: string; total: number; pass: number; fail: number; fail_rate: number }[];
};

export function GoldRecallPanel() {
  const [runs, setRuns] = useState<{ id: string; name: string }[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [summary, setSummary] = useState<GoldRecallSummary | null>(null);
  const [excludeSpecial, setExcludeSpecial] = useState(true);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch("/api/intercepts/runs")
      .then((r) => r.json())
      .then((all: any[]) => {
        const gr = all
          .filter((r) => r.kind === "commodity_classification_gold_recall")
          .map((r) => ({ id: r.id, name: r.name }));
        setRuns(gr);
        if (gr.length > 0 && !activeId) setActiveId(gr[0].id);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!activeId) {
      setSummary(null);
      return;
    }
    setLoading(true);
    fetch(`/api/intercepts/runs/${activeId}/recall_summary`)
      .then((r) => r.json())
      .then(setSummary)
      .catch(() => setSummary(null))
      .finally(() => setLoading(false));
  }, [activeId]);

  const chapters = useMemo(() => {
    if (!summary) return [];
    const rows = excludeSpecial
      ? summary.chapters.filter((c) => c.chapter !== "98" && c.chapter !== "99")
      : summary.chapters;
    return rows.map((c) => ({ ...c, fail_pct: c.fail_rate * 100 }));
  }, [summary, excludeSpecial]);

  const totals = useMemo(() => {
    if (!summary) return null;
    const filtered = excludeSpecial
      ? summary.chapters.filter((c) => c.chapter !== "98" && c.chapter !== "99")
      : summary.chapters;
    const total = filtered.reduce((a, c) => a + c.total, 0);
    const fail = filtered.reduce((a, c) => a + c.fail, 0);
    return { total, fail, pass: total - fail, fail_pct: total ? (fail / total) * 100 : 0 };
  }, [summary, excludeSpecial]);

  const worstChapters = useMemo(
    () => chapters.filter((c) => c.total >= 30).sort((a, b) => b.fail_rate - a.fail_rate).slice(0, 10),
    [chapters],
  );

  if (runs.length === 0) return null;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded p-4 space-y-3">
      <div className="flex items-baseline justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-100">Gold-recall audit</h3>
          <div className="text-[11px] text-gray-500">
            For each commodity, asks: does the gold code appear in the top-30 retrieved when fed a layperson paraphrase?
            This is the honest "is the system actually finding the right code?" metric.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-[11px] text-gray-400 flex items-center gap-1">
            <input
              type="checkbox"
              checked={excludeSpecial}
              onChange={(e) => setExcludeSpecial(e.target.checked)}
            />
            Exclude Ch 98/99
          </label>
          <select
            value={activeId || ""}
            onChange={(e) => setActiveId(e.target.value || null)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs"
          >
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {loading && <div className="text-xs text-gray-400">Loading…</div>}

      {summary && totals && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs">
            <Stat
              label="Codes"
              value={totals.total.toLocaleString()}
              hint={excludeSpecial ? "Excludes Ch 98 (special) and Ch 99" : "All commodities in the run"}
            />
            <Stat
              label="Recall@30"
              value={`${((totals.pass / totals.total) * 100).toFixed(1)}%`}
              hint="Fraction whose own code appears in top-30 of retrieval results."
              highlight
            />
            <Stat
              label="Recall@1"
              value={summary.recall_metrics ? `${(summary.recall_metrics.recall_at_1 * 100).toFixed(1)}%` : "-"}
              hint="Gold code appeared at rank 1."
            />
            <Stat
              label="Recall@5"
              value={summary.recall_metrics ? `${(summary.recall_metrics.recall_at_5 * 100).toFixed(1)}%` : "-"}
              hint="Gold code in top 5."
            />
            <Stat
              label="Failures"
              value={totals.fail.toLocaleString()}
              sub={`${totals.fail_pct.toFixed(1)}%`}
              hint="Commodities where the gold code didn't appear in top-30."
            />
          </div>

          <div>
            <div className="text-xs font-semibold text-gray-200 mb-1">Fail rate by chapter</div>
            <div className="text-[10px] text-gray-500 mb-2">
              Bars show % of commodities in each chapter whose gold code wasn't reachable in top-30. Hover for raw counts.
            </div>
            <ResponsiveContainer width="100%" height={280}>
              <BarChart data={chapters} margin={{ top: 8, right: 8, left: 8, bottom: 24 }}>
                <CartesianGrid stroke="#1f2937" />
                <XAxis
                  dataKey="chapter"
                  interval={4}
                  tick={{ fill: "#9ca3af", fontSize: 10 }}
                  label={{ value: "Chapter", position: "insideBottom", offset: -8, fill: "#9ca3af", fontSize: 11 }}
                />
                <YAxis
                  tick={{ fill: "#9ca3af", fontSize: 10 }}
                  unit="%"
                  label={{ value: "Fail rate", angle: -90, position: "insideLeft", offset: 12, fill: "#9ca3af", fontSize: 11 }}
                />
                <Tooltip
                  contentStyle={{ background: "#111827", border: "1px solid #374151" }}
                  content={({ payload }) => {
                    if (!payload || !payload.length) return null;
                    const p = payload[0].payload as any;
                    return (
                      <div className="text-xs bg-gray-900 border border-gray-700 p-2 rounded">
                        <div className="font-semibold">Chapter {p.chapter}</div>
                        <div>{p.fail}/{p.total} failed ({p.fail_pct.toFixed(1)}%)</div>
                        <div className="text-gray-500">{p.pass} pass</div>
                      </div>
                    );
                  }}
                />
                <Bar dataKey="fail_pct" fill="#f87171" />
              </BarChart>
            </ResponsiveContainer>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div>
              <div className="text-xs font-semibold text-gray-200 mb-1">Rank distribution</div>
              <div className="text-[10px] text-gray-500 mb-2">
                Where did the gold code land for the commodities where it WAS found?
              </div>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart
                  data={[
                    { rank: "rank 1", count: summary.rank_distribution["1"] || 0, fill: "#34d399" },
                    { rank: "rank 2-5", count: summary.rank_distribution["2-5"] || 0, fill: "#60a5fa" },
                    { rank: "rank 6-10", count: summary.rank_distribution["6-10"] || 0, fill: "#fbbf24" },
                    { rank: "rank 11-30", count: summary.rank_distribution["11-30"] || 0, fill: "#fb923c" },
                    { rank: "missed", count: summary.rank_distribution["miss"] || 0, fill: "#f87171" },
                  ]}
                  margin={{ top: 8, right: 8, left: 8, bottom: 8 }}
                >
                  <CartesianGrid stroke="#1f2937" />
                  <XAxis dataKey="rank" tick={{ fill: "#9ca3af", fontSize: 10 }} />
                  <YAxis tick={{ fill: "#9ca3af", fontSize: 10 }} />
                  <Tooltip contentStyle={{ background: "#111827", border: "1px solid #374151" }} />
                  <Bar dataKey="count" />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div>
              <div className="text-xs font-semibold text-gray-200 mb-1">Worst chapters (≥30 codes)</div>
              <div className="text-[10px] text-gray-500 mb-2">Top 10 sorted by fail rate. Map to real classification pain.</div>
              <table className="w-full text-xs">
                <thead className="text-gray-400">
                  <tr>
                    <th className="text-left p-1">Chapter</th>
                    <th className="text-right p-1">Codes</th>
                    <th className="text-right p-1">Fails</th>
                    <th className="text-right p-1">Fail %</th>
                  </tr>
                </thead>
                <tbody>
                  {worstChapters.map((c) => (
                    <tr key={c.chapter} className="border-t border-gray-800">
                      <td className="p-1 font-mono">{c.chapter}</td>
                      <td className="p-1 text-right">{c.total}</td>
                      <td className="p-1 text-right text-red-300">{c.fail}</td>
                      <td className="p-1 text-right">{(c.fail_rate * 100).toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}


export function CommoditySweepView({
  sweep,
  sweeping,
  progress,
  interceptRows,
  activeCode,
  onPickCommodity,
  details,
  weights,
}: {
  sweep: { rows: KPIRow[]; n_items: number; elapsed_seconds: number; sample_size: number | null; completed_at: string } | null;
  sweeping: boolean;
  progress: { done: number; total: number; currentCode?: string } | null;
  interceptRows: KPIRow[] | null;
  activeCode: string | null;
  onPickCommodity: (code: string | null) => void;
  details: Record<string, Detail>;
  weights: Record<string, number>;
}) {
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState<keyof KPIRow>("composite");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [chapterFilter, setChapterFilter] = useState<string>("all");

  // Reweight sweep rows live with the same composite formula used for the
  // 728 intercepts, so the grey commodity backdrop and the coloured overlays
  // stay on the same y-scale when the user moves the sliders.
  const rows = useMemo(() => {
    const base = sweep?.rows || [];
    if (!base.length) return base;
    return recomputeCompositeRows(base, weights);
  }, [sweep, weights]);

  // Filter + sort, same UX as the 728 view.
  const ranked = useMemo(() => {
    const filtered = rows.filter((r) => {
      const code = (r as any).code as string;
      if (chapterFilter !== "all" && (r as any).chapter !== chapterFilter) return false;
      if (search) {
        const q = search.toLowerCase();
        if (!(code || "").toLowerCase().includes(q) && !r.term.toLowerCase().includes(q)) return false;
      }
      return true;
    });
    const copy = [...filtered];
    copy.sort((a, b) => {
      const av = (a as any)[sortBy];
      const bv = (b as any)[sortBy];
      if (typeof av === "number" && typeof bv === "number") {
        return sortDir === "desc" ? bv - av : av - bv;
      }
      return sortDir === "desc"
        ? String(bv ?? "").localeCompare(String(av ?? ""))
        : String(av ?? "").localeCompare(String(bv ?? ""));
    });
    return copy;
  }, [rows, search, chapterFilter, sortBy, sortDir]);

  const activeRow = activeCode ? rows.find((r) => (r as any).code === activeCode) : undefined;
  const activeDetail = activeCode ? details[activeCode] : null;
  const activeEntropyData = useMemo(() => {
    if (!activeRow) return [];
    return LEVELS.map((lvl) => ({
      level: lvl,
      entropy: (activeRow as any)[`entropy_${lvl}`] as number,
      count: (activeRow as any)[`n_${lvl}`] as number,
    }));
  }, [activeRow]);
  const activeTree = useMemo(() => {
    if (!activeDetail?.top_candidates || activeDetail.top_candidates.length === 0) return null;
    return buildCandidateTree(activeDetail.top_candidates);
  }, [activeDetail]);

  // Distinct chapters present, sorted, for the filter dropdown.
  const chapters = useMemo(() => {
    const set = new Set<string>();
    for (const r of rows) {
      const ch = (r as any).chapter as string;
      if (ch) set.add(ch);
    }
    return Array.from(set).sort();
  }, [rows]);

  // Scroll into view once when the panel first appears.
  const rootRef = useRef<HTMLDivElement | null>(null);
  const scrolledRef = useRef(false);
  useEffect(() => {
    if (sweeping && !scrolledRef.current && rootRef.current) {
      rootRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
      scrolledRef.current = true;
    }
    if (!sweeping && !sweep) scrolledRef.current = false;
  }, [sweeping, sweep]);

  const commodityData = useMemo(() => {
    return rows
      .filter((r) => Number.isFinite(r.composite))
      .map((r) => {
        const code = (r as any).code as string;
        const ch = parseInt((r as any).chapter, 10);
        if (!Number.isFinite(ch)) return null;
        return {
          plotX: ch + stableJitter(code || r.term),
          y: r.composite,
          code,
          chapter: (r as any).chapter,
          section: (r as any).section,
        };
      })
      .filter((d): d is { plotX: number; y: number; code: string; chapter: string; section: string } => d !== null);
  }, [rows]);

  const interceptOverlay = useMemo(() => {
    const grouped: Record<string, { plotX: number; y: number; chapter: string; term: string; template: string }[]> = {
      Generic: [],
      "Hard-to-classify": [],
      Escalate: [],
    };
    if (!interceptRows) return grouped;
    for (const r of interceptRows) {
      if (!r.template || !grouped[r.template] || !Number.isFinite(r.composite)) continue;
      const ch = parseInt(((r as any).top_chapter || "").slice(0, 2), 10);
      // Retrieval miss (n_results=0 ⇒ no chapter): place in a gutter column
      // centred on x≈103 so they stay visible and keep their original HMRC
      // template colour. Wide horizontal jitter + small vertical jitter so
      // 23 stacked n=0 rows don't all sit on the same pixel.
      const inMissGutter = !Number.isFinite(ch);
      const j = stableJitter(r.term);
      const plotX = inMissGutter
        ? 103 + j * 5
        : ch + j;
      // For miss-gutter rows the composite is 1.0 (the blend rule maxes it);
      // nudge slightly downward by the jitter so dots fan out vertically.
      const y = inMissGutter
        ? Math.max(0.86, r.composite - Math.abs(j) * 0.12)
        : r.composite;
      grouped[r.template].push({
        plotX,
        y,
        chapter: inMissGutter ? "MISS" : String(ch).padStart(2, "0"),
        term: r.term,
        template: r.template,
      });
    }
    return grouped;
  }, [interceptRows]);

  const actionOverlay = useMemo(() => {
    const grouped: Record<string, { plotX: number; y: number; chapter: string; code: string; action: string; reason?: string }[]> = {
      "description.guidance": [],
    };
    for (const r of rows) {
      const code = (r as any).code as string;
      const ch = parseInt((r as any).chapter, 10);
      if (!Number.isFinite(ch) || !Number.isFinite(r.composite)) continue;
      const action = r.intercept_type;
      if (!action || !grouped[action]) continue;
      grouped[action].push({
        plotX: ch + stableJitter(code || r.term),
        y: r.composite,
        chapter: String(ch).padStart(2, "0"),
        code,
        action,
        reason: (r as any).bucket_reason,
      });
    }
    return grouped;
  }, [rows]);

  return (
    <div ref={rootRef} className="space-y-3">
      {/* Header bar: title + progress / completion summary */}
      <div className="bg-gray-900 border border-gray-800 rounded p-3 flex items-baseline justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-100">{(() => {
            const sample = rows[0] as any;
            if (typeof sample?.recall_pass === "boolean") return "Gold-recall classification audit";
            if (sample?.query_strategy === "paraphrase") return "Classification difficulty (LLM paraphrase)";
            if (sample?.query_strategy === "structural_only") return "Structural complexity";
            return "Commodity neighbour density (self_text → retrieval)";
          })()}</h3>
          <div className="text-[11px] text-gray-500">{(() => {
            const sample = rows[0] as any;
            if (typeof sample?.recall_pass === "boolean") {
              return "For each commodity, asks whether its own code shows up in top-30 retrieval given a layperson paraphrase. Bucket A = gold found, Bucket B = gold missed (real classification failure).";
            }
            if (sample?.query_strategy === "paraphrase") {
              return "Each commodity's self_text was LLM-paraphrased into an ordinary trader query, then retrieved. Measures whether retrieval converges narrowly — but note: 'narrow' ≠ 'correct'. Run the gold-recall audit for the honest classification check.";
            }
            if (sample?.query_strategy === "structural_only") {
              return "Pure tree-walk metric. No retrieval. Score = total Q&A turns from root to declarable under 4-option cap. Captures structural depth only.";
            }
            return "One row per declarable UK commodity. Uses each commodity's own AI-166 self_text as the retrieval query — measures description distinctiveness (sibling crowding), NOT user-facing classification difficulty.";
          })()}</div>
        </div>
        <div className="text-[11px] text-gray-400">
          {sweeping && !progress && <span className="text-teal-300">Fetching commodity list…</span>}
          {sweeping && progress && (
            <span className="text-teal-300">
              {progress.done}/{progress.total} ({((progress.done / progress.total) * 100).toFixed(0)}%)
              {progress.currentCode ? ` · ${progress.currentCode}` : ""}
            </span>
          )}
          {!sweeping && sweep && (
            <span>
              {sweep.rows.length} commodities
              {sweep.sample_size ? ` (sampled ${sweep.sample_size})` : ""}
              {sweep.elapsed_seconds > 0 && ` · ${(sweep.elapsed_seconds / 60).toFixed(1)} min`}
            </span>
          )}
        </div>
      </div>

      {/* Scatter chart — MOVED to top so it's visible without scrolling past
          the 14k-row table. */}
      {sweep && (
        <div className="bg-gray-900 border border-gray-800 rounded p-4">
          <div className="text-xs text-gray-400 mb-3">
            Grey dots = commodity codes in this run. Legacy template colours (Generic / Hard-to-classify / Escalate) = HMRC's 728 curated intercept-list terms, never re-classified.
            "Context dependant" = bucket-B commodities whose walked path has legal/lab/expert predicates (chapter notes, regulation refs, lab-measurable attributes) — the trader can't answer the differentiator without external context. The "MISS" column at the far right holds the 728 intercept terms with n_results=0 — same HMRC template colour as the rest, just placed in a gutter because they have no chapter to anchor on.
            X-axis = chapter (commodity chapter for grey dots and triangles; top retrieved chapter for intercept-term circles), visually spread inside each chapter to reduce overlap.
            Y-axis = classification-complexity composite.{" "}
            <strong className="text-teal-300">Click a grey commodity dot</strong> to drill into the tree, candidates, and reason chips for that commodity.
          </div>
          <ResponsiveContainer width="100%" height={420}>
            <ScatterChart margin={{ top: 10, right: 30, left: 40, bottom: 50 }}>
              <CartesianGrid stroke="#1f2937" />
              {/* Shade the MISS gutter so it reads as a distinct column. */}
              <ReferenceArea x1={100} x2={105} y1={0} y2={1} fill="#dc2626" fillOpacity={0.06} />
              <ReferenceLine x={100} stroke="#7f1d1d" strokeDasharray="4 4" />
              <XAxis
                type="number"
                dataKey="plotX"
                name="Chapter"
                domain={[1, 105]}
                ticks={[1, 10, 20, 30, 40, 50, 60, 70, 80, 90, 99, 102]}
                tickFormatter={(v) => (Math.round(v) === 102 ? "MISS" : String(Math.round(v)))}
                allowDecimals={false}
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                label={{ value: "Chapter", position: "insideBottom", offset: -20, fill: "#9ca3af" }}
              />
              <YAxis
                type="number"
                dataKey="y"
                name="Composite complexity"
                domain={[0, 1]}
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                label={{ value: "Composite complexity", angle: -90, position: "insideLeft", offset: 10, fill: "#9ca3af" }}
              />
              <ZAxis range={[35, 35]} />
              <Tooltip
                contentStyle={{ background: "#111827", border: "1px solid #374151" }}
                content={({ payload }) => {
                  if (!payload || !payload.length) return null;
                  const p = payload[0].payload as any;
                  const source = p.term ? "Intercept term" : p.action ? "Bucket-B commodity" : "Commodity";
                  return (
                    <div className="text-xs bg-gray-900 border border-gray-700 p-2 rounded">
                      <div className="font-semibold">{p.code || p.term}</div>
                      <div className="text-gray-500">{source}</div>
                      {p.template && <div className="text-gray-400">Template: {p.template}</div>}
                      {p.action && <div className="text-gray-400">Action: {ACTION_LABELS[p.action]?.replace("Action: ", "") ?? p.action}</div>}
                      {p.reason && <div className="text-gray-500 italic">{p.reason}</div>}
                      <div>chapter {p.chapter ?? Math.round(p.plotX)}, complexity {p.y.toFixed(3)}</div>
                    </div>
                  );
                }}
              />
              <Legend verticalAlign="top" align="right" wrapperStyle={{ paddingBottom: 8 }} />
              <Scatter
                name="Commodity codes"
                data={commodityData}
                fill="#9ca3af"
                opacity={0.45}
                onClick={(d: any) => onPickCommodity(d?.code ?? null)}
                cursor="pointer"
              />
              {["Generic", "Hard-to-classify", "Escalate"].map((tmpl) => (
                <Scatter
                  key={tmpl}
                  name={`Intercept: ${tmpl}`}
                  data={interceptOverlay[tmpl] || []}
                  fill={TEMPLATE_COLORS[tmpl]}
                  opacity={0.95}
                />
              ))}
              {ACTION_KEYS.map((act) => (
                <Scatter
                  key={act}
                  name={ACTION_LABELS[act]}
                  data={actionOverlay[act] || []}
                  fill={ACTION_COLORS[act]}
                  fillOpacity={0.18}
                  stroke={ACTION_COLORS[act]}
                  strokeWidth={1.5}
                  opacity={0.9}
                />
              ))}
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Filters bar — mirrors the 728 view */}
      {sweep && sweep.rows.length > 0 && (
        <div className="flex items-center gap-3">
          <input
            type="text"
            placeholder="Search code or description…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="flex-1 max-w-md bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm"
          />
          <select
            value={chapterFilter}
            onChange={(e) => setChapterFilter(e.target.value)}
            className="bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm"
          >
            <option value="all">All chapters</option>
            {chapters.map((c) => (
              <option key={c} value={c}>
                Ch {c}
              </option>
            ))}
          </select>
          <div className="text-sm text-gray-400">
            {ranked.length} of {rows.length} rows shown
          </div>
        </div>
      )}

      {/* Table + DetailPanel grid */}
      {sweep && sweep.rows.length > 0 && (
        <div className="grid grid-cols-12 gap-4">
          <div className="col-span-7">
            <RankedTable
              rows={ranked}
              activeTerm={activeCode}
              onPick={(id) => onPickCommodity(id === activeCode ? null : id)}
              sortBy={sortBy}
              sortDir={sortDir}
              onSort={(k) => {
                if (sortBy === k) setSortDir(sortDir === "desc" ? "asc" : "desc");
                else { setSortBy(k); setSortDir("desc"); }
              }}
              keyField={"code" as keyof KPIRow}
              variant="commodities"
            />
          </div>
          <div className="col-span-5 space-y-3">
            {activeRow && activeDetail ? (
              <DetailPanel
                row={activeRow}
                detail={activeDetail}
                entropyData={activeEntropyData}
                candidateTree={activeTree}
              />
            ) : (
              <div className="text-sm text-gray-500 p-6 border border-gray-800 rounded">
                Select a commodity from the table to see its KPIs and candidate breakdown.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Full-width tree */}
      {activeRow && activeTree && (
        <div className="bg-gray-900 border border-gray-800 rounded p-4">
          <div className="flex items-baseline justify-between mb-2">
            <h3 className="text-sm font-semibold">
              Candidate decision tree — <span className="font-mono">{activeCode}</span>{" "}
              <span className="text-gray-400 font-normal">{activeRow.term.slice(0, 80)}</span>
            </h3>
          </div>
          <TopDownTree root={activeTree} term={activeRow.term} />
        </div>
      )}

    </div>
  );
}

// ---- Sub-components -----------------------------------------------------

function TermPickerEmptyState({
  terms,
  selected,
  setSelected,
  hasAnalysis,
}: {
  terms: Term[];
  selected: Set<number>;
  setSelected: (s: Set<number>) => void;
  hasAnalysis?: boolean;
}) {
  const [search, setSearch] = useState("");
  const [templateFilter, setTemplateFilter] = useState<string>("all");
  const [sortBy, setSortBy] = useState<"term" | "count" | "template">("term");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const templateRank: Record<string, number> = {
    Escalate: 3,
    "Hard-to-classify": 2,
    Generic: 1,
  };

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    const subset = terms.filter((t) => {
      if (templateFilter !== "all" && t.template !== templateFilter) return false;
      if (q && !t.term.toLowerCase().includes(q)) return false;
      return true;
    });
    const sign = sortDir === "asc" ? 1 : -1;
    return [...subset].sort((a, b) => {
      if (sortBy === "term") return sign * a.term.localeCompare(b.term);
      if (sortBy === "count") return sign * ((a.count || 0) - (b.count || 0));
      if (sortBy === "template") {
        return sign * ((templateRank[a.template] || 0) - (templateRank[b.template] || 0));
      }
      return 0;
    });
  }, [terms, search, templateFilter, sortBy, sortDir]);

  const toggleSort = (key: "term" | "count" | "template") => {
    if (sortBy === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(key);
      // Sensible defaults: term=asc (alphabetical), count=desc (highest first),
      // template=desc (Escalate first via severity rank).
      setSortDir(key === "term" ? "asc" : "desc");
    }
  };

  const arrow = (key: "term" | "count" | "template") => {
    if (sortBy !== key) return <span className="text-gray-600 ml-1">↕</span>;
    return <span className="text-gray-200 ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>;
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded p-4">
      <div className="text-sm text-gray-400 mb-3">
        {hasAnalysis
          ? `Pick more terms below to add to / replace the current analysis. ${selected.size} selected.`
          : `No analysis loaded. Pick a few terms (use the checkboxes) and click "Analyze selected", or run all ${terms.length} at once.`}
      </div>
      <div className="flex items-center gap-3 mb-2">
        <input
          type="text"
          placeholder="Filter terms…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="bg-gray-800 border border-gray-700 rounded px-3 py-1 text-sm w-64"
        />
        <select
          value={templateFilter}
          onChange={(e) => setTemplateFilter(e.target.value)}
          className="bg-gray-800 border border-gray-700 rounded px-3 py-1 text-sm"
        >
          <option value="all">All templates</option>
          <option value="Generic">Generic</option>
          <option value="Hard-to-classify">Hard-to-classify</option>
          <option value="Escalate">Escalate</option>
        </select>
        <span className="text-xs text-gray-500">
          {filtered.length} / {terms.length} shown · {selected.size} selected
        </span>
        {selected.size > 0 && (
          <button
            onClick={() => setSelected(new Set())}
            className="text-[11px] text-gray-400 hover:text-gray-200 underline"
          >
            Clear selection
          </button>
        )}
      </div>
      <div className="max-h-[28rem] overflow-auto">
        <table className="w-full text-xs">
          <thead className="text-gray-400 sticky top-0 bg-gray-900 select-none">
            <tr>
              <th className="text-left p-1 w-8"></th>
              <th
                className="text-left p-1 cursor-pointer hover:bg-gray-800/60"
                onClick={() => toggleSort("term")}
                title="Sort by term name"
              >
                Term{arrow("term")}
              </th>
              <th
                className="text-right p-1 cursor-pointer hover:bg-gray-800/60"
                onClick={() => toggleSort("count")}
                title="Sort by search volume"
              >
                Volume{arrow("count")}
              </th>
              <th
                className="text-left p-1 cursor-pointer hover:bg-gray-800/60"
                onClick={() => toggleSort("template")}
                title="Sort by template (severity: Escalate > Hard-to-classify > Generic)"
              >
                Template{arrow("template")}
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t) => (
              <tr
                key={t.index}
                className="border-t border-gray-800 hover:bg-gray-800/50 cursor-pointer"
                onClick={() => {
                  const next = new Set(selected);
                  if (next.has(t.index)) next.delete(t.index);
                  else next.add(t.index);
                  setSelected(next);
                }}
              >
                <td className="p-1">
                  <input type="checkbox" checked={selected.has(t.index)} readOnly />
                </td>
                <td className="p-1">{t.term}</td>
                <td className="p-1 text-right">{t.count.toLocaleString()}</td>
                <td className="p-1">
                  <span
                    className="px-2 py-0.5 rounded text-[10px]"
                    style={{ background: TEMPLATE_COLORS[t.template] + "33", color: TEMPLATE_COLORS[t.template] }}
                  >
                    {t.template}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RankedTable({
  rows,
  activeTerm,
  onPick,
  sortBy,
  sortDir,
  onSort,
  keyField = "term",
  variant = "terms",
}: {
  rows: KPIRow[];
  activeTerm: string | null;
  onPick: (term: string) => void;
  sortBy: keyof KPIRow;
  sortDir: "asc" | "desc";
  onSort: (k: keyof KPIRow) => void;
  keyField?: keyof KPIRow;
  variant?: "terms" | "commodities";
}) {
  // Commodity rows have `code` + chapter/section instead of count/template.
  // Same KPI columns; first two columns differ.
  const cols: { key: keyof KPIRow; label: string; w?: string; fmt?: (v: any) => string }[] =
    variant === "commodities"
      ? [
          { key: "code" as keyof KPIRow, label: "Code", w: "min-w-[7rem]" },
          { key: "term", label: "Query used", w: "min-w-[12rem]", fmt: (v) => String(v ?? "").slice(0, 60) },
          { key: "chapter" as keyof KPIRow, label: "Ch" },
          { key: "bucket" as keyof KPIRow, label: "Bucket", fmt: (v) => v ?? "-" },
          { key: "intercept_type" as keyof KPIRow, label: "Action", fmt: (v) => v ?? "-" },
          // "composite" is the backend complexity proxy for the selected run.
          { key: "composite", label: "Complexity", fmt: (v) => v.toFixed(3) },
          { key: "n_results", label: "Hits", fmt: (v) => v },
          { key: "top_cosine", label: "Top cos", fmt: (v) => (v != null ? v.toFixed(2) : "-") },
          { key: "vagueness", label: "Vague", fmt: (v) => (v != null ? v.toFixed(2) : "-") },
          { key: "worst_case_questions", label: "Q worst", fmt: (v) => `≤ ${v}` },
          { key: "decision_points", label: "Forks", fmt: (v) => v },
          { key: "widest_branch", label: "Widest" },
          { key: "n_section", label: "Sec" },
          { key: "n_chapter", label: "Chap" },
          { key: "n_heading", label: "Hd" },
          { key: "other_leaf_share", label: "Other", fmt: (v) => `${(v * 100).toFixed(0)}%` },
        ]
      : [
          { key: "term", label: "Term", w: "min-w-[10rem]" },
          { key: "template", label: "Template" },
          { key: "count", label: "Volume", fmt: (v) => (v ?? 0).toLocaleString() },
          { key: "composite", label: "Complexity", fmt: (v) => v.toFixed(3) },
          { key: "n_results", label: "Hits", fmt: (v) => v },
          { key: "top_cosine", label: "Top cos", fmt: (v) => (v != null ? v.toFixed(2) : "-") },
          { key: "vagueness", label: "Vague", fmt: (v) => (v != null ? v.toFixed(2) : "-") },
          { key: "worst_case_questions", label: "Q worst", fmt: (v) => `≤ ${v}` },
          { key: "decision_points", label: "Forks", fmt: (v) => v },
          { key: "widest_branch", label: "Widest" },
          { key: "n_section", label: "Sec" },
          { key: "n_chapter", label: "Ch" },
          { key: "n_heading", label: "Hd" },
          { key: "other_leaf_share", label: "Other", fmt: (v) => `${(v * 100).toFixed(0)}%` },
        ];

  return (
    <div className="border border-gray-800 rounded overflow-hidden">
      <div className="max-h-[40rem] overflow-auto">
        <table className="w-full text-xs">
          <thead className="bg-gray-900 sticky top-0 z-10">
            <tr>
              {cols.map((c) => (
                <th
                  key={String(c.key)}
                  className={`text-left p-2 cursor-pointer hover:bg-gray-800 ${c.w || ""}`}
                  onClick={() => onSort(c.key)}
                >
                  {c.label} {sortBy === c.key && (sortDir === "desc" ? "↓" : "↑")}
                </th>
              ))}
              <th className="text-left p-2 min-w-[12rem]">Signals</th>
              <th className="text-left p-2 whitespace-nowrap">1Q split</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const id = String((r as any)[keyField] ?? r.term);
              return (
              <tr
                key={id}
                onClick={() => onPick(id)}
                className={`border-t border-gray-800 cursor-pointer hover:bg-gray-800/40 ${
                  activeTerm === id ? "bg-blue-900/30" : ""
                } ${r.n_results === 0 ? "bg-red-900/15" : ""}`}
              >
                {cols.map((c) => {
                  const v = (r as any)[c.key];
                  let display: string;
                  if (c.fmt) display = c.fmt(v);
                  else display = v === undefined || v === null ? "" : String(v);
                  let extra = "";
                  if ((c.key === "term" || c.key === ("code" as any)) && r.n_results === 0) {
                    return (
                      <td key={String(c.key)} className="p-2">
                        <span className="flex items-center gap-1">
                          <span title="Retrieval returned no candidates" className="text-red-400">⚠</span>
                          <span>{display}</span>
                        </span>
                      </td>
                    );
                  }
                  if (c.key === "template" && typeof v === "string") {
                    return (
                      <td key={String(c.key)} className="p-2">
                        <span
                          className="px-2 py-0.5 rounded text-[10px]"
                          style={{
                            background: TEMPLATE_COLORS[v] + "33",
                            color: TEMPLATE_COLORS[v],
                          }}
                        >
                          {v}
                        </span>
                      </td>
                    );
                  }
                  if (c.key === ("bucket" as any) && typeof v === "string") {
                    const isA = v === "A";
                    return (
                      <td key={String(c.key)} className="p-2">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-semibold ${
                            isA ? "bg-emerald-900/40 text-emerald-300 border border-emerald-800" : "bg-amber-900/40 text-amber-300 border border-amber-800"
                          }`}
                          title={r.bucket_reason || ""}
                        >
                          {isA ? "A · AI ok" : "B · intervene"}
                        </span>
                      </td>
                    );
                  }
                  if (c.key === ("intercept_type" as any) && typeof v === "string") {
                    // Colour-code action types
                    const palette: Record<string, string> = {
                      "description.exclude": "bg-red-900/40 text-red-300 border border-red-800",
                      "description.filter": "bg-blue-900/40 text-blue-300 border border-blue-800",
                      "description.guidance": "bg-purple-900/40 text-purple-300 border border-purple-800",
                      "commodity.exclude": "bg-red-900/40 text-red-300 border border-red-800",
                      "commodity.message": "bg-purple-900/40 text-purple-300 border border-purple-800",
                    };
                    const lane = r.lane || "";
                    const isLane = lane === "annotate_ai166_fix";
                    return (
                      <td key={String(c.key)} className="p-2">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-mono ${
                            isLane
                              ? "bg-yellow-900/40 text-yellow-300 border border-yellow-800"
                              : palette[v] || "bg-gray-800 text-gray-300"
                          }`}
                          title={r.bucket_reason || ""}
                        >
                          {isLane ? "ai166 fix" : v}
                        </span>
                      </td>
                    );
                  }
                  if (c.key === "composite" && typeof v === "number") {
                    const intensity = Math.min(1, v);
                    extra = `bg-red-500/[${(intensity * 0.3).toFixed(2)}]`;
                  }
                  return (
                    <td key={String(c.key)} className={`p-2 ${extra}`}>
                      {display}
                    </td>
                  );
                })}
                <td className="p-2">
                  <SignalBadges signals={reviewSignalsFor(r)} max={3} />
                </td>
                <td className="p-2">
                  <OneQuestionBadge signal={oneQuestionSignalFor(r)} />
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DetailPanel({
  row,
  detail,
  entropyData,
  candidateTree,
}: {
  row: KPIRow;
  detail: Detail | null;
  entropyData: { level: string; entropy: number; count: number }[];
  candidateTree: TreeNode | null;
}) {
  const retrievalFailed = row.n_results === 0;
  const belowThreshold = detail?.below_threshold_candidates || [];
  const reviewSignals = reviewSignalsFor(row);
  const oneQuestionSignal = oneQuestionSignalFor(row);

  return (
    <div className="space-y-3">
      {retrievalFailed && (
        <div className="bg-red-900/20 border border-red-700/60 rounded p-4 space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-red-300 text-base">⚠</span>
            <strong className="text-red-200">Retrieval returned no candidates</strong>
          </div>
          <p className="text-xs text-gray-300">
            Both retrieval legs came back empty for <span className="font-mono">"{row.term}"</span> at the
            current cosine threshold. This is a strong signal that AI search genuinely can't handle this
            term — <strong>the intercept is doing real work</strong>.
          </p>
          {belowThreshold.length > 0 && (
            <div className="border-t border-red-800/50 pt-2 mt-2">
              <div className="text-[11px] text-red-200 mb-1">
                What AI <em>almost</em> matched (below the {detail?.retrieval_meta?.vector_threshold_used ?? 0.35}{" "}
                cosine threshold):
              </div>
              <table className="w-full text-[11px]">
                <tbody>
                  {belowThreshold.slice(0, 5).map((b, i) => (
                    <tr key={i} className="border-t border-red-900/40">
                      <td className="p-1 font-mono text-gray-400">{b.goods_nomenclature_item_id}</td>
                      <td className="p-1 text-right font-mono text-amber-300">{b.cosine_score.toFixed(3)}</td>
                      <td className="p-1 truncate max-w-md text-gray-400">
                        {(b.search_text || b.self_text || "").slice(0, 80)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-[10px] text-gray-400 mt-1">
                Drag the Cosine threshold slider lower to include these — useful for understanding what AI
                <em> would </em>find with looser semantic matching.
              </p>
            </div>
          )}
        </div>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded p-4">
        <h3 className="text-base font-semibold">{row.term}</h3>
        <div className="text-xs text-gray-400 mt-1">
          {row.template} · {(row.count || 0).toLocaleString()} searches · top section <strong>{row.top_section || "—"}</strong>,
          top chapter <strong>{row.top_chapter || "—"}</strong>
          {row.top_chapter_share > 0 && ` (${(row.top_chapter_share * 100).toFixed(0)}%)`}
        </div>
        <div className="text-xs text-gray-300 mt-1">Section chain: {row.section_chain || "—"}</div>
          <div className="grid grid-cols-3 gap-2 mt-3 text-xs">
          <Stat
            label="Complexity"
            value={row.composite.toFixed(3)}
            highlight
            hint="Composite score in [0,1]. Weighted blend of all KPIs below (weights set by the sliders at top). Higher = harder for AI to handle without an intercept. Note: 'too vague to retrieve' shows up here as high complexity too — when retrieval returns very few hits, spread/flatness/entropy norms max out even though the LLM has few structural forks to walk. That's by design: vague-and-unclassifiable IS the intercept signal."
          />
          <Stat
            label="Retrieval hits"
            value={String(row.n_results)}
            sub={row.top_cosine != null ? `top cos ${row.top_cosine.toFixed(2)}` : undefined}
            hint={`Total candidates returned by retrieval (vector + BM25 fused, post-threshold, post-leaf filter). Compare to K=${row.k}. Hits << K can mean either (a) the term is vague and retrieval failed (top cosine near the threshold = intercept candidate) or (b) the product is narrow and well-defined (top cosine high = NOT intercept). The 'top cos' sub-figure distinguishes the two.`}
          />
          <Stat
            label="Vagueness"
            value={(row.vagueness ?? 0).toFixed(2)}
            hint="Combined retrieval-failure signal: (1 - hits/K) × (1 - cosine_strength). 1.0 = total retrieval failure (n=0 or n=1 with cosine at threshold). 0 = healthy retrieval. Catches 'gift set'/'baby pink' where spread/entropy norms otherwise collapse to 0 and hide an obvious intercept candidate. Weighted at 0.20 in the composite by default."
          />
          <Stat
            label="Worst-case Qs"
            value={`≤ ${row.worst_case_questions}`}
            sub={row.worst_case_bits != null ? `${row.worst_case_bits.toFixed(1)} bits` : undefined}
            hint="Worst-case TURN count along any single root-to-leaf path, assuming prod's max 4 options per question. A fork of width W costs ceil(log_4(W)) turns: binary fork = 1, width-12 = 2, width-50 = 3, width-100 = 4. Walked-path only (forks on parallel branches the LLM never visits don't cost it turns). The 'bits' sub-figure is the cap-independent information cost (Σ log2(width)) — useful when comparing terms regardless of the option cap. NOTE: low Q-worst + high complexity = retrieval-failure case (term too vague to find candidates), not a low-effort case."
          />
          <Stat
            label="Forks (total tree)"
            value={`${row.decision_points}`}
            hint="Total branching nodes anywhere in the candidate tree. Higher than worst-case Qs because most forks sit on parallel branches the LLM never walks. Useful as a 'how messy is the candidate space overall' signal."
          />
          <Stat
            label="Widest fork"
            value={`${row.widest_branch} options`}
            hint="The biggest single branching point. 12 headings under a chapter = one multi-option question with 12 options. Wider forks = bigger picklists."
          />
          <Stat
            label="Sections"
            value={String(row.n_section)}
            hint="Distinct Sections (Roman-numeral level above chapters, e.g. VII = Plastics, XVI = Machinery) represented in the top-K. >1 means retrieval is split across business domains — strong intercept signal."
          />
          <Stat
            label="Chapters"
            value={String(row.n_chapter)}
            hint="Distinct 2-digit Chapters in top-K. >1 means even within the right section AI has to pick a chapter."
          />
          <Stat
            label="Headings"
            value={String(row.n_heading)}
            hint="Distinct 4-digit Headings in top-K. Common to be high (10-15) for vague terms within a single chapter."
          />
          <Stat
            label="Shared prefix"
            value={`${row.lca_digits} / 10 digits`}
            hint="LCA = Longest Common Ancestor: how many leading digits ALL candidates share. 0 = candidates start with totally different chapters. 4 = all share heading. 10 = single declarable. Higher = retrieval is more concentrated."
          />
          <Stat
            label="n.e.s. destinations"
            value={`${(row.other_leaf_share * 100).toFixed(0)}%`}
            hint="Fraction of top-K that land on 'Other' / n.e.s. (not elsewhere specified) codes. This is a CLASSIFICATION-QUALITY signal, not a retrieval-quality one — AI-166 gave these codes real contextualised descriptions, so retrieval can match them properly. High % just means the trader ends up in a soft bucket regardless of which candidate wins."
          />
          <Stat
            label="Score flatness"
            value={row.score_flatness.toFixed(2)}
            hint="How flat the RRF scores are. 0 = top result strongly dominates (good — clear answer). 1 = top and bottom scores nearly equal (bad — retrieval can't separate winners)."
            />
          </div>
        <div className="border-t border-gray-800 mt-3 pt-3 grid grid-cols-1 lg:grid-cols-[1fr_14rem] gap-3 text-xs">
          <div>
            <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Review signals</div>
            <SignalBadges signals={reviewSignals} />
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">One-question split</div>
            <OneQuestionBadge signal={oneQuestionSignal} />
            <div className="text-[10px] text-gray-500 mt-1 leading-snug">{oneQuestionSignal.detail}</div>
          </div>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded p-4">
        <h4 className="text-sm font-semibold mb-2">Per-level entropy &amp; spread</h4>
        <ResponsiveContainer width="100%" height={180}>
          <BarChart data={entropyData} margin={{ top: 4, right: 4, left: 4, bottom: 4 }}>
            <CartesianGrid stroke="#1f2937" />
            <XAxis dataKey="level" tick={{ fill: "#9ca3af", fontSize: 10 }} />
            <YAxis tick={{ fill: "#9ca3af", fontSize: 10 }} />
            <Tooltip contentStyle={{ background: "#111827", border: "1px solid #374151" }} />
            <Bar dataKey="entropy" name="Entropy (bits)" fill="#3b82f6" />
            <Bar dataKey="count" name="Distinct cuts" fill="#f59e0b" />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {candidateTree && (
        <details className="bg-gray-900 border border-gray-800 rounded p-4">
          <summary className="text-sm font-semibold cursor-pointer text-gray-300 hover:text-white">
            Indented tree view (alt — tree below this panel is the main view)
          </summary>
          <div className="overflow-auto max-h-[36rem] mt-2">
            <CandidateTreeView root={candidateTree} />
          </div>
        </details>
      )}

      {detail?.top_candidates && detail.top_candidates.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded p-4">
          <h4 className="text-sm font-semibold mb-2">Top candidates ({detail.top_candidates.length})</h4>
          <div className="max-h-64 overflow-auto">
            <table className="w-full text-xs">
              <thead className="text-gray-400">
                <tr>
                  <th className="text-left p-1">Code</th>
                  <th className="text-right p-1">RRF</th>
                  <th className="text-right p-1">cos</th>
                  <th className="text-left p-1">Description</th>
                </tr>
              </thead>
              <tbody>
                {detail.top_candidates.slice(0, 30).map((c, i) => {
                  const desc = c.declarable_title || c.search_text || c.self_text || "";
                  return (
                    <tr key={i} className="border-t border-gray-800">
                      <td className="p-1 font-mono whitespace-nowrap">
                        {c.goods_nomenclature_item_id}
                        {c.contextualised_other && (
                          <span
                            className="ml-1 px-1 rounded bg-purple-500/20 text-purple-300 text-[9px] uppercase font-semibold"
                            title="AI-166 contextualised 'Other'"
                          >
                            ai-166
                          </span>
                        )}
                      </td>
                      <td className="p-1 text-right">{c.score.toFixed(4)}</td>
                      <td className="p-1 text-right">{c.cosine_score?.toFixed(3) ?? "-"}</td>
                      <td className="p-1 truncate max-w-md" title={desc}>{desc}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  hint,
  highlight,
}: {
  label: string;
  value: string;
  sub?: string;
  hint?: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={`p-2 rounded border ${highlight ? "border-blue-700 bg-blue-900/20" : "border-gray-800 bg-gray-800/40"}`}
      title={hint || ""}
    >
      <div className="text-[10px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className="text-sm font-mono">{value}</div>
      {sub && <div className="text-[10px] text-gray-500 font-mono">{sub}</div>}
    </div>
  );
}

function SignalBadges({ signals, max }: { signals: ReviewSignal[]; max?: number }) {
  const visible = max ? signals.slice(0, max) : signals;
  const hiddenSignals = max ? signals.slice(max) : [];
  return (
    <div className="flex flex-wrap gap-1">
      {visible.map((signal) => (
        <span
          key={signal.id}
          className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none whitespace-nowrap ${SIGNAL_TONE_CLASSES[signal.tone]}`}
          title={signal.detail}
        >
          {signal.label}
        </span>
      ))}
      {hiddenSignals.length > 0 && (
        <span
          className="inline-flex items-center rounded border border-gray-700 bg-gray-800 px-1.5 py-0.5 text-[10px] text-gray-300 leading-none"
          // title attribute strips newlines to spaces, so comma-separate names.
          // Full details remain available by clicking through to the detail panel.
          title={`Also: ${hiddenSignals.map((s) => s.label).join(", ")}`}
        >
          +{hiddenSignals.length}
        </span>
      )}
    </div>
  );
}

function OneQuestionBadge({ signal }: { signal: OneQuestionSignal }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none whitespace-nowrap ${SIGNAL_TONE_CLASSES[signal.tone]}`}
      title={signal.detail}
    >
      {signal.label}
    </span>
  );
}

// ---- Candidate decision tree --------------------------------------------
//
// Builds Section -> Chapter -> Heading -> Subheading -> 8-digit -> Declarable
// nesting, weighted by per-candidate RRF score. Renders as a nested list with
// inflexion-point highlighting (any node whose children count > 1 is the spot
// where the Q&A loop has to pick a branch).

type TreeNode = {
  level: "root" | "section" | "chapter" | "heading" | "subheading" | "eight_digit" | "declarable";
  key: string;
  label: string;
  description?: string | null;
  weight: number;          // sum of RRF scores under this subtree
  candidates: number;      // count of declarable leaves under this subtree
  children: TreeNode[];
  bits: number;            // log2(direct children) — questions to descend through here
  contextualisedOther?: boolean;  // AI-166: this declarable's description came from a contextualised "Other" self_text
};

function buildCandidateTree(cands: Candidate[]): TreeNode {
  const root: TreeNode = {
    level: "root", key: "ROOT", label: "all candidates",
    weight: 0, candidates: 0, children: [], bits: 0,
  };
  const cutAt = (c: Candidate, level: TreeNode["level"]): { key: string; label: string; desc?: string | null; contextualisedOther?: boolean } => {
    const code = c.goods_nomenclature_item_id;
    switch (level) {
      case "section":     return {
        key: c.section || "?",
        label: c.section ? `Section ${c.section}` : "Section ?",
        desc: c.section_title,
      };
      case "chapter":     return {
        key: code.slice(0, 2),
        label: `Ch ${code.slice(0, 2)}`,
        desc: c.chapter_title,
        contextualisedOther: c.chapter_contextualised === true,
      };
      case "heading":     return {
        key: code.slice(0, 4),
        label: code.slice(0, 4),
        desc: c.heading_title,
        contextualisedOther: c.heading_contextualised === true,
      };
      case "subheading":  return {
        key: code.slice(0, 6),
        label: code.slice(0, 6),
        desc: c.subheading_title,
        contextualisedOther: c.subheading_contextualised === true,
      };
      case "eight_digit": return {
        key: code.slice(0, 8),
        label: code.slice(0, 8),
        desc: c.eight_digit_title,
        contextualisedOther: c.eight_digit_contextualised === true,
      };
      case "declarable":  return {
        key: code,
        label: code,
        desc: c.declarable_title || c.search_text || c.self_text,
        contextualisedOther: c.contextualised_other === true,
      };
      default:            return { key: "ROOT", label: "all candidates" };
    }
  };
  const levels: TreeNode["level"][] = ["section", "chapter", "heading", "subheading", "eight_digit", "declarable"];
  for (const c of cands) {
    let node = root;
    root.candidates += 1;
    root.weight += Math.max(c.score, 0);
    for (const lvl of levels) {
      const cut = cutAt(c, lvl);
      let child = node.children.find((n) => n.key === cut.key);
      if (!child) {
        child = {
          level: lvl, key: cut.key, label: cut.label,
          description: cut.desc, weight: 0, candidates: 0, children: [], bits: 0,
          contextualisedOther: cut.contextualisedOther,
        };
        node.children.push(child);
      }
      child.weight += Math.max(c.score, 0);
      child.candidates += 1;
      node = child;
    }
  }
  // Compute bits per node (log2 of direct children count when >1).
  const walk = (n: TreeNode) => {
    n.bits = n.children.length > 1 ? Math.log2(n.children.length) : 0;
    // Sort children by weight desc so the dominant branch is on top.
    n.children.sort((a, b) => b.weight - a.weight);
    for (const c of n.children) walk(c);
  };
  walk(root);
  return root;
}

function CandidateTreeView({ root }: { root: TreeNode }) {
  const totalWeight = root.weight || 1;
  return (
    <ul className="text-xs font-mono space-y-0.5">
      {root.children.map((c) => (
        <TreeBranch key={`${c.level}-${c.key}`} node={c} totalWeight={totalWeight} depth={0} parentBranches={root.children.length} />
      ))}
    </ul>
  );
}

// ---- Top-down SVG tree (root at top, leaves at bottom) ------------------
//
// Boxes + connectors, organic-tree style. Section row up top, declarable
// leaves at the bottom. Inflexion nodes (>1 children) ringed orange.
// Box size scaled by RRF share so dominant branches are visually bigger.

type LaidOutNode = TreeNode & {
  _x: number;
  _y: number;
  _w: number;
  _h: number;
  _depth: number;
};

function flattenWithLayout(root: TreeNode): {
  nodes: LaidOutNode[];
  edges: { from: LaidOutNode; to: LaidOutNode }[];
  width: number;
  height: number;
} {
  // Walk to assign depth + collect leaves
  const leaves: TreeNode[] = [];
  const allNodes: TreeNode[] = [];
  const parentOf = new Map<TreeNode, TreeNode | null>();
  parentOf.set(root, null);

  const assignDepth = (n: TreeNode, depth: number) => {
    (n as LaidOutNode)._depth = depth;
    allNodes.push(n);
    if (n.children.length === 0) leaves.push(n);
    for (const c of n.children) {
      parentOf.set(c, n);
      assignDepth(c, depth + 1);
    }
  };
  assignDepth(root, 0);

  // Geometry
  const LEAF_SPACING = 80;
  const LEVEL_HEIGHT = 78;
  const BOX_W = 70;
  const BOX_H = 30;

  // Assign x to leaves by order of appearance
  leaves.forEach((leaf, i) => {
    (leaf as LaidOutNode)._x = i * LEAF_SPACING;
  });

  // Recursive: internal nodes get centered on children's x range
  const computeX = (n: TreeNode): number => {
    if (n.children.length === 0) return (n as LaidOutNode)._x;
    const childXs = n.children.map(computeX);
    const x = (Math.min(...childXs) + Math.max(...childXs)) / 2;
    (n as LaidOutNode)._x = x;
    return x;
  };
  computeX(root);

  // y = depth * LEVEL_HEIGHT; w/h fixed
  for (const n of allNodes) {
    const l = n as LaidOutNode;
    l._y = l._depth * LEVEL_HEIGHT;
    l._w = BOX_W;
    l._h = BOX_H;
  }

  // Edges
  const edges: { from: LaidOutNode; to: LaidOutNode }[] = [];
  for (const n of allNodes) {
    for (const c of n.children) {
      edges.push({ from: n as LaidOutNode, to: c as LaidOutNode });
    }
  }

  // Bounds (accounting for box dimensions)
  const xs = allNodes.map((n) => (n as LaidOutNode)._x);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const maxDepth = Math.max(...allNodes.map((n) => (n as LaidOutNode)._depth));
  const width = maxX - minX + BOX_W + 40;
  const height = maxDepth * LEVEL_HEIGHT + BOX_H + 40;

  // Shift everything to positive coords
  const xOffset = -minX + 20;
  for (const n of allNodes) {
    (n as LaidOutNode)._x += xOffset;
  }

  return { nodes: allNodes as LaidOutNode[], edges, width, height };
}

const LEVEL_COLOR_HEX: Record<string, string> = {
  section: "#a78bfa",
  chapter: "#60a5fa",
  heading: "#22d3ee",
  subheading: "#2dd4bf",
  eight_digit: "#34d399",
  declarable: "#fde68a",
  root: "#9ca3af",
};

function TopDownTree({ root, term }: { root: TreeNode; term: string }) {
  const { nodes, edges, width, height, parentOf } = flattenWithLayoutExt(root);
  const totalWeight = root.weight || 1;
  const [hover, setHover] = useState<LaidOutNode | null>(null);
  const [selected, setSelected] = useState<LaidOutNode | null>(null);
  // LLM-generated question cache, keyed by node identity. While a request is
  // in flight, value is { loading: true }.
  type Q = { question?: string; options?: string[]; error?: string; model?: string; elapsed_seconds?: number; loading?: boolean };
  const [llmQ, setLlmQ] = useState<Map<TreeNode, Q>>(new Map());

  async function generateQuestionFor(node: LaidOutNode, breadcrumb: LaidOutNode[]) {
    setLlmQ((m) => new Map(m).set(node, { loading: true }));
    try {
      // Gather all declarable candidates under this node
      const cands: any[] = [];
      const stack: LaidOutNode[] = [node];
      while (stack.length) {
        const n = stack.pop()!;
        if (n.level === "declarable") {
          cands.push({
            goods_nomenclature_item_id: n.key,
            description: n.description || n.label,
            score: n.weight,
          });
        } else {
          for (const c of n.children) stack.push(c as LaidOutNode);
        }
      }
      const breadcrumbPayload = breadcrumb
        .filter((n) => n.level !== "root")
        .map((n) => ({ level: n.level, label: n.label, description: n.description || "" }));

      const res = await fetch("/api/intercepts/generate-question", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ term, candidates: cands, breadcrumb: breadcrumbPayload }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setLlmQ((m) => new Map(m).set(node, data));
    } catch (err) {
      setLlmQ((m) => new Map(m).set(node, { error: String(err) }));
    }
  }

  // Compute highlighted set: ancestors + the selected node + descendants
  const highlighted = useMemo(() => {
    if (!selected) return null;
    const ids = new Set<LaidOutNode>();
    // Ancestors
    let cur: LaidOutNode | null = selected;
    while (cur) {
      ids.add(cur);
      cur = parentOf.get(cur) || null;
    }
    // Descendants
    const stack: LaidOutNode[] = [selected];
    while (stack.length) {
      const n = stack.pop()!;
      ids.add(n);
      for (const c of n.children) stack.push(c as LaidOutNode);
    }
    return ids;
  }, [selected, parentOf]);

  // Ancestor chain for the details readout
  const chain = useMemo(() => {
    if (!selected) return [];
    const out: LaidOutNode[] = [];
    let cur: LaidOutNode | null = selected;
    while (cur) {
      out.unshift(cur);
      cur = parentOf.get(cur) || null;
    }
    return out;
  }, [selected, parentOf]);

  const padX = 20;

  return (
    <div className="border border-gray-800 rounded bg-gray-950/30">
      <div className="overflow-auto max-h-[40rem]">
        <svg width={width + padX * 2} height={height} className="block">
          {/* Edges */}
          {edges.map((e, i) => {
            const x1 = e.from._x + e.from._w / 2;
            const y1 = e.from._y + e.from._h;
            const x2 = e.to._x + e.to._w / 2;
            const y2 = e.to._y;
            const midY = (y1 + y2) / 2;
            const onPath = highlighted && highlighted.has(e.from) && highlighted.has(e.to);
            return (
              <path
                key={i}
                d={`M${x1},${y1} V${midY} H${x2} V${y2}`}
                stroke={onPath ? "#f97316" : "#374151"}
                strokeWidth={onPath ? 2 : 1}
                strokeOpacity={highlighted && !onPath ? 0.25 : 1}
                fill="none"
              />
            );
          })}

          {/* Boxes */}
          {nodes.map((n) => {
            const isInflexion = n.children.length > 1;
            const sharePct = (n.weight / totalWeight) * 100;
            const label =
              n.level === "section"
                ? n.label.replace(/^Section /, "")
                : n.level === "root"
                ? "ALL"
                : n.label.replace(/^Ch /, "");
            const fill = LEVEL_COLOR_HEX[n.level] || "#9ca3af";
            const isHighlighted = !highlighted || highlighted.has(n);
            const isSelected = selected === n;
            return (
              <g
                key={`${n.level}-${n.key}`}
                transform={`translate(${n._x},${n._y})`}
                onMouseEnter={() => setHover(n)}
                onMouseLeave={() => setHover(null)}
                onClick={() => setSelected((cur) => (cur === n ? null : n))}
                className="cursor-pointer"
                opacity={isHighlighted ? 1 : 0.25}
              >
                <title>
                  {n.label}
                  {n.description ? ` — ${n.description}` : ""}
                  {"\n"}
                  {sharePct.toFixed(1)}% of RRF · {n.candidates} candidate{n.candidates !== 1 ? "s" : ""}
                  {isInflexion ? `\n⊢ pick 1 of ${n.children.length} options` : ""}
                </title>
                <rect
                  width={n._w}
                  height={n._h}
                  rx={4}
                  fill={fill}
                  fillOpacity={isSelected ? 1 : 0.85}
                  stroke={isSelected ? "#f59e0b" : isInflexion ? "#f97316" : "#1f2937"}
                  strokeWidth={isSelected ? 3 : isInflexion ? 2 : 1}
                />
                <text
                  x={n._w / 2}
                  y={n._h / 2 - 2}
                  textAnchor="middle"
                  dominantBaseline="middle"
                  fontSize={11}
                  fontFamily="ui-monospace, monospace"
                  fill="#111827"
                  fontWeight={isInflexion ? 700 : 500}
                >
                  {label.length > 10 ? label.slice(0, 10) : label}
                </text>
                {n.level !== "root" && (
                  <text
                    x={n._w / 2}
                    y={n._h - 4}
                    textAnchor="middle"
                    fontSize={9}
                    fontFamily="ui-monospace, monospace"
                    fill="#1f2937"
                    fillOpacity={0.7}
                  >
                    {sharePct.toFixed(0)}%
                  </text>
                )}
                {isInflexion && (
                  <text
                    x={n._w + 4}
                    y={11}
                    fontSize={9}
                    fontFamily="ui-monospace, monospace"
                    fill="#f97316"
                    fontWeight={700}
                  >
                    {n.children.length} opts
                  </text>
                )}
              </g>
            );
          })}

          {/* Level guides on the left edge */}
          {(["section", "chapter", "heading", "subheading", "eight_digit", "declarable"] as const).map(
            (lvl, i) => (
              <text
                key={lvl}
                x={4}
                y={(i + 1) * 78 + 18}
                fontSize={9}
                fill="#6b7280"
                fontFamily="ui-monospace, monospace"
              >
                {lvl}
              </text>
            ),
          )}
        </svg>
      </div>

      {/* Selection details readout */}
      {selected && (
        <div className="border-t border-gray-800 bg-gray-900 px-3 py-2 text-xs space-y-1">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-orange-300">Selected branch:</span>
            {chain.map((n, i) => (
              <span key={`${n.level}-${n.key}-${i}`} className="flex items-center gap-1">
                {i > 0 && <span className="text-gray-600">→</span>}
                <span
                  className="px-1.5 py-0.5 rounded text-[10px]"
                  style={{ background: (LEVEL_COLOR_HEX[n.level] || "#9ca3af") + "44", color: LEVEL_COLOR_HEX[n.level] }}
                  title={n.description || ""}
                >
                  {n.label}
                  {n.description && (
                    <span className="text-gray-400 ml-1">
                      ({n.description.slice(0, 30)}{n.description.length > 30 ? "…" : ""})
                    </span>
                  )}
                </span>
              </span>
            ))}
            <button
              className="ml-auto text-[10px] text-gray-400 hover:text-gray-200 underline"
              onClick={() => setSelected(null)}
            >
              clear
            </button>
          </div>
          <div className="text-gray-400">
            <strong className="text-gray-200">{selected.candidates}</strong> candidate
            {selected.candidates !== 1 ? "s" : ""} under this branch ·{" "}
            <strong className="text-gray-200">{((selected.weight / totalWeight) * 100).toFixed(1)}%</strong>{" "}
            of total RRF ·{" "}
            <strong className="text-gray-200">
              {countInflexionsOnPath(chain)}
            </strong>{" "}
            inflection point{countInflexionsOnPath(chain) !== 1 ? "s" : ""} from root to here
            {selected.children.length > 1 && (
              <span>
                {" "}·{" "}
                <strong className="text-orange-300">⊢ pick 1 of {selected.children.length}</strong> at this node
              </span>
            )}
          </div>
          {selected.description && (
            <div className="text-gray-400 leading-snug whitespace-normal break-words line-clamp-3 hover:line-clamp-none">
              {selected.contextualisedOther && (
                <span
                  className="mr-1.5 inline-block px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 text-[9px] uppercase font-semibold align-middle"
                  title="AI-166: this leaf's raw description is just 'Other' — text shown is the contextualised self_text the AI sees."
                >
                  AI-166 contextualised
                </span>
              )}
              {selected.description}
            </div>
          )}

          {/* LLM-generated differentiator question — only shown for inflection nodes */}
          {selected.children.length > 1 && (
            <div className="border-t border-gray-800 pt-2 mt-2">
              {(() => {
                const cached = llmQ.get(selected);
                if (!cached) {
                  return (
                    <button
                      className="px-2 py-1 text-[11px] bg-blue-600 hover:bg-blue-500 rounded font-medium"
                      onClick={() => generateQuestionFor(selected, chain)}
                      title="Calls GPT-5.5 (medium reasoning effort) with the exact production InteractiveSearchService prompt template + the candidate set under this branch."
                    >
                      ▶ Generate real LLM question (GPT-5.5, medium)
                    </button>
                  );
                }
                if (cached.loading) {
                  return (
                    <div className="flex items-center gap-2 text-gray-400">
                      <svg className="animate-spin h-3 w-3 text-blue-400" viewBox="0 0 24 24" fill="none">
                        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity="0.25" />
                        <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
                      </svg>
                      <span>Calling GPT-5.5 with the production prompt…</span>
                    </div>
                  );
                }
                if (cached.error) {
                  return (
                    <div className="text-red-400 text-[11px]">
                      <strong>Error:</strong> {cached.error}
                      <button
                        className="ml-2 underline"
                        onClick={() => generateQuestionFor(selected, chain)}
                      >
                        retry
                      </button>
                    </div>
                  );
                }
                if (cached.question) {
                  return (
                    <div>
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-[10px] uppercase text-gray-500">LLM ({cached.model})</span>
                        {cached.elapsed_seconds != null && (
                          <span className="text-[10px] text-gray-600">{cached.elapsed_seconds}s</span>
                        )}
                        <button
                          className="ml-auto text-[10px] text-gray-500 hover:text-gray-300 underline"
                          onClick={() => generateQuestionFor(selected, chain)}
                        >
                          regenerate
                        </button>
                      </div>
                      <div className="text-orange-300 font-semibold">{cached.question}</div>
                      {cached.options && cached.options.length > 0 && (
                        <ul className="mt-1 grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-0.5 text-[11px] text-gray-200">
                          {cached.options.map((opt, i) => (
                            <li key={i} className="flex items-baseline gap-1">
                              <span className="text-gray-500">{String.fromCharCode(65 + i)}.</span>
                              <span>{opt}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  );
                }
                return null;
              })()}
            </div>
          )}
        </div>
      )}

      {/* Hover preview — title + differentiator options with descriptions */}
      {!selected && hover && (
        <div className="border-t border-gray-800 bg-gray-900 px-3 py-2 text-xs space-y-2">
          <div className="flex items-center gap-2">
            <span
              className="px-1.5 py-0.5 rounded text-[10px] uppercase"
              style={{ background: (LEVEL_COLOR_HEX[hover.level] || "#9ca3af") + "44", color: LEVEL_COLOR_HEX[hover.level] }}
            >
              {hover.level}
            </span>
            <span className="font-semibold text-gray-100">{hover.label}</span>
            {hover.contextualisedOther && (
              <span
                className="px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 text-[9px] uppercase font-semibold"
                title="AI-166 contextualised 'Other' — description below is self_text, not the raw 'Other' label."
              >
                AI-166
              </span>
            )}
            {hover.description && (
              <span className="text-gray-300 truncate min-w-0 flex-1">— {hover.description}</span>
            )}
            <span className="text-gray-500 ml-auto whitespace-nowrap">
              {((hover.weight / totalWeight) * 100).toFixed(1)}% · {hover.candidates}{" "}
              cand · click to lock
            </span>
          </div>

          {/* Inflection nodes: show options the LLM would pick between, with titles */}
          {hover.children.length > 1 && (
            <div className="border-t border-gray-800 pt-2">
              <div className="text-orange-300 font-semibold mb-1">
                Differentiator at this node — pick 1 of {hover.children.length}{" "}
                <span className="text-gray-500 font-normal">
                  ({nextLevelHuman(hover.children[0].level)})
                </span>
              </div>
              <ul className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-4 gap-y-1 text-[11px]">
                {hover.children.slice(0, 9).map((c) => {
                  const pct = (c.weight / hover.weight) * 100;
                  return (
                    <li key={`${c.level}-${c.key}`} className="flex items-baseline gap-2 truncate">
                      <span
                        className="font-mono shrink-0"
                        style={{ color: LEVEL_COLOR_HEX[c.level] }}
                      >
                        {c.label}
                      </span>
                      <span className="text-gray-500 shrink-0">{pct.toFixed(0)}%</span>
                      <span className="text-gray-300 truncate" title={c.description || ""}>
                        {c.description || "(no description)"}
                      </span>
                    </li>
                  );
                })}
                {hover.children.length > 9 && (
                  <li className="text-gray-500 italic">+{hover.children.length - 9} more…</li>
                )}
              </ul>
              <div className="text-[10px] text-gray-500 mt-1.5 italic">
                Production LLM may phrase this as an attribute question (material / use / size /
                form) that resolves multiple levels at once.
              </div>
            </div>
          )}

          {/* Single-child non-leaf: no question, walks straight down */}
          {hover.children.length === 1 && (
            <div className="text-gray-500 italic">
              No decision here — only one branch ({hover.children[0].label}{" "}
              {hover.children[0].description ? `· ${hover.children[0].description.slice(0, 80)}` : ""}
              ). AI walks down without asking.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Human-readable label for "the level below this node" — used when framing
// the differentiator question in the hover panel ("Which chapter?",
// "Which heading?", etc).
function nextLevelHuman(level: string): string {
  switch (level) {
    case "section": return "section";
    case "chapter": return "chapter";
    case "heading": return "heading";
    case "subheading": return "subheading";
    case "eight_digit": return "8-digit code";
    case "declarable": return "declarable code";
    default: return "branch";
  }
}

function countInflexionsOnPath(chain: LaidOutNode[]): number {
  let count = 0;
  for (const n of chain) {
    if (n.children.length > 1) count += 1;
  }
  return count;
}

// Extended layout that also returns parentOf map for ancestor traversal.
function flattenWithLayoutExt(
  root: TreeNode,
): ReturnType<typeof flattenWithLayout> & { parentOf: Map<LaidOutNode, LaidOutNode | null> } {
  const base = flattenWithLayout(root);
  const parentOf = new Map<LaidOutNode, LaidOutNode | null>();
  const walk = (n: LaidOutNode, parent: LaidOutNode | null) => {
    parentOf.set(n, parent);
    for (const c of n.children) walk(c as LaidOutNode, n);
  };
  walk(root as LaidOutNode, null);
  return { ...base, parentOf };
}

const LEVEL_ABBR: Record<string, string> = {
  section: "SEC",
  chapter: "CH",
  heading: "HD",
  subheading: "SUB",
  eight_digit: "8D",
  declarable: "★",
};

function TreeBranch({
  node, totalWeight, depth, parentBranches,
}: {
  node: TreeNode;
  totalWeight: number;
  depth: number;
  parentBranches: number;
}) {
  const [open, setOpen] = useState(depth < 2);   // expand top two levels by default
  const sharePct = (node.weight / totalWeight) * 100;
  const isInflexion = node.children.length > 1;
  const isLeaf = node.level === "declarable";
  const barWidth = Math.max(2, Math.min(160, sharePct * 1.6));
  const levelColor: Record<string, string> = {
    section: "text-purple-300",
    chapter: "text-blue-300",
    heading: "text-cyan-300",
    subheading: "text-teal-300",
    eight_digit: "text-emerald-300",
    declarable: "text-yellow-200",
  };

  return (
    <li className="leading-tight">
      <div
        className={`flex items-center gap-2 py-0.5 px-1 rounded hover:bg-gray-800/40 ${
          isInflexion ? "ring-1 ring-orange-500/40 bg-orange-900/10" : ""
        }`}
      >
        {!isLeaf ? (
          <button
            className="w-4 text-gray-500 hover:text-gray-200 text-[10px]"
            onClick={() => setOpen((o) => !o)}
            title={open ? "Collapse" : "Expand"}
          >
            {open ? "▾" : "▸"}
          </button>
        ) : (
          <span className="w-4 text-center text-gray-600">·</span>
        )}
        <span className={`text-[9px] uppercase w-8 ${levelColor[node.level]}`}>
          {LEVEL_ABBR[node.level]}
        </span>
        <span className={`${levelColor[node.level]} font-semibold whitespace-nowrap`}>
          {node.label}
        </span>
        <div
          className="h-2 bg-blue-500/60 rounded-sm"
          style={{ width: `${barWidth}px` }}
          title={`${sharePct.toFixed(1)}% of total RRF`}
        />
        <span className="text-gray-400 tabular-nums">
          {sharePct.toFixed(0)}% · {node.candidates} cand
        </span>
        {isInflexion && (
          <span
            className="ml-1 px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-300 text-[10px] font-semibold"
            title={`${node.children.length} branches at this level. AI has to pick one — typically as a single multi-option question with these ${node.children.length} options.`}
          >
            ⊢ pick 1 of {node.children.length}
          </span>
        )}
        {isLeaf && node.contextualisedOther && (
          <span
            className="ml-2 px-1 py-0.5 rounded bg-purple-500/20 text-purple-300 text-[9px] uppercase font-semibold"
            title="AI-166 contextualised 'Other' — description is the AI-generated self_text, not the literal 'Other' label."
          >
            ai-166
          </span>
        )}
        {node.description && isLeaf && (
          <span className="text-gray-500 truncate min-w-0 flex-1 ml-2" title={node.description}>
            {node.description}
          </span>
        )}
      </div>
      {!isLeaf && open && node.children.length > 0 && (
        <ul className="border-l border-gray-700 ml-3 pl-2 space-y-0.5">
          {node.children.map((c) => (
            <TreeBranch
              key={`${c.level}-${c.key}`}
              node={c}
              totalWeight={totalWeight}
              depth={depth + 1}
              parentBranches={node.children.length}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

// ---- HelpLabel + treemap helper -----------------------------------------

function HelpLabel({ label, tip }: { label: string; tip: string }) {
  return (
    <div className="flex items-center gap-1 text-xs text-gray-400 mb-1 min-w-0">
      <span className="truncate min-w-0">{label}</span>
      {tip && (
        <span className="relative group cursor-help shrink-0" tabIndex={0}>
          <span className="inline-flex items-center justify-center w-3.5 h-3.5 rounded-full bg-gray-700 text-gray-300 text-[10px] font-bold leading-none">
            ?
          </span>
          <span
            className="invisible opacity-0 group-hover:visible group-hover:opacity-100 group-focus:visible group-focus:opacity-100 transition-opacity duration-100 absolute left-1/2 -translate-x-1/2 bottom-full mb-2 z-50 w-72 bg-gray-800 border border-gray-700 rounded p-2 text-xs text-gray-200 shadow-xl pointer-events-none"
            role="tooltip"
          >
            {tip}
          </span>
        </span>
      )}
    </div>
  );
}
