import { useEffect, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
} from "recharts";
import {
  getBenchmarkResults,
  listRuns,
  getRunResults,
  exportJsonUrl,
  exportCsvUrl,
  getPrompts,
  getConfig,
  updateConfig,
} from "../api";
import type { RunListItem } from "../api";
import type {
  BenchmarkResults,
  CompletionResult,
  FactEntry,
  ModelSummary,
  QARound,
  ScoringWeights,
  SectionInfo,
  SimulatorTraceEntry,
} from "../types";

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"];

const COLUMN_TOOLTIPS: Record<string, string> = {
  "Model": "Model name / identifier",
  "Composite": "Weighted verdict score (0-1). Sum of all 13 dimensions using the Scoring Weights above.",
  // Deterministic accuracy (6) - ranked-list aware
  "Top-1": "Exact top-1 code match rate vs reference (strict).",
  "Top-3": "Reference's top-1 in candidate's top-3. UX-realistic - traders scan top-3.",
  "MRR": "Mean Reciprocal Rank. 1/rank of reference's top-1 in candidate's ranked list. 0 if missed top-5.",
  "Heading": "First-4-digit match vs reference (same HS heading).",
  "Chapter": "First-2-digit match vs reference (same HS chapter).",
  "Top-5": "Avg Jaccard overlap of top-5 code sets vs reference.",
  // Deterministic quality (3)
  "Schema": "JSON schema compliance (0-1). Deterministic replacement for judge structured_output.",
  "R-eff": "Rounds efficiency. 1.0 if one round, 0.0 if five. Fewer rounds = more decisive.",
  "Q-eff": "Question efficiency = new_slots_set / total_questions. Higher = less redundant.",
  // LLM (2)
  "Fact Consist.": "Judge: fact_consistency (0-10). Does the final code respect every committed fact? 10 if no Q&A.",
  "Q Quality": "Judge: question_quality (0-10). Phrasing clarity and discriminativeness of clarifying questions.",
  "Judged": "Successful judge evaluations vs total. API errors are excluded (not silently zeroed).",
  // Operational (4 visible)
  "Speed": "Reference avg latency / model latency. >1x = faster than reference.",
  "Avg Rounds": "Average Q&A rounds to reach a classification.",
  "Cost/Class": "Average cost ($) per single classification (all rounds).",
  "Total Cost": "Total cost ($) across all prompts in this run.",
};

function HeaderTip({ label, align = "right" }: { label: string; align?: "left" | "right" }) {
  const tip = COLUMN_TOOLTIPS[label] ?? "";
  return (
    <th className={`py-2 pr-4 text-${align} group relative cursor-help`}>
      <span className="border-b border-dotted border-gray-600">{label}</span>
      {tip && (
        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-3 py-2 bg-gray-800 border border-gray-600 rounded shadow-lg text-xs text-gray-200 font-normal whitespace-normal w-56 text-left z-50 hidden group-hover:block pointer-events-none">
          {tip}
        </div>
      )}
    </th>
  );
}

const DEFAULT_WEIGHTS: ScoringWeights = {
  // Deterministic accuracy (50%) - ranked-list aware
  top1_match: 0.15,
  mean_reciprocal_rank: 0.12,
  top3_hit: 0.08,
  heading_match: 0.08,
  top5_overlap: 0.04,
  chapter_match: 0.03,
  // Deterministic quality (11%)
  schema_valid: 0.03,
  rounds_efficiency: 0.04,
  question_efficiency: 0.04,
  // Operational (17%)
  speed: 0.10,
  cost: 0.07,
  // LLM (22%)
  fact_consistency: 0.15,
  question_quality: 0.07,
  // Ground truth - used instead of the reference-agreement bucket when
  // every compared model has gold-evaluated prompts
  gold_top1: 0.35,
  gold_hierarchical: 0.15,
};

function computeVerdict(summaries: ModelSummary[], weights: ScoringWeights) {
  if (summaries.length === 0) return null;

  // When every compared model has gold-evaluated prompts, correctness vs
  // gold replaces agreement-with-the-reference: a model that beats the
  // reference on gold must not be penalised for disagreeing with it.
  const useGold = summaries.every((x) => (x.gold_evaluated_count ?? 0) > 0);

  // Normalise weights to sum=1 so the UI can accept raw positive numbers.
  const raw: Record<string, number> = { ...weights };
  const referenceBucket = ["top1_match", "top3_hit", "mean_reciprocal_rank", "heading_match", "chapter_match", "top5_overlap"];
  const goldBucket = ["gold_top1", "gold_hierarchical"];
  for (const k of useGold ? referenceBucket : goldBucket) raw[k] = 0;
  const sum = Object.values(raw).reduce((a, b) => a + Math.max(0, b), 0);
  const W = sum > 0
    ? Object.fromEntries(
        Object.entries(raw).map(([k, v]) => [k, Math.max(0, v) / sum]),
      ) as Record<keyof ScoringWeights, number>
    : (DEFAULT_WEIGHTS as unknown as Record<keyof ScoringWeights, number>);

  const maxCost = Math.max(...summaries.map((x) => x.avg_cost_per_classification), 0.001);

  const scored = summaries.map((s) => {
    const top1 = s.top1_accuracy;
    const top3 = s.top3_hit_rate ?? 0;
    const mrr = s.avg_mean_reciprocal_rank ?? 0;
    const heading = s.heading_match_rate ?? 0;
    const chapter = s.chapter_match_rate ?? 0;
    const top5 = s.avg_top5_overlap;
    const schema = s.avg_schema_valid ?? 0;
    const roundsEff = s.avg_rounds_efficiency ?? 0;
    const questionEff = s.avg_question_efficiency ?? 0;
    const speed = Math.min(s.avg_speed_factor, 3) / 3;
    const cost = 1 - s.avg_cost_per_classification / maxCost;
    const factConsistency = (s.avg_judge_fact_consistency ?? 0) / 10;
    const questionQuality = (s.avg_judge_question_quality ?? 0) / 10;

    const goldTop1 = s.gold_top1_rate;
    const goldHier = s.avg_gold_hierarchical_score;

    // [weight, value|null] - null means the dimension is unavailable for
    // this model (e.g. judge call failed); renormalise over the available
    // weights instead of silently scoring it 0.
    const terms: Array<[number, number | null]> = [
      [W.top1_match ?? 0, top1],
      [W.top3_hit ?? 0, top3],
      [W.mean_reciprocal_rank ?? 0, mrr],
      [W.heading_match ?? 0, heading],
      [W.chapter_match ?? 0, chapter],
      [W.top5_overlap ?? 0, top5],
      [W.gold_top1 ?? 0, goldTop1 ?? null],
      [W.gold_hierarchical ?? 0, goldHier ?? null],
      [W.schema_valid ?? 0, schema],
      [W.rounds_efficiency ?? 0, roundsEff],
      [W.question_efficiency ?? 0, questionEff],
      [W.speed ?? 0, speed],
      [W.cost ?? 0, cost],
      [W.fact_consistency ?? 0, s.avg_judge_fact_consistency != null ? s.avg_judge_fact_consistency / 10 : null],
      [W.question_quality ?? 0, s.avg_judge_question_quality != null ? s.avg_judge_question_quality / 10 : null],
    ];
    const availableWeight = terms.reduce((a, [w, v]) => a + (v != null ? w : 0), 0);
    const composite = availableWeight > 0
      ? terms.reduce((a, [w, v]) => a + (v != null ? w * v : 0), 0) / availableWeight
      : 0;

    return {
      summary: s,
      composite,
      top1, top3, mrr, heading, chapter, top5,
      schema, roundsEff, questionEff,
      speed, cost,
      factConsistency, questionQuality,
      // Legacy aliases for the existing verdict tradeoff UI
      judgeAccuracy: top1,
      judgeOverall: factConsistency,
      judgeFactConsistency: factConsistency,
      judgeQuality: questionQuality,
      judgeStructure: schema,
      rounds: roundsEff,
    };
  });

  scored.sort((a, b) => b.composite - a.composite);

  const best = scored[0];
  const runner = scored.length > 1 ? scored[1] : null;
  const bs = best.summary;

  // Build reasoning - lead with judge quality, then operational
  const reasons: string[] = [];

  reasons.push("Top-1 accuracy: " + (bs.top1_accuracy * 100).toFixed(0) + "% exact match vs reference");
  if ((bs.avg_mean_reciprocal_rank ?? 0) > 0) {
    reasons.push("MRR: " + (bs.avg_mean_reciprocal_rank ?? 0).toFixed(2) + " (rank-aware)");
  }
  if (bs.avg_judge_fact_consistency != null) {
    reasons.push("Fact consistency: " + bs.avg_judge_fact_consistency.toFixed(1) + "/10");
  }
  if (bs.avg_judge_question_quality != null && bs.avg_judge_question_quality >= 6) {
    reasons.push("Question quality: " + bs.avg_judge_question_quality.toFixed(1) + "/10");
  }
  if (bs.avg_speed_factor >= 1) {
    reasons.push(bs.avg_speed_factor.toFixed(1) + "x faster than reference");
  } else {
    reasons.push(bs.avg_speed_factor.toFixed(2) + "x reference speed");
  }
  reasons.push(bs.avg_rounds.toFixed(1) + " rounds avg, $" + bs.avg_cost_per_classification.toFixed(4) + "/classification");

  // Trade-off note if runner-up is close
  let tradeoff: string | null = null;
  if (runner && best.composite - runner.composite < 0.05) {
    const rName = runner.summary.model_name;
    if (runner.judgeAccuracy > best.judgeAccuracy) {
      tradeoff = rName + " scored higher on accuracy (top-1 " + (runner.summary.top1_accuracy * 100).toFixed(0) + "%) but is slower.";
    } else if (runner.speed > best.speed) {
      tradeoff = rName + " is faster (" + runner.summary.avg_speed_factor.toFixed(1) + "x) but lower judge scores.";
    } else {
      tradeoff = rName + " is a close alternative (" + (runner.composite * 100).toFixed(1) + "% vs " + (best.composite * 100).toFixed(1) + "%).";
    }
  }

  return { best, runner, scored, reasons, tradeoff };
}

function runLabel(r: RunListItem) {
  return `${new Date(r.timestamp).toLocaleString()} - ${r.prompt_count}p x ${r.model_count}m - OS:${r.opensearch_limit}`;
}

function SectionChip({ section, compact = false }: { section: SectionInfo; compact?: boolean }) {
  // Stable colour per section number from a 21-slot palette
  const palette = [
    "bg-red-900/50 text-red-200 border-red-700",
    "bg-orange-900/50 text-orange-200 border-orange-700",
    "bg-amber-900/50 text-amber-200 border-amber-700",
    "bg-yellow-900/50 text-yellow-200 border-yellow-700",
    "bg-lime-900/50 text-lime-200 border-lime-700",
    "bg-green-900/50 text-green-200 border-green-700",
    "bg-emerald-900/50 text-emerald-200 border-emerald-700",
    "bg-teal-900/50 text-teal-200 border-teal-700",
    "bg-cyan-900/50 text-cyan-200 border-cyan-700",
    "bg-sky-900/50 text-sky-200 border-sky-700",
    "bg-blue-900/50 text-blue-200 border-blue-700",
    "bg-indigo-900/50 text-indigo-200 border-indigo-700",
    "bg-violet-900/50 text-violet-200 border-violet-700",
    "bg-purple-900/50 text-purple-200 border-purple-700",
    "bg-fuchsia-900/50 text-fuchsia-200 border-fuchsia-700",
    "bg-pink-900/50 text-pink-200 border-pink-700",
    "bg-rose-900/50 text-rose-200 border-rose-700",
    "bg-slate-800/70 text-slate-200 border-slate-600",
    "bg-gray-800/70 text-gray-200 border-gray-600",
    "bg-zinc-800/70 text-zinc-200 border-zinc-600",
    "bg-stone-800/70 text-stone-200 border-stone-600",
  ];
  const cls = palette[(section.number - 1) % palette.length];
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] font-mono px-1.5 py-0.5 rounded border ${cls}`}
      title={`Section ${section.roman} · ${section.title}`}
    >
      <span className="font-bold">§{section.roman}</span>
      {!compact && <span className="truncate max-w-[180px]">{section.title}</span>}
    </span>
  );
}

function SlotTag({ slot }: { slot: string }) {
  // Hash the slot name to a stable colour so the same slot looks the same
  // across the per-round trace and the fact-store panel.
  const palette = [
    "bg-purple-900/60 text-purple-200 border-purple-700",
    "bg-sky-900/60 text-sky-200 border-sky-700",
    "bg-emerald-900/60 text-emerald-200 border-emerald-700",
    "bg-pink-900/60 text-pink-200 border-pink-700",
    "bg-indigo-900/60 text-indigo-200 border-indigo-700",
    "bg-lime-900/60 text-lime-200 border-lime-700",
    "bg-teal-900/60 text-teal-200 border-teal-700",
    "bg-fuchsia-900/60 text-fuchsia-200 border-fuchsia-700",
    "bg-cyan-900/60 text-cyan-200 border-cyan-700",
    "bg-rose-900/60 text-rose-200 border-rose-700",
  ];
  let h = 0;
  for (let i = 0; i < slot.length; i++) h = (h * 31 + slot.charCodeAt(i)) | 0;
  const cls = palette[Math.abs(h) % palette.length];
  return (
    <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${cls}`}>
      {slot}
    </span>
  );
}

function SimulatorTraceBlock({
  round,
  accent,
}: {
  round: QARound;
  accent: "amber" | "blue";
}) {
  const questions = round.questions_asked ?? [];
  const trace: SimulatorTraceEntry[] = round.simulator_trace ?? [];
  const answers = round.answers_given ?? [];
  if (questions.length === 0) return null;

  const borderHover = accent === "amber" ? "hover:border-amber-600" : "hover:border-blue-600";
  const chosenBg = accent === "amber" ? "bg-amber-950/40 border-amber-700" : "bg-blue-950/40 border-blue-700";

  const simCost = round.simulator_cost ?? 0;
  const simLatency = round.simulator_latency_ms ?? 0;

  return (
    <div className="mt-2 rounded bg-gray-950/60 border border-gray-800 p-2">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs font-medium text-gray-300">
          Trader simulator picks
          <span className="ml-2 text-gray-600">({questions.length} question{questions.length === 1 ? "" : "s"})</span>
        </div>
        <div className="text-[10px] text-gray-500 font-mono">
          ${simCost.toFixed(6)} · {simLatency.toFixed(0)}ms
        </div>
      </div>
      <div className="space-y-2">
        {questions.map((q, i) => {
          const t = trace[i];
          const ans = answers[i];
          const chosen = t?.chosen ?? ans?.answer ?? "?";
          const reason = t?.reasoning ?? "";
          const consistent = t?.consistent_with_prior === true;
          const slot = t?.slot ?? "";
          const hasSim = !!t;

          return (
            <div
              key={i}
              className={`rounded border border-gray-800 ${borderHover} p-2 bg-gray-900/60`}
            >
              <div className="flex items-start justify-between gap-2 mb-1">
                <div className="text-xs text-gray-200 font-medium">
                  Q{i + 1}: {q.question}
                </div>
                {slot && <SlotTag slot={slot} />}
              </div>
              {q.options && q.options.length > 0 && (
                <ul className="text-xs space-y-0.5 mb-2">
                  {q.options.map((opt, oi) => {
                    const isChosen = opt === chosen;
                    return (
                      <li
                        key={oi}
                        className={`flex items-start gap-2 pl-1 pr-2 py-0.5 rounded ${
                          isChosen ? `${chosenBg} border` : "border border-transparent"
                        }`}
                      >
                        <span className={`mt-0.5 font-mono text-[10px] w-4 text-right ${
                          isChosen ? "text-emerald-400" : "text-gray-600"
                        }`}>
                          {isChosen ? "➜" : "·"}
                        </span>
                        <span className={isChosen ? "text-gray-100" : "text-gray-500"}>
                          {opt}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              )}
              <div className="flex items-center gap-2 flex-wrap text-[10px]">
                {hasSim ? (
                  <>
                    {consistent ? (
                      <span className="px-1.5 py-0.5 rounded bg-indigo-900/60 text-indigo-200 border border-indigo-700">
                        reused slot (consistent with prior fact)
                      </span>
                    ) : (
                      <span className="px-1.5 py-0.5 rounded bg-emerald-900/60 text-emerald-200 border border-emerald-700">
                        new slot (first set)
                      </span>
                    )}
                    <span className="text-gray-500 font-mono">
                      ${t!.cost.toFixed(6)} · {t!.latency_ms.toFixed(0)}ms
                    </span>
                  </>
                ) : (
                  <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">
                    legacy options[0] (no simulator)
                  </span>
                )}
              </div>
              {reason && (
                <div className="mt-1 text-xs text-gray-300 italic border-l-2 border-gray-700 pl-2">
                  {reason}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

type SlotEvent =
  | { kind: "commit"; slot: string; answer: string; model: string; round: number; reasoning: string; question: string }
  | { kind: "reuse"; slot: string; reusedAnswer: string; model: string; round: number; question: string; currentChoice: string };

function collectSlotEvents(
  results: BenchmarkResults,
  promptIndex: number,
): SlotEvent[] {
  const events: SlotEvent[] = [];
  const firstSeen = new Set<string>();

  const allResults = [
    ...(results.panel_results ?? []),
    ...(results.model_results ?? []),
  ].filter((r) => r.prompt_index === promptIndex && !r.error);

  // Preserve a stable ordering by (round, model_id) so the timeline reads
  // top-down as time progressed.
  const rows: Array<{
    model_id: string;
    round: number;
    trace: SimulatorTraceEntry;
    question: string;
  }> = [];
  for (const r of allResults) {
    for (const rnd of r.rounds ?? []) {
      const trace = rnd.simulator_trace ?? [];
      const qs = rnd.questions_asked ?? [];
      trace.forEach((t, i) => {
        rows.push({
          model_id: r.model_id,
          round: rnd.round_number,
          trace: t,
          question: qs[i]?.question ?? t.question,
        });
      });
    }
  }
  rows.sort((a, b) =>
    a.round !== b.round ? a.round - b.round : a.model_id.localeCompare(b.model_id),
  );

  for (const row of rows) {
    const slot = row.trace.slot;
    if (!slot) continue;
    if (!firstSeen.has(slot)) {
      firstSeen.add(slot);
      events.push({
        kind: "commit",
        slot,
        answer: row.trace.chosen,
        model: row.model_id,
        round: row.round,
        reasoning: row.trace.reasoning,
        question: row.question,
      });
    } else {
      events.push({
        kind: "reuse",
        slot,
        reusedAnswer: row.trace.chosen,
        model: row.model_id,
        round: row.round,
        question: row.question,
        currentChoice: row.trace.chosen,
      });
    }
  }
  return events;
}

function FactStorePanel({
  results,
  rawQueryByPrompt,
}: {
  results: BenchmarkResults;
  rawQueryByPrompt: Record<number, string>;
}) {
  const factStore = results.fact_store ?? {};
  const promptIndices = results.prompt_indices ?? [];
  if (promptIndices.length === 0) return null;

  const [expanded, setExpanded] = useState<Record<number, boolean>>({});

  const totalFacts = Object.values(factStore).reduce((s, arr) => s + arr.length, 0);
  if (totalFacts === 0 && Object.keys(factStore).length === 0) {
    // Run pre-dates the fact store feature; hide the panel.
    return null;
  }

  return (
    <section>
      <div className="flex items-end justify-between mb-3">
        <div>
          <h2 className="text-lg font-semibold">Simulator Fact Store</h2>
          <p className="text-xs text-gray-500 mt-1">
            One schema per prompt. The first model to ask about a concept coins
            the slot and sets the trader's answer. Every later question that
            falls under the same slot (from any model, any round) reuses that
            answer - that is the apples-to-apples property.
          </p>
        </div>
        <div className="text-xs text-gray-400">
          {totalFacts} fact{totalFacts === 1 ? "" : "s"} across {promptIndices.length} prompt{promptIndices.length === 1 ? "" : "s"}
        </div>
      </div>
      <div className="space-y-3">
        {promptIndices.map((pi) => {
          const facts: FactEntry[] = factStore[String(pi)] ?? [];
          const events = collectSlotEvents(results, pi);
          const isOpen = expanded[pi] ?? false;
          const rawQuery = rawQueryByPrompt[pi] ?? "";

          return (
            <div key={pi} className="rounded-lg bg-gray-900 border border-gray-800">
              <button
                type="button"
                onClick={() =>
                  setExpanded((prev) => ({ ...prev, [pi]: !(prev[pi] ?? false) }))
                }
                className="w-full text-left p-3 flex items-center justify-between gap-4 hover:bg-gray-800 rounded-lg"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <span className="text-xs font-mono text-gray-500">#{pi}</span>
                  <span className="text-sm font-medium truncate">
                    {rawQuery || `Prompt ${pi}`}
                  </span>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <div className="flex items-center gap-1 flex-wrap justify-end max-w-md">
                    {facts.slice(0, 8).map((f) => (
                      <SlotTag key={f.slot} slot={f.slot} />
                    ))}
                    {facts.length > 8 && (
                      <span className="text-[10px] text-gray-500">+{facts.length - 8} more</span>
                    )}
                  </div>
                  <span className="text-xs text-gray-500 tabular-nums">
                    {facts.length} slot{facts.length === 1 ? "" : "s"} · {events.length} event{events.length === 1 ? "" : "s"}
                  </span>
                  <span className="text-gray-500">{isOpen ? "[-]" : "[+]"}</span>
                </div>
              </button>
              {isOpen && (
                <div className="border-t border-gray-800 p-3 space-y-4">
                  {/* Committed facts (the final schema) */}
                  <div>
                    <h3 className="text-xs font-medium text-gray-300 mb-2">
                      Committed schema ({facts.length} slot{facts.length === 1 ? "" : "s"})
                    </h3>
                    {facts.length === 0 ? (
                      <p className="text-xs text-gray-500">
                        No facts committed. The model(s) answered without asking clarifying questions.
                      </p>
                    ) : (
                      <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="border-b border-gray-800 text-left text-gray-500">
                              <th className="py-1.5 pr-3">Slot</th>
                              <th className="py-1.5 pr-3">Answer</th>
                              <th className="py-1.5 pr-3">First set by</th>
                              <th className="py-1.5 pr-3">Round</th>
                              <th className="py-1.5 pr-3">Source question</th>
                            </tr>
                          </thead>
                          <tbody>
                            {facts.map((f) => (
                              <tr key={f.slot} className="border-b border-gray-900/70">
                                <td className="py-1.5 pr-3"><SlotTag slot={f.slot} /></td>
                                <td className="py-1.5 pr-3 text-gray-100">{f.answer}</td>
                                <td className="py-1.5 pr-3 text-gray-400">{f.source_model}</td>
                                <td className="py-1.5 pr-3 text-gray-500 font-mono">r{f.source_round}</td>
                                <td className="py-1.5 pr-3 text-gray-400 italic">"{f.source_question}"</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>

                  {/* Timeline of slot commits and reuses */}
                  <div>
                    <h3 className="text-xs font-medium text-gray-300 mb-2">
                      Timeline: how the schema was built ({events.length} event{events.length === 1 ? "" : "s"})
                    </h3>
                    {events.length === 0 ? (
                      <p className="text-xs text-gray-500">No simulator events for this prompt.</p>
                    ) : (
                      <ol className="space-y-1.5">
                        {events.map((ev, i) => (
                          <li key={i} className="flex items-start gap-2 text-xs">
                            <span className="text-gray-600 font-mono w-6 shrink-0 text-right">{i + 1}.</span>
                            <span className={`px-1.5 py-0.5 rounded shrink-0 ${
                              ev.kind === "commit"
                                ? "bg-emerald-900/50 text-emerald-200 border border-emerald-700/60"
                                : "bg-indigo-900/50 text-indigo-200 border border-indigo-700/60"
                            }`}>
                              {ev.kind === "commit" ? "set" : "recalled"}
                            </span>
                            <SlotTag slot={ev.slot} />
                            <span className="text-gray-400 font-mono shrink-0">r{ev.round}</span>
                            <span className="text-gray-500 shrink-0">by {ev.model}</span>
                            <span className="text-gray-400 italic truncate">"{ev.question}"</span>
                            <span className="text-gray-600 shrink-0">→</span>
                            <span className="text-gray-100 font-medium truncate">
                              {ev.kind === "commit" ? ev.answer : ev.currentChoice}
                            </span>
                          </li>
                        ))}
                      </ol>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function RunMetaBadge({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="text-xs px-2 py-1 rounded bg-gray-800 text-gray-300">
      <span className="text-gray-500">{label}:</span> {value}
    </span>
  );
}

function computeBaselineSummary(results: BenchmarkResults) {
  const baseline_results = results.consensus_results?.length
    ? results.consensus_results
    : results.baseline_results;
  if (baseline_results.length === 0) return null;
  const n = baseline_results.length;
  const avgLatency = baseline_results.reduce((s, r) => s + r.total_latency_ms, 0) / n;
  const avgRounds = baseline_results.reduce((s, r) => s + r.total_rounds, 0) / n;
  const totalCost = baseline_results.reduce((s, r) => s + r.total_cost, 0);
  const isMultiPanel = (results.panel_model_ids?.length ?? 0) > 1;
  const label = isMultiPanel
    ? `Consensus (${results.panel_model_ids.length} models)`
    : `Reference (${baseline_results[0]?.model_id})`;
  return {
    name: label,
    model_id: baseline_results[0]?.model_id,
    avgLatency: Math.round(avgLatency),
    avgRounds: Math.round(avgRounds * 100) / 100,
    totalCost,
    avgCost: totalCost / n,
  };
}

export default function AnalysisPanel() {
  const [results, setResults] = useState<BenchmarkResults | null>(null);
  const [compareResults, setCompareResults] = useState<BenchmarkResults | null>(null);
  const [savedRuns, setSavedRuns] = useState<RunListItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>("current");
  const [compareRunId, setCompareRunId] = useState<string>("none");
  const [err, setErr] = useState("");
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const [promptFilter, setPromptFilter] = useState<number | "all">("all");
  const [rawQueryByPrompt, setRawQueryByPrompt] = useState<Record<number, string>>({});
  const [weights, setWeights] = useState<ScoringWeights>(DEFAULT_WEIGHTS);
  const [savingWeights, setSavingWeights] = useState(false);

  useEffect(() => {
    getConfig()
      .then((c) => {
        if (c.scoring_weights) setWeights(c.scoring_weights);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    // Try current in-memory run first; if none, fall back to the most recent
    // saved run so the panel is populated without needing a live benchmark.
    (async () => {
      try {
        const runs = await listRuns();
        setSavedRuns(runs);
        try {
          const live = await getBenchmarkResults();
          setResults(live);
          setSelectedRunId("current");
        } catch {
          if (runs.length > 0) {
            // listRuns sorts by filename not timestamp; pick newest by timestamp
            const mostRecent = [...runs].sort(
              (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime(),
            )[0];
            const saved = await getRunResults(mostRecent.id);
            setResults(saved);
            setSelectedRunId(mostRecent.id);
          } else {
            setErr("No benchmark results yet. Run a benchmark first.");
          }
        }
      } catch (e) {
        setErr("Failed to load runs: " + String(e));
      }
    })();
    getPrompts()
      .then((ps) => {
        const map: Record<number, string> = {};
        for (const p of ps) map[p.index] = p.raw_query;
        setRawQueryByPrompt(map);
      })
      .catch(() => {});
  }, []);

  const loadRun = async (runId: string) => {
    setSelectedRunId(runId);
    setErr("");
    try {
      if (runId === "current") {
        setResults(await getBenchmarkResults());
      } else {
        setResults(await getRunResults(runId));
      }
    } catch {
      setErr("Failed to load run.");
      setResults(null);
    }
  };

  const loadCompare = async (runId: string) => {
    setCompareRunId(runId);
    if (runId === "none") {
      setCompareResults(null);
      return;
    }
    try {
      if (runId === "current") {
        setCompareResults(await getBenchmarkResults());
      } else {
        setCompareResults(await getRunResults(runId));
      }
    } catch {
      setCompareResults(null);
    }
  };

  if (err) return <div className="text-gray-400">{err}</div>;
  if (!results) return <div className="text-gray-400">Loading results...</div>;

  const { summaries: allSummaries, evaluations, baseline_results, model_results } = results;
  const comparing = compareResults !== null;

  // Separate consensus summary from candidate summaries
  const consensusSummary = allSummaries.find((s) => s.model_id === "consensus");
  const summaries = allSummaries.filter((s) => s.model_id !== "consensus");

  // Build lookup for compare run summaries
  const compareSummaryMap = new Map<string, ModelSummary>();
  if (compareResults) {
    for (const s of compareResults.summaries) {
      compareSummaryMap.set(s.model_id, s);
    }
  }

  // Bar chart data: latency comparison
  const latencyData = summaries.map((s) => ({
    name: s.model_name,
    "Avg Total Latency (ms)": Math.round(s.avg_total_latency_ms),
    "Avg Rounds": s.avg_rounds,
  }));

  const baselineSummary = computeBaselineSummary(results);

  if (baselineSummary) {
    latencyData.unshift({
      name: baselineSummary.name,
      "Avg Total Latency (ms)": baselineSummary.avgLatency,
      "Avg Rounds": baselineSummary.avgRounds,
    });
  }

  // Radar data: each axis = one of the 13 dimensions (subset shown) normalised
  // to 0-1. Include consensus/reference alongside candidates so you can see
  // where each candidate sits vs the reference profile.
  const radarModels = consensusSummary
    ? [consensusSummary, ...summaries]
    : summaries;
  const maxCostForRadar = Math.max(
    ...radarModels.map((s) => s.avg_cost_per_classification),
    0.001,
  );
  const radarData = [
    { metric: "Top-1", ...Object.fromEntries(radarModels.map((s) => [s.model_name, s.top1_accuracy])) },
    { metric: "MRR", ...Object.fromEntries(radarModels.map((s) => [s.model_name, s.avg_mean_reciprocal_rank ?? 0])) },
    { metric: "Heading", ...Object.fromEntries(radarModels.map((s) => [s.model_name, s.heading_match_rate ?? 0])) },
    { metric: "Fact Consist.", ...Object.fromEntries(radarModels.map((s) => [s.model_name, (s.avg_judge_fact_consistency ?? 0) / 10])) },
    { metric: "Q Quality", ...Object.fromEntries(radarModels.map((s) => [s.model_name, (s.avg_judge_question_quality ?? 0) / 10])) },
    { metric: "Schema", ...Object.fromEntries(radarModels.map((s) => [s.model_name, s.avg_schema_valid ?? 0])) },
    { metric: "R-eff", ...Object.fromEntries(radarModels.map((s) => [s.model_name, s.avg_rounds_efficiency ?? 0])) },
    { metric: "Speed", ...Object.fromEntries(radarModels.map((s) => [s.model_name, Math.min(s.avg_speed_factor, 2) / 2])) },
    { metric: "Cost", ...Object.fromEntries(radarModels.map((s) => [s.model_name, 1 - s.avg_cost_per_classification / maxCostForRadar])) },
  ];

  // Cost data
  const costData = summaries.map((s) => ({
    name: s.model_name,
    "Avg Cost/Classification ($)": Number(s.avg_cost_per_classification.toFixed(4)),
    "Total Cost ($)": Number(s.total_cost.toFixed(4)),
  }));

  // Consensus is deliberately EXCLUDED from the ranked verdict: its
  // reference-agreement scores are perfect by construction, so ranking it
  // against candidates presents a tautology as a result. It remains visible
  // as a baseline row in the tables.
  const verdict = computeVerdict(summaries, weights);
  // Lookup composite scores by model_id for the summary table
  const compositeByModel = new Map<string, number>();
  if (verdict) {
    for (const s of verdict.scored) compositeByModel.set(s.summary.model_id, s.composite);
  }

  // Gold-truth columns only render when at least one prompt in the run had
  // a gold_code. Before that they're invisible - the feature becomes opt-in.
  const hasGoldEvals = [consensusSummary, ...summaries].some(
    (s) => s && (s.gold_evaluated_count ?? 0) > 0,
  );
  // Helper for rendering a gold rate cell that may be null (not evaluated).
  const goldCell = (v: number | null | undefined, className = "") =>
    v == null ? (
      <td className={"py-3 pr-4 text-right text-gray-600 " + className}>-</td>
    ) : (
      <td className={"py-3 pr-4 text-right " + className}>{v.toFixed(2)}</td>
    );

  // Persist weight edits with light debounce via a save helper
  const updateWeight = async (key: keyof ScoringWeights, value: number) => {
    const next = { ...weights, [key]: Math.max(0, value) };
    setWeights(next);
    setSavingWeights(true);
    try {
      await updateConfig({ scoring_weights: next });
    } finally {
      setSavingWeights(false);
    }
  };
  const resetWeights = async () => {
    setWeights(DEFAULT_WEIGHTS);
    setSavingWeights(true);
    try {
      await updateConfig({ scoring_weights: DEFAULT_WEIGHTS });
    } finally {
      setSavingWeights(false);
    }
  };
  const weightSum = Object.values(weights).reduce((a, b) => a + Math.max(0, b), 0);

  const toggleExpand = (key: string) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const findResult = (modelId: string, promptIndex: number, list: CompletionResult[]) =>
    list.find((r) => r.model_id === modelId && r.prompt_index === promptIndex);

  const deltaCell = (a: number, b: number | undefined, fmt: (v: number) => string, higherIsBetter = true) => {
    if (b === undefined) return null;
    const diff = a - b;
    if (Math.abs(diff) < 0.0005) return <span className="text-gray-600 text-xs">-</span>;
    const good = higherIsBetter ? diff > 0 : diff < 0;
    return (
      <span className={`text-xs ${good ? "text-green-400" : "text-red-400"}`}>
        {diff > 0 ? "+" : ""}{fmt(diff)}
      </span>
    );
  };

  return (
    <div className="space-y-8">
      {/* Run selector + Compare + Export */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-400">Run:</label>
          <select
            value={selectedRunId}
            onChange={(e) => loadRun(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm"
          >
            <option value="current">Current (in-memory)</option>
            {savedRuns.map((r) => (
              <option key={r.id} value={r.id}>{runLabel(r)}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-400">Compare with:</label>
          <select
            value={compareRunId}
            onChange={(e) => loadCompare(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm"
          >
            <option value="none">None</option>
            <option value="current">Current (in-memory)</option>
            {savedRuns
              .filter((r) => r.id !== selectedRunId)
              .map((r) => (
                <option key={r.id} value={r.id}>{runLabel(r)}</option>
              ))}
          </select>
        </div>
        <a
          href={exportCsvUrl()}
          className="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded text-sm"
          download
        >
          Export CSV
        </a>
        <a
          href={exportJsonUrl()}
          className="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded text-sm"
          download
        >
          Export JSON
        </a>
      </div>

      {/* Run metadata */}
      <div className="flex items-center gap-3 flex-wrap">
        <RunMetaBadge label="OS Limit" value={results.opensearch_limit ?? "?"} />
        {results.panel_model_ids && results.panel_model_ids.length > 1 ? (
          <RunMetaBadge label="Panel" value={results.panel_model_ids.join(", ")} />
        ) : (
          <RunMetaBadge label="Reference" value={results.baseline_model_id ?? baseline_results[0]?.model_id ?? "?"} />
        )}
        <RunMetaBadge label="Prompts" value={results.prompt_indices.length} />
        <RunMetaBadge label="Candidates" value={summaries.length} />
        <RunMetaBadge label="Time" value={new Date(results.timestamp).toLocaleString()} />
        {comparing && (
          <>
            <span className="text-gray-600">vs</span>
            <RunMetaBadge label="OS Limit" value={compareResults!.opensearch_limit ?? "?"} />
            <RunMetaBadge label="Prompts" value={compareResults!.prompt_indices.length} />
            <RunMetaBadge label="Candidates" value={compareResults!.summaries.length} />
          </>
        )}
      </div>

      {/* Regression summary banner - visible when Compare With is active */}
      {comparing && compareResults && (() => {
        // Score deltas per model on our strongest signal (top1_accuracy).
        // "Regression" threshold = drop of >0.05 (~5 percentage points).
        const THRESHOLD = 0.05;
        const regressions: Array<{ name: string; before: number; after: number; delta: number }> = [];
        const improvements: Array<{ name: string; before: number; after: number; delta: number }> = [];
        const missing: string[] = [];
        const added: string[] = [];
        const candByIdCur = new Map(summaries.map((s) => [s.model_id, s]));
        const candByIdCmp = new Map(compareResults.summaries.filter((s) => s.model_id !== "consensus").map((s) => [s.model_id, s]));
        for (const [mid, before] of candByIdCmp) {
          const after = candByIdCur.get(mid);
          if (!after) { missing.push(before.model_name); continue; }
          const delta = after.top1_accuracy - before.top1_accuracy;
          if (delta <= -THRESHOLD) regressions.push({ name: after.model_name, before: before.top1_accuracy, after: after.top1_accuracy, delta });
          else if (delta >= THRESHOLD) improvements.push({ name: after.model_name, before: before.top1_accuracy, after: after.top1_accuracy, delta });
        }
        for (const [mid, after] of candByIdCur) {
          if (!candByIdCmp.has(mid)) added.push(after.model_name);
        }
        const hasFindings = regressions.length || improvements.length || missing.length || added.length;
        if (!hasFindings) {
          return (
            <section className="rounded-lg border border-gray-800 bg-gray-950/50 p-3 text-xs text-gray-400">
              No significant regressions or improvements vs compared run (top-1 within ±{(THRESHOLD * 100).toFixed(0)}pp for all models).
            </section>
          );
        }
        return (
          <section className="rounded-lg border border-amber-800/50 bg-amber-950/20 p-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-amber-300">Regression report vs compared run</h3>
              <div className="text-[10px] text-gray-500">
                threshold: ±{(THRESHOLD * 100).toFixed(0)}pp top-1 accuracy
              </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
              {regressions.length > 0 && (
                <div>
                  <div className="text-red-300 font-medium mb-1">🔻 {regressions.length} regression{regressions.length > 1 ? "s" : ""}</div>
                  <ul className="space-y-0.5">
                    {regressions.map((r) => (
                      <li key={r.name} className="flex justify-between">
                        <span>{r.name}</span>
                        <span className="font-mono">
                          <span className="text-gray-500">{(r.before * 100).toFixed(0)}%</span>
                          <span className="text-gray-600"> → </span>
                          <span className="text-red-300">{(r.after * 100).toFixed(0)}%</span>
                          <span className="text-red-400 ml-1">({(r.delta * 100).toFixed(0)}pp)</span>
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {improvements.length > 0 && (
                <div>
                  <div className="text-emerald-300 font-medium mb-1">🔺 {improvements.length} improvement{improvements.length > 1 ? "s" : ""}</div>
                  <ul className="space-y-0.5">
                    {improvements.map((r) => (
                      <li key={r.name} className="flex justify-between">
                        <span>{r.name}</span>
                        <span className="font-mono">
                          <span className="text-gray-500">{(r.before * 100).toFixed(0)}%</span>
                          <span className="text-gray-600"> → </span>
                          <span className="text-emerald-300">{(r.after * 100).toFixed(0)}%</span>
                          <span className="text-emerald-400 ml-1">(+{(r.delta * 100).toFixed(0)}pp)</span>
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {(missing.length > 0 || added.length > 0) && (
                <div className="md:col-span-2 text-gray-500 text-[11px] pt-1 border-t border-gray-800">
                  {missing.length > 0 && <span className="mr-3">Removed: {missing.join(", ")}</span>}
                  {added.length > 0 && <span>Added: {added.join(", ")}</span>}
                </div>
              )}
            </div>
          </section>
        );
      })()}

      {/* Verdict */}
      {verdict && (
        <section className="bg-gradient-to-r from-emerald-950/40 to-gray-900 border border-emerald-800/50 rounded-lg p-5">
          <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
            <span className="text-emerald-400">Verdict</span>
            <span className="text-xs px-2 py-0.5 rounded bg-purple-900 text-purple-300 font-normal">Judge-led</span>
            <span className="text-xs font-normal text-gray-600">judge quality 60% + speed/cost/rounds 40%</span>
          </h2>
          <div className="flex items-start gap-6">
            {/* Recommendation */}
            <div className="flex-1">
              <div className="flex items-center gap-3 mb-2">
                <span className="text-2xl font-bold text-emerald-300">{verdict.best.summary.model_name}</span>
                <span className="text-xs px-2 py-0.5 rounded bg-emerald-900 text-emerald-300">Recommended</span>
                <span className="text-sm text-gray-400">
                  Composite: {(verdict.best.composite * 100).toFixed(1)}%
                </span>
              </div>
              {verdict.reasons.length > 0 && (
                <ul className="text-sm text-gray-300 space-y-0.5 mb-2">
                  {verdict.reasons.map((r, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <span className="text-emerald-500 text-xs">+</span> {r}
                    </li>
                  ))}
                </ul>
              )}
              {verdict.tradeoff && (
                <p className="text-xs text-amber-400 mt-2">
                  Trade-off: {verdict.tradeoff}
                </p>
              )}
            </div>
            {/* Ranking */}
            <div className="w-64 shrink-0">
              <h4 className="text-xs text-gray-500 mb-2 uppercase tracking-wide">Ranking</h4>
              <div className="space-y-1">
                {verdict.scored.map((s, i) => (
                  <div key={s.summary.model_id} className="flex items-center gap-2 text-sm">
                    <span className={`w-5 text-right font-mono ${i === 0 ? "text-emerald-400" : "text-gray-500"}`}>
                      {i + 1}.
                    </span>
                    <div className="flex-1 flex items-center gap-2">
                      <div
                        className="h-1.5 rounded-full bg-emerald-600"
                        style={{ width: `${s.composite * 100}%`, maxWidth: "100%" }}
                      />
                      <span className={i === 0 ? "text-emerald-300" : "text-gray-400"}>
                        {s.summary.model_name}
                      </span>
                    </div>
                    <span className="text-xs text-gray-500 font-mono w-12 text-right">
                      {(s.composite * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>
      )}

      {/* Scoring Weights editor - composite verdict is Pareto over these */}
      <section className="bg-gray-900 rounded-lg p-4 border border-gray-800">
        <div className="flex items-end justify-between mb-3 flex-wrap gap-2">
          <div>
            <h3 className="text-sm font-medium">Scoring Weights</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Verdict is a weighted composite over these dimensions. Raw positive numbers are auto-normalised to sum=1. Change weights live to see which model wins under your priorities. Set any weight to 0 to exclude it from the composite (still shown in the summary table).
            </p>
          </div>
          <div className="flex items-center gap-3">
            {savingWeights && <span className="text-xs text-gray-500">saving...</span>}
            <button
              onClick={resetWeights}
              className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700"
            >
              Reset to defaults
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          {([
            ["top1_match", "Det · Top-1 match", "Exact 10-digit match at rank 1 vs reference"],
            ["top3_hit", "Det · Top-3 hit", "Reference's top-1 appears anywhere in candidate's top-3"],
            ["mean_reciprocal_rank", "Det · MRR", "1/rank of reference's top-1 in candidate's ranked list (0 if missed top-5)"],
            ["heading_match", "Det · Heading match", "First 4 digits agree (same HS heading)"],
            ["chapter_match", "Det · Chapter match", "First 2 digits agree (same HS chapter)"],
            ["top5_overlap", "Det · Top-5 overlap", "Jaccard overlap of top-5 codes vs reference"],
            ["schema_valid", "Det · Schema valid", "JSON schema compliance (deterministic)"],
            ["rounds_efficiency", "Det · Rounds efficiency", "1.0 if one round, 0.0 if five (fewer = better)"],
            ["question_efficiency", "Det · Question efficiency", "New slots set / total questions asked (higher = less redundant)"],
            ["speed", "Op · Speed", "Reference-relative speed factor"],
            ["cost", "Op · Cost", "Cheaper per classification = higher"],
            ["fact_consistency", "LLM · Fact consistency", "Does final code respect every committed fact? (judge)"],
            ["question_quality", "LLM · Question quality", "Phrasing/option quality of clarifying questions (judge)"],
          ] as Array<[keyof ScoringWeights, string, string]>).map(([key, label, tip]) => {
            const v = weights[key];
            const pct = weightSum > 0 ? (Math.max(0, v) / weightSum) * 100 : 0;
            return (
              <div key={key} className="bg-gray-950/40 border border-gray-800 rounded p-2">
                <div className="flex items-center justify-between">
                  <span className="text-gray-300 font-medium" title={tip}>{label}</span>
                  <span className="font-mono text-emerald-300">{pct.toFixed(0)}%</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={v}
                  onChange={(e) => updateWeight(key, parseFloat(e.target.value))}
                  className="w-full mt-1 accent-emerald-500"
                />
                <div className="flex items-center justify-between text-[10px] text-gray-500">
                  <span>raw</span>
                  <input
                    type="number"
                    min={0}
                    max={1}
                    step={0.01}
                    value={v}
                    onChange={(e) => updateWeight(key, parseFloat(e.target.value) || 0)}
                    className="w-14 bg-gray-800 border border-gray-700 rounded px-1 py-0.5 text-right font-mono text-gray-300"
                  />
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* Summary table */}
      <section>
        <h2 className="text-lg font-semibold mb-4">
          Model Summary
          {comparing && <span className="text-sm font-normal text-gray-400 ml-2">(with comparison deltas)</span>}
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-700 text-left text-gray-400">
                <HeaderTip label="Model" align="left" />
                <HeaderTip label="Composite" />
                {/* Deterministic accuracy */}
                <HeaderTip label="Top-1" />
                <HeaderTip label="Top-3" />
                <HeaderTip label="MRR" />
                <HeaderTip label="Heading" />
                <HeaderTip label="Chapter" />
                <HeaderTip label="Top-5" />
                {/* Gold truth (vs known-correct commodity code, if any prompt has one).
                    Conditionally rendered: hidden entirely when no prompt had a gold code. */}
                {hasGoldEvals && (
                  <>
                    <HeaderTip label="Gold T1" />
                    <HeaderTip label="Gold H" />
                    <HeaderTip label="Gold C" />
                    <HeaderTip label="Gold N" />
                  </>
                )}
                {/* Deterministic quality */}
                <HeaderTip label="Schema" />
                <HeaderTip label="R-eff" />
                <HeaderTip label="Q-eff" />
                {/* LLM */}
                <HeaderTip label="Fact Consist." />
                <HeaderTip label="Q Quality" />
                <HeaderTip label="Judged" />
                {/* Operational */}
                <HeaderTip label="Speed" />
                <HeaderTip label="Avg Rounds" />
                <HeaderTip label="Cost/Class" />
                <HeaderTip label="Total Cost" />
              </tr>
            </thead>
            <tbody>
              {baselineSummary && consensusSummary && (
                <tr className="border-b border-amber-900/50 bg-amber-950/20">
                  <td className="py-3 pr-4 font-medium">
                    {baselineSummary.name}
                    <span className="ml-2 text-xs px-2 py-0.5 rounded bg-amber-900 text-amber-300">Reference</span>
                  </td>
                  {/* Composite: reference has max on every deterministic dim vs itself */}
                  <td className="py-3 pr-4 text-right text-amber-300 font-medium">
                    {compositeByModel.has("consensus") ? compositeByModel.get("consensus")!.toFixed(3) : "1.000"}
                  </td>
                  {/* Deterministic accuracy - reference matches itself */}
                  <td className="py-3 pr-4 text-right text-amber-300">{consensusSummary.top1_accuracy.toFixed(2)}</td>
                  <td className="py-3 pr-4 text-right text-amber-300">{(consensusSummary.top3_hit_rate ?? 0).toFixed(2)}</td>
                  <td className="py-3 pr-4 text-right text-amber-300">{(consensusSummary.avg_mean_reciprocal_rank ?? 0).toFixed(2)}</td>
                  <td className="py-3 pr-4 text-right text-amber-300">{(consensusSummary.heading_match_rate ?? 0).toFixed(2)}</td>
                  <td className="py-3 pr-4 text-right text-amber-300">{(consensusSummary.chapter_match_rate ?? 0).toFixed(2)}</td>
                  <td className="py-3 pr-4 text-right text-amber-300">{consensusSummary.avg_top5_overlap.toFixed(2)}</td>
                  {/* Gold truth - reference scored vs gold (NOT by construction) */}
                  {hasGoldEvals && (
                    <>
                      {goldCell(consensusSummary.gold_top1_rate, "text-amber-300")}
                      {goldCell(consensusSummary.gold_heading_rate, "text-amber-300")}
                      {goldCell(consensusSummary.gold_chapter_rate, "text-amber-300")}
                      <td className="py-3 pr-4 text-right text-amber-300 text-xs">
                        {consensusSummary.gold_evaluated_count ?? 0}
                      </td>
                    </>
                  )}
                  {/* Deterministic quality */}
                  <td className="py-3 pr-4 text-right text-amber-300">{(consensusSummary.avg_schema_valid ?? 0).toFixed(2)}</td>
                  <td className="py-3 pr-4 text-right text-amber-300">{(consensusSummary.avg_rounds_efficiency ?? 0).toFixed(2)}</td>
                  <td className="py-3 pr-4 text-right text-amber-300">{(consensusSummary.avg_question_efficiency ?? 0).toFixed(2)}</td>
                  {/* LLM - reference is not judged */}
                  <td
                    className="py-3 pr-4 text-center text-xs text-amber-300 italic"
                    title="The reference is the answer key by construction; it is not judged against itself."
                    colSpan={3}
                  >
                    not judged
                  </td>
                  {/* Operational */}
                  <td className="py-3 pr-4 text-right text-gray-500">1.00x</td>
                  <td className="py-3 pr-4 text-right">{baselineSummary.avgRounds.toFixed(1)}</td>
                  <td className="py-3 pr-4 text-right">${baselineSummary.avgCost.toFixed(4)}</td>
                  <td className="py-3 text-right">${baselineSummary.totalCost.toFixed(4)}</td>
                </tr>
              )}
              {summaries.map((s) => {
                const cmp = compareSummaryMap.get(s.model_id);
                const composite = compositeByModel.get(s.model_id);
                return (
                  <tr
                    key={s.model_id}
                    className="border-b border-gray-800 hover:bg-gray-900"
                  >
                    <td className="py-3 pr-4 font-medium">{s.model_name}</td>
                    {/* Composite */}
                    <td className="py-3 pr-4 text-right font-medium">
                      {composite != null ? (
                        <span className={
                          composite > 0.7 ? "text-green-400"
                          : composite > 0.4 ? "text-amber-400"
                          : "text-red-400"
                        }>
                          {composite.toFixed(3)}
                        </span>
                      ) : <span className="text-gray-600">-</span>}
                    </td>
                    {/* Deterministic accuracy */}
                    <td className="py-3 pr-4 text-right">
                      {s.top1_accuracy.toFixed(2)}
                      {comparing && <div>{deltaCell(s.top1_accuracy, cmp?.top1_accuracy, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {(s.top3_hit_rate ?? 0).toFixed(2)}
                      {comparing && <div>{deltaCell(s.top3_hit_rate ?? 0, cmp?.top3_hit_rate, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {(s.avg_mean_reciprocal_rank ?? 0).toFixed(2)}
                      {comparing && <div>{deltaCell(s.avg_mean_reciprocal_rank ?? 0, cmp?.avg_mean_reciprocal_rank, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {(s.heading_match_rate ?? 0).toFixed(2)}
                      {comparing && <div>{deltaCell(s.heading_match_rate ?? 0, cmp?.heading_match_rate, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {(s.chapter_match_rate ?? 0).toFixed(2)}
                      {comparing && <div>{deltaCell(s.chapter_match_rate ?? 0, cmp?.chapter_match_rate, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {s.avg_top5_overlap.toFixed(2)}
                      {comparing && <div>{deltaCell(s.avg_top5_overlap, cmp?.avg_top5_overlap, v => v.toFixed(2))}</div>}
                    </td>
                    {/* Gold truth */}
                    {hasGoldEvals && (
                      <>
                        {goldCell(s.gold_top1_rate)}
                        {goldCell(s.gold_heading_rate)}
                        {goldCell(s.gold_chapter_rate)}
                        <td className="py-3 pr-4 text-right text-xs text-gray-500">
                          {s.gold_evaluated_count ?? 0}
                        </td>
                      </>
                    )}
                    {/* Deterministic quality */}
                    <td className="py-3 pr-4 text-right">
                      {(s.avg_schema_valid ?? 0).toFixed(2)}
                      {comparing && <div>{deltaCell(s.avg_schema_valid ?? 0, cmp?.avg_schema_valid, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {(s.avg_rounds_efficiency ?? 0).toFixed(2)}
                      {comparing && <div>{deltaCell(s.avg_rounds_efficiency ?? 0, cmp?.avg_rounds_efficiency, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {(s.avg_question_efficiency ?? 0).toFixed(2)}
                      {comparing && <div>{deltaCell(s.avg_question_efficiency ?? 0, cmp?.avg_question_efficiency, v => v.toFixed(2))}</div>}
                    </td>
                    {/* LLM */}
                    <td className="py-3 pr-4 text-right">
                      {s.avg_judge_fact_consistency != null ? s.avg_judge_fact_consistency.toFixed(2) : <span className="text-gray-600">-</span>}
                      {comparing && s.avg_judge_fact_consistency != null && <div>{deltaCell(s.avg_judge_fact_consistency, cmp?.avg_judge_fact_consistency ?? undefined, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {s.avg_judge_question_quality != null ? s.avg_judge_question_quality.toFixed(2) : <span className="text-gray-600">-</span>}
                      {comparing && s.avg_judge_question_quality != null && <div>{deltaCell(s.avg_judge_question_quality, cmp?.avg_judge_question_quality ?? undefined, v => v.toFixed(2))}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right text-xs">
                      {s.judge_scored_count != null ? (
                        <span
                          className={(s.judge_error_count ?? 0) > 0 ? "text-amber-300" : "text-gray-500"}
                          title={(s.judge_error_count ?? 0) > 0
                            ? `${s.judge_error_count} API errors excluded from averages`
                            : ""}
                        >
                          {s.judge_scored_count}/{(s.judge_scored_count ?? 0) + (s.judge_error_count ?? 0)}
                          {(s.judge_error_count ?? 0) > 0 ? ` (${s.judge_error_count} err)` : ""}
                        </span>
                      ) : (
                        <span className="text-gray-600">-</span>
                      )}
                    </td>
                    {/* Operational */}
                    <td className="py-3 pr-4 text-right">
                      <span className={s.avg_speed_factor > 1 ? "text-green-400" : "text-red-400"}>
                        {s.avg_speed_factor.toFixed(2)}x
                      </span>
                      {comparing && <div>{deltaCell(s.avg_speed_factor, cmp?.avg_speed_factor, v => v.toFixed(2) + "x")}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {s.avg_rounds.toFixed(1)}
                      {comparing && <div>{deltaCell(s.avg_rounds, cmp?.avg_rounds, v => v.toFixed(1), false)}</div>}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      ${s.avg_cost_per_classification.toFixed(4)}
                      {comparing && <div>{deltaCell(s.avg_cost_per_classification, cmp?.avg_cost_per_classification, v => "$" + v.toFixed(4), false)}</div>}
                    </td>
                    <td className="py-3 text-right">
                      ${s.total_cost.toFixed(4)}
                      {comparing && <div>{deltaCell(s.total_cost, cmp?.total_cost, v => "$" + v.toFixed(4), false)}</div>}
                    </td>
                  </tr>
                );
              })}
              {/* Models only in compare run */}
              {comparing && compareResults!.summaries
                .filter((cs) => !summaries.find((s) => s.model_id === cs.model_id))
                .map((cs) => (
                  <tr key={cs.model_id} className="border-b border-gray-800 opacity-50">
                    <td className="py-3 pr-4 font-medium">
                      {cs.model_name}
                      <span className="ml-2 text-xs px-2 py-0.5 rounded bg-purple-900 text-purple-300">compare only</span>
                    </td>
                    {/* Composite - not computed for compare-only models */}
                    <td className="py-3 pr-4 text-right text-gray-600">-</td>
                    {/* Deterministic accuracy */}
                    <td className="py-3 pr-4 text-right">{cs.top1_accuracy.toFixed(2)}</td>
                    <td className="py-3 pr-4 text-right">{(cs.top3_hit_rate ?? 0).toFixed(2)}</td>
                    <td className="py-3 pr-4 text-right">{(cs.avg_mean_reciprocal_rank ?? 0).toFixed(2)}</td>
                    <td className="py-3 pr-4 text-right">{(cs.heading_match_rate ?? 0).toFixed(2)}</td>
                    <td className="py-3 pr-4 text-right">{(cs.chapter_match_rate ?? 0).toFixed(2)}</td>
                    <td className="py-3 pr-4 text-right">{cs.avg_top5_overlap.toFixed(2)}</td>
                    {/* Gold truth */}
                    {hasGoldEvals && (
                      <>
                        {goldCell(cs.gold_top1_rate)}
                        {goldCell(cs.gold_heading_rate)}
                        {goldCell(cs.gold_chapter_rate)}
                        <td className="py-3 pr-4 text-right text-xs text-gray-500">
                          {cs.gold_evaluated_count ?? 0}
                        </td>
                      </>
                    )}
                    {/* Deterministic quality */}
                    <td className="py-3 pr-4 text-right">{(cs.avg_schema_valid ?? 0).toFixed(2)}</td>
                    <td className="py-3 pr-4 text-right">{(cs.avg_rounds_efficiency ?? 0).toFixed(2)}</td>
                    <td className="py-3 pr-4 text-right">{(cs.avg_question_efficiency ?? 0).toFixed(2)}</td>
                    {/* LLM */}
                    <td className="py-3 pr-4 text-right">{cs.avg_judge_fact_consistency != null ? cs.avg_judge_fact_consistency.toFixed(2) : "-"}</td>
                    <td className="py-3 pr-4 text-right">{cs.avg_judge_question_quality != null ? cs.avg_judge_question_quality.toFixed(2) : "-"}</td>
                    <td className="py-3 pr-4 text-right text-xs text-gray-500">
                      {cs.judge_scored_count != null
                        ? `${cs.judge_scored_count}/${(cs.judge_scored_count ?? 0) + (cs.judge_error_count ?? 0)}`
                        : "-"}
                    </td>
                    {/* Operational */}
                    <td className="py-3 pr-4 text-right">{cs.avg_speed_factor.toFixed(2)}x</td>
                    <td className="py-3 pr-4 text-right">{cs.avg_rounds.toFixed(1)}</td>
                    <td className="py-3 pr-4 text-right">${cs.avg_cost_per_classification.toFixed(4)}</td>
                    <td className="py-3 text-right">${cs.total_cost.toFixed(4)}</td>
                  </tr>
                ))
              }
            </tbody>
          </table>
        </div>
      </section>

      {/* Per-model breakdown of the 5 primary scoring dimensions on a 0-10
          scale: 4 deterministic (Top-1, MRR, Heading, Fact Consist. scaled)
          + 2 LLM (Fact Consist., Q Quality). Composite verdict is separate. */}
      {(summaries.some((s) => s.top1_accuracy > 0 || s.avg_judge_fact_consistency != null)) && (() => {
        const scoreToTen = (v: number) => v * 10;
        const judgeScoreData = [
          ...(consensusSummary ? [{
            name: consensusSummary.model_name,
            "Top-1": scoreToTen(consensusSummary.top1_accuracy),
            "MRR": scoreToTen(consensusSummary.avg_mean_reciprocal_rank ?? 0),
            "Heading": scoreToTen(consensusSummary.heading_match_rate ?? 0),
            "Fact Consist.": consensusSummary.avg_judge_fact_consistency ?? 0,
            "Q Quality": consensusSummary.avg_judge_question_quality ?? 0,
            isBaseline: true,
          }] : []),
          ...summaries.map((s) => ({
            name: s.model_name,
            "Top-1": scoreToTen(s.top1_accuracy),
            "MRR": scoreToTen(s.avg_mean_reciprocal_rank ?? 0),
            "Heading": scoreToTen(s.heading_match_rate ?? 0),
            "Fact Consist.": s.avg_judge_fact_consistency ?? 0,
            "Q Quality": s.avg_judge_question_quality ?? 0,
            isBaseline: false,
          })),
        ].sort((a, b) => b["Top-1"] - a["Top-1"]);

        return (
          <section className="bg-gray-900 rounded-lg p-4">
            <h3 className="text-sm font-medium mb-1">Primary Quality Dimensions (0-10)</h3>
            <p className="text-xs text-gray-500 mb-4">
              Three deterministic accuracy signals (Top-1, MRR, Heading match, all scaled to 0-10) plus the two LLM signals (Fact Consist., Q Quality). Reference included for direct comparison.
            </p>
            <ResponsiveContainer width="100%" height={Math.max(200, judgeScoreData.length * 44 + 40)}>
              <BarChart data={judgeScoreData} layout="vertical" margin={{ left: 20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" horizontal={false} />
                <XAxis type="number" domain={[0, 10]} tick={{ fill: "#9ca3af" }} />
                <YAxis
                  type="category"
                  dataKey="name"
                  tick={{ fill: "#9ca3af", fontSize: 11 }}
                  width={160}
                />
                <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "none" }} />
                <Legend />
                <Bar dataKey="Top-1" fill="#3b82f6" barSize={12} />
                <Bar dataKey="MRR" fill="#06b6d4" barSize={12} />
                <Bar dataKey="Heading" fill="#10b981" barSize={12} />
                <Bar dataKey="Fact Consist." fill="#8b5cf6" barSize={12} />
                <Bar dataKey="Q Quality" fill="#f59e0b" barSize={12} />
              </BarChart>
            </ResponsiveContainer>
          </section>
        );
      })()}

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Latency chart */}
        <section className="bg-gray-900 rounded-lg p-4">
          <h3 className="text-sm font-medium mb-4">
            Avg Total Latency (all Q&A rounds)
          </h3>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={latencyData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="name" tick={{ fill: "#9ca3af", fontSize: 11 }} angle={-20} textAnchor="end" height={60} />
              <YAxis tick={{ fill: "#9ca3af" }} />
              <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "none" }} />
              <Bar dataKey="Avg Total Latency (ms)" fill="#3b82f6" />
            </BarChart>
          </ResponsiveContainer>
        </section>

        {/* Radar chart */}
        <section className="bg-gray-900 rounded-lg p-4">
          <h3 className="text-sm font-medium mb-4">Quality Metrics</h3>
          <ResponsiveContainer width="100%" height={300}>
            <RadarChart data={radarData}>
              <PolarGrid stroke="#374151" />
              <PolarAngleAxis dataKey="metric" tick={{ fill: "#9ca3af", fontSize: 11 }} />
              <PolarRadiusAxis tick={{ fill: "#6b7280" }} domain={[0, 1]} />
              {radarModels.map((s, i) => (
                <Radar
                  key={s.model_id}
                  name={s.model_name}
                  dataKey={s.model_name}
                  stroke={s.model_id === "consensus" ? "#f59e0b" : COLORS[i % COLORS.length]}
                  fill={s.model_id === "consensus" ? "#f59e0b" : COLORS[i % COLORS.length]}
                  fillOpacity={s.model_id === "consensus" ? 0.08 : 0.15}
                  strokeDasharray={s.model_id === "consensus" ? "5 5" : undefined}
                />
              ))}
              <Legend />
            </RadarChart>
          </ResponsiveContainer>
        </section>

        {/* Cost chart */}
        <section className="bg-gray-900 rounded-lg p-4">
          <h3 className="text-sm font-medium mb-4">Cost per Classification</h3>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={costData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="name" tick={{ fill: "#9ca3af", fontSize: 11 }} angle={-20} textAnchor="end" height={60} />
              <YAxis tick={{ fill: "#9ca3af" }} />
              <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "none" }} />
              <Bar dataKey="Avg Cost/Classification ($)" fill="#10b981" />
            </BarChart>
          </ResponsiveContainer>
        </section>

        {/* Rounds chart */}
        <section className="bg-gray-900 rounded-lg p-4">
          <h3 className="text-sm font-medium mb-4">Avg Q&A Rounds to Classify</h3>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={latencyData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="name" tick={{ fill: "#9ca3af", fontSize: 11 }} angle={-20} textAnchor="end" height={60} />
              <YAxis tick={{ fill: "#9ca3af" }} />
              <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "none" }} />
              <Bar dataKey="Avg Rounds" fill="#f59e0b" />
            </BarChart>
          </ResponsiveContainer>
        </section>
      </div>

      {/* Latency per round: prompt x model matrix */}
      <section>
        <h2 className="text-lg font-semibold mb-4">Avg Time per Round (ms) - by Prompt x Model</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-700 text-left text-gray-400">
                <th className="py-2 pr-4">Prompt</th>
                {baselineSummary && (
                  <th className="py-2 pr-4 text-right">{baselineSummary.name}</th>
                )}
                {summaries.map((s) => (
                  <th key={s.model_id} className="py-2 pr-4 text-right">{s.model_name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {results.prompt_indices.map((pi) => {
                const bl = baseline_results.find((r) => r.prompt_index === pi);
                const blMsPerRound = bl && bl.total_rounds > 0
                  ? Math.round(bl.total_latency_ms / bl.total_rounds) : null;

                return (
                  <tr key={pi} className="border-b border-gray-800 hover:bg-gray-900">
                    <td className="py-2 pr-4 text-gray-300">#{pi}</td>
                    {baselineSummary && (
                      <td className="py-2 pr-4 text-right font-mono">
                        {blMsPerRound != null ? `${blMsPerRound}ms` : "-"}
                        {bl && <span className="text-gray-600 ml-1">({bl.total_rounds}r)</span>}
                      </td>
                    )}
                    {summaries.map((s) => {
                      const mr = model_results.find(
                        (r) => r.model_id === s.model_id && r.prompt_index === pi,
                      );
                      const msPerRound = mr && mr.total_rounds > 0
                        ? Math.round(mr.total_latency_ms / mr.total_rounds) : null;
                      const ratio = blMsPerRound && msPerRound
                        ? msPerRound / blMsPerRound : null;

                      return (
                        <td key={s.model_id} className="py-2 pr-4 text-right font-mono">
                          {msPerRound != null ? (
                            <>
                              {msPerRound}ms
                              <span className="text-gray-600 ml-1">({mr!.total_rounds}r)</span>
                              {ratio != null && (
                                <span className={`ml-2 text-xs ${ratio < 1 ? "text-green-400" : ratio > 1.5 ? "text-red-400" : "text-amber-400"}`}>
                                  {ratio < 1 ? "" : "+"}{((ratio - 1) * 100).toFixed(0)}%
                                </span>
                              )}
                            </>
                          ) : (
                            <span className="text-gray-600">-</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
              {/* Average row */}
              <tr className="border-t-2 border-gray-600 font-medium">
                <td className="py-2 pr-4 text-gray-300">Avg</td>
                {baselineSummary && (
                  <td className="py-2 pr-4 text-right font-mono">
                    {baselineSummary.avgRounds > 0
                      ? `${Math.round(baselineSummary.avgLatency / baselineSummary.avgRounds)}ms`
                      : "-"}
                  </td>
                )}
                {summaries.map((s) => (
                  <td key={s.model_id} className="py-2 pr-4 text-right font-mono">
                    {s.avg_rounds > 0 ? `${Math.round(s.avg_total_latency_ms / s.avg_rounds)}ms` : "-"}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      {/* Per-section accuracy matrix - stratified by OTT section */}
      {results.prompt_sections && Object.keys(results.prompt_sections).length > 0 && (() => {
        // Group prompts by section_number, then compute per-(section, model) top1
        const promptsBySectionNum = new Map<number, { section: typeof results.prompt_sections[string]; promptIndices: number[] }>();
        for (const [piStr, section] of Object.entries(results.prompt_sections)) {
          const pi = Number(piStr);
          if (!promptsBySectionNum.has(section.number)) {
            promptsBySectionNum.set(section.number, { section, promptIndices: [] });
          }
          promptsBySectionNum.get(section.number)!.promptIndices.push(pi);
        }
        const sectionsSorted = Array.from(promptsBySectionNum.values())
          .sort((a, b) => a.section.number - b.section.number);

        // Build model × section top1 matrix from evaluations
        const candidateModelIds = summaries.map((s) => s.model_id);
        const matrix: Record<string, Record<number, { hit: number; total: number }>> = {};
        for (const mid of candidateModelIds) {
          matrix[mid] = {};
          for (const { section } of sectionsSorted) matrix[mid][section.number] = { hit: 0, total: 0 };
        }
        for (const ev of evaluations) {
          if (ev.model_id === "consensus") continue;
          const section = results.prompt_sections?.[String(ev.prompt_index)];
          if (!section || !(ev.model_id in matrix)) continue;
          const cell = matrix[ev.model_id][section.number];
          if (cell) {
            cell.total += 1;
            if (ev.top1_match) cell.hit += 1;
          }
        }

        return (
          <section className="bg-gray-900 rounded-lg p-4 border border-gray-800">
            <div className="mb-3">
              <h3 className="text-sm font-medium">Accuracy by OTT Section</h3>
              <p className="text-xs text-gray-500 mt-0.5">
                Top-1 accuracy broken down by UK{"/"}HS section (derived from the reference's top code per prompt). Shows which categories a model handles vs struggles with.
              </p>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-500 border-b border-gray-800">
                    <th className="py-1.5 pr-3">Section</th>
                    <th className="py-1.5 pr-3 text-right w-16">Prompts</th>
                    {candidateModelIds.map((mid) => (
                      <th key={mid} className="py-1.5 pr-3 text-right">
                        {summaries.find((s) => s.model_id === mid)?.model_name ?? mid}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sectionsSorted.map(({ section, promptIndices }) => (
                    <tr key={section.number} className="border-b border-gray-900/70">
                      <td className="py-1.5 pr-3">
                        <SectionChip section={section} />
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono text-gray-400">
                        {promptIndices.length}
                      </td>
                      {candidateModelIds.map((mid) => {
                        const cell = matrix[mid][section.number];
                        if (!cell || cell.total === 0) {
                          return <td key={mid} className="py-1.5 pr-3 text-right text-gray-700">—</td>;
                        }
                        const pct = (cell.hit / cell.total) * 100;
                        return (
                          <td key={mid} className="py-1.5 pr-3 text-right font-mono">
                            <span className={
                              pct >= 80 ? "text-emerald-300"
                              : pct >= 50 ? "text-amber-300"
                              : "text-red-300"
                            }>
                              {pct.toFixed(0)}%
                            </span>
                            <span className="text-gray-600 ml-1">({cell.hit}/{cell.total})</span>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        );
      })()}

      {/* Fact store: how the per-prompt schema was built */}
      <FactStorePanel results={results} rawQueryByPrompt={rawQueryByPrompt} />

      {/* Detailed per-prompt results */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Per-Prompt Results</h2>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-400">Filter prompt:</label>
            <select
              value={promptFilter}
              onChange={(e) => setPromptFilter(e.target.value === "all" ? "all" : Number(e.target.value))}
              className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm"
            >
              <option value="all">All prompts</option>
              {results.prompt_indices.map((pi) => (
                <option key={pi} value={pi}>Prompt #{pi}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="space-y-2">
          {evaluations
            .filter((ev) => promptFilter === "all" || ev.prompt_index === promptFilter)
            .map((ev) => {
            const key = `${ev.model_id}-${ev.prompt_index}`;
            const isExpanded = expandedRows.has(key);
            const modelResult = findResult(ev.model_id, ev.prompt_index, model_results);
            const baselineResult = findResult(
              baseline_results[0]?.model_id || "",
              ev.prompt_index,
              baseline_results,
            );

            return (
              <div key={key} className="bg-gray-900 rounded-lg">
                <div
                  className={`p-3 flex items-center justify-between cursor-pointer hover:bg-gray-800 rounded-lg ${
                    !ev.top1_match && ev.model_id !== "consensus"
                      ? "border-l-4 border-amber-700"
                      : ""
                  }`}
                  onClick={() => toggleExpand(key)}
                >
                  <div className="flex items-center gap-3 text-sm">
                    <span className="font-medium w-48 truncate">{ev.model_id}</span>
                    <span className="text-gray-400">Prompt #{ev.prompt_index}</span>
                    {results.prompt_sections?.[String(ev.prompt_index)] && (
                      <SectionChip section={results.prompt_sections[String(ev.prompt_index)]} compact />
                    )}
                    {/* Divergence badge: show candidate's top code vs reference's */}
                    {ev.model_id !== "consensus" && (() => {
                      const candTop = modelResult && (() => {
                        try {
                          const p = JSON.parse(modelResult.response_text || "{}");
                          return p?.answers?.[0]?.commodity_code ?? null;
                        } catch { return null; }
                      })();
                      const refTop = baselineResult && (() => {
                        try {
                          const p = JSON.parse(baselineResult.response_text || "{}");
                          return p?.answers?.[0]?.commodity_code ?? null;
                        } catch { return null; }
                      })();
                      if (!candTop || !refTop) return null;
                      if (candTop === refTop) {
                        return (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-200 border border-emerald-700 font-mono">
                            ✓ {candTop}
                          </span>
                        );
                      }
                      return (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/50 text-amber-200 border border-amber-700 font-mono" title={`ref: ${refTop}`}>
                          ✗ {candTop} (ref: {refTop})
                        </span>
                      );
                    })()}
                  </div>
                  <div className="flex items-center gap-6 text-xs">
                    {ev.judge_fact_consistency != null && (
                      <span>Fact: <span className="text-emerald-400">{ev.judge_fact_consistency.toFixed(1)}/10</span></span>
                    )}
                    {ev.judge_question_quality != null && (
                      <span>Q: <span className="text-indigo-400">{ev.judge_question_quality.toFixed(1)}/10</span></span>
                    )}
                    <span>MRR: {(ev.mean_reciprocal_rank ?? 0).toFixed(2)}</span>
                    <span>{ev.total_latency_ms.toFixed(0)}ms ({ev.total_rounds}r)</span>
                    <span className={ev.speed_factor > 1 ? "text-green-400" : "text-red-400"}>
                      {ev.speed_factor.toFixed(2)}x
                    </span>
                    <span>{isExpanded ? "[-]" : "[+]"}</span>
                  </div>
                </div>

                {isExpanded && modelResult && (
                  <div className="px-4 pb-4 border-t border-gray-800">
                    <div className="grid grid-cols-2 gap-4 mt-3">
                      {/* Reference Q&A Loop */}
                      <div>
                        <h4 className="text-xs font-medium text-amber-400 mb-2 flex items-center gap-3 flex-wrap">
                          <span>
                            Reference ({baselineResult?.total_rounds || 0} rounds,{" "}
                            {baselineResult?.total_latency_ms.toFixed(0)}ms, ${baselineResult?.total_cost.toFixed(4)})
                          </span>
                          {(baselineResult?.total_simulator_cost ?? 0) > 0 && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-300 font-normal">
                              sim ${(baselineResult!.total_simulator_cost ?? 0).toFixed(6)}
                              {baselineResult!.simulator_store_hits ? (
                                <span className="ml-2 text-indigo-300">
                                  {baselineResult!.simulator_store_hits} store-hit{baselineResult!.simulator_store_hits === 1 ? "" : "s"}
                                </span>
                              ) : null}
                            </span>
                          )}
                        </h4>
                        {baselineResult?.rounds?.map((r) => (
                          <div key={r.round_number} className="mb-3 border-l-2 border-amber-800 pl-3">
                            <div className="text-xs font-medium text-gray-300 mb-1">
                              Round {r.round_number}
                              <span className={`ml-2 px-1.5 py-0.5 rounded text-xs ${
                                r.response_type === "answers" ? "bg-green-900 text-green-300" :
                                r.response_type === "questions" ? "bg-blue-900 text-blue-300" :
                                "bg-gray-700 text-gray-300"
                              }`}>{r.response_type}</span>
                              <span className="ml-2 text-gray-500">{r.latency_ms.toFixed(0)}ms</span>
                            </div>
                            <pre className="bg-gray-800 rounded p-2 text-xs overflow-auto max-h-40 whitespace-pre-wrap">
                              {r.response_text.slice(0, 2000)}{r.response_text.length > 2000 ? "..." : ""}
                            </pre>
                            <SimulatorTraceBlock round={r} accent="amber" />
                          </div>
                        )) || (
                          <pre className="bg-gray-800 rounded p-2 text-xs overflow-auto max-h-60 whitespace-pre-wrap">
                            {baselineResult?.response_text || "N/A"}
                          </pre>
                        )}
                      </div>
                      {/* Model Q&A Loop */}
                      <div>
                        <h4 className="text-xs font-medium text-blue-400 mb-2 flex items-center gap-3 flex-wrap">
                          <span>
                            {ev.model_id} ({modelResult.total_rounds} rounds,{" "}
                            {modelResult.total_latency_ms.toFixed(0)}ms, ${modelResult.total_cost.toFixed(4)})
                          </span>
                          {(modelResult.total_simulator_cost ?? 0) > 0 && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-300 font-normal">
                              sim ${(modelResult.total_simulator_cost ?? 0).toFixed(6)}
                              {modelResult.simulator_store_hits ? (
                                <span className="ml-2 text-indigo-300">
                                  {modelResult.simulator_store_hits} store-hit{modelResult.simulator_store_hits === 1 ? "" : "s"}
                                </span>
                              ) : null}
                            </span>
                          )}
                        </h4>
                        {modelResult.rounds?.map((r) => (
                          <div key={r.round_number} className="mb-3 border-l-2 border-blue-800 pl-3">
                            <div className="text-xs font-medium text-gray-300 mb-1">
                              Round {r.round_number}
                              <span className={`ml-2 px-1.5 py-0.5 rounded text-xs ${
                                r.response_type === "answers" ? "bg-green-900 text-green-300" :
                                r.response_type === "questions" ? "bg-blue-900 text-blue-300" :
                                "bg-gray-700 text-gray-300"
                              }`}>{r.response_type}</span>
                              <span className="ml-2 text-gray-500">{r.latency_ms.toFixed(0)}ms</span>
                            </div>
                            <pre className="bg-gray-800 rounded p-2 text-xs overflow-auto max-h-40 whitespace-pre-wrap">
                              {r.response_text.slice(0, 2000)}{r.response_text.length > 2000 ? "..." : ""}
                            </pre>
                            <SimulatorTraceBlock round={r} accent="blue" />
                          </div>
                        )) || (
                          <pre className="bg-gray-800 rounded p-2 text-xs overflow-auto max-h-60 whitespace-pre-wrap">
                            {modelResult.response_text}
                          </pre>
                        )}
                      </div>
                    </div>
                    {/* Judge evaluation */}
                    {ev.judge_score != null && (
                      <div className="mt-3 border-t border-gray-800 pt-3">
                        <h4 className="text-xs font-medium text-purple-400 mb-2">
                          LLM Judge (GPT-5.2) Evaluation
                        </h4>
                        <div className="flex items-center gap-6 text-xs mb-2">
                          <span>Overall: <span className="text-purple-300 font-medium">{ev.judge_score.toFixed(1)}/10</span></span>
                          {ev.judge_classification_accuracy != null && (
                            <span>Accuracy: <span className="text-purple-300">{ev.judge_classification_accuracy.toFixed(1)}/10</span></span>
                          )}
                          {ev.judge_question_quality != null && (
                            <span>Q Quality: <span className="text-purple-300">{ev.judge_question_quality.toFixed(1)}/10</span></span>
                          )}
                          {ev.judge_structured_output != null && (
                            <span>Structure: <span className="text-purple-300">{ev.judge_structured_output.toFixed(1)}/10</span></span>
                          )}
                        </div>
                        {ev.judge_reasoning && (
                          <pre className="bg-gray-800 rounded p-2 text-xs overflow-auto max-h-32 whitespace-pre-wrap text-gray-400">
                            {ev.judge_reasoning}
                          </pre>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
