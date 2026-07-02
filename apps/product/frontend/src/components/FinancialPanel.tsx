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
  ScatterChart,
  Scatter,
  ZAxis,
  Cell,
} from "recharts";
import { getBenchmarkResults, getEvalCostSummary, listRuns, getRunResults } from "../api";
import type { EvalCostSummary, RunListItem } from "../api";
import type { BenchmarkResults } from "../types";

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"];

function runLabel(r: RunListItem) {
  return `${new Date(r.timestamp).toLocaleString()} - ${r.prompt_count}p x ${r.model_count}m - OS:${r.opensearch_limit}`;
}

function usd(value: number | null | undefined) {
  return `$${Number(value || 0).toFixed(4)}`;
}

function compactInt(value: number | null | undefined) {
  return Number(value || 0).toLocaleString();
}

function duration(value: number | null | undefined) {
  const seconds = Number(value || 0);
  if (seconds <= 0) return "-";
  if (seconds < 90) return `${seconds.toFixed(0)}s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${minutes.toFixed(1)}m`;
  return `${(minutes / 60).toFixed(1)}h`;
}

export default function FinancialPanel() {
  const [results, setResults] = useState<BenchmarkResults | null>(null);
  const [evalCost, setEvalCost] = useState<EvalCostSummary | null>(null);
  const [savedRuns, setSavedRuns] = useState<RunListItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>("current");
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;

    const refreshEvalCost = () => {
      getEvalCostSummary()
        .then((summary) => {
          if (!cancelled) setEvalCost(summary);
        })
        .catch(() => {
          if (!cancelled) setEvalCost(null);
        });
    };

    refreshEvalCost();
    const evalCostTimer = window.setInterval(refreshEvalCost, 30000);

    (async () => {
      try {
        const runs = await listRuns();
        if (cancelled) return;
        setSavedRuns(runs);
        try {
          const live = await getBenchmarkResults();
          if (cancelled) return;
          setResults(live);
          setSelectedRunId("current");
        } catch {
          if (runs.length > 0) {
            const mostRecent = [...runs].sort(
              (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime(),
            )[0];
            const saved = await getRunResults(mostRecent.id);
            if (cancelled) return;
            setResults(saved);
            setSelectedRunId(mostRecent.id);
          } else if (!cancelled) {
            setErr("No benchmark results yet. Run a benchmark first.");
          }
        }
      } catch (e) {
        if (!cancelled) setErr("Failed to load runs: " + String(e));
      }
    })();

    return () => {
      cancelled = true;
      window.clearInterval(evalCostTimer);
    };
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

  if (err) return <div className="text-gray-400">{err}</div>;
  if (!results) return <div className="text-gray-400">Loading results...</div>;

  const { summaries, evaluations, baseline_results, model_results } = results;
  const spendTotals = evalCost?.spend_totals;

  // -- Aggregate costs --
  const baselineInferenceCost = baseline_results.reduce((s, r) => s + r.total_cost, 0);
  const modelInferenceCost = model_results.reduce((s, r) => s + r.total_cost, 0);
  const judgeCost = evaluations.reduce((s, e) => s + e.judge_cost, 0);
  const totalCost = baselineInferenceCost + modelInferenceCost + judgeCost;

  // -- Token totals --
  const baselineTokens = baseline_results.reduce((s, r) => s + r.total_input_tokens + r.total_output_tokens, 0);
  const modelTokens = model_results.reduce((s, r) => s + r.total_input_tokens + r.total_output_tokens, 0);

  // -- Per-model cost breakdown --
  const modelCostMap = new Map<string, { inference: number; judge: number; inputTokens: number; outputTokens: number; prompts: number }>();
  for (const r of model_results) {
    const entry = modelCostMap.get(r.model_id) || { inference: 0, judge: 0, inputTokens: 0, outputTokens: 0, prompts: 0 };
    entry.inference += r.total_cost;
    entry.inputTokens += r.total_input_tokens;
    entry.outputTokens += r.total_output_tokens;
    entry.prompts += 1;
    modelCostMap.set(r.model_id, entry);
  }
  for (const ev of evaluations) {
    const entry = modelCostMap.get(ev.model_id);
    if (entry) entry.judge += ev.judge_cost;
  }

  const costBreakdownData = Array.from(modelCostMap.entries()).map(([modelId, data]) => {
    const summary = summaries.find((s) => s.model_id === modelId);
    return {
      name: summary?.model_name || modelId,
      model_id: modelId,
      "Inference Cost ($)": Number(data.inference.toFixed(4)),
      "Judge Cost ($)": Number(data.judge.toFixed(4)),
      "Total ($)": Number((data.inference + data.judge).toFixed(4)),
      inputTokens: data.inputTokens,
      outputTokens: data.outputTokens,
      prompts: data.prompts,
    };
  });

  // Add reference to cost breakdown
  const baselineName = `Reference (${baseline_results[0]?.model_id || "?"})`;
  costBreakdownData.unshift({
    name: baselineName,
    model_id: baseline_results[0]?.model_id || "",
    "Inference Cost ($)": Number(baselineInferenceCost.toFixed(4)),
    "Judge Cost ($)": 0,
    "Total ($)": Number(baselineInferenceCost.toFixed(4)),
    inputTokens: baseline_results.reduce((s, r) => s + r.total_input_tokens, 0),
    outputTokens: baseline_results.reduce((s, r) => s + r.total_output_tokens, 0),
    prompts: baseline_results.length,
  });

  // -- Cost per classification chart --
  const costPerClassData = summaries.map((s) => ({
    name: s.model_name,
    "Cost/Classification ($)": Number(s.avg_cost_per_classification.toFixed(6)),
  }));

  // -- Cost vs Quality scatter --
  // Quality axis uses Top-1 accuracy scaled to 0-10 for legibility. Top-1 is
  // the single strongest deterministic signal in the new scoring system.
  // Pareto frontier computed: a model is DOMINATED if another exists with
  // equal-or-better cost AND equal-or-better quality (at least one strict).
  // Non-dominated models are on the efficient frontier - they represent
  // genuine tradeoff choices, the rest are strictly worse alternatives.
  const scatterRaw = summaries.map((s) => ({
    name: s.model_name,
    model_id: s.model_id,
    cost: Number(s.avg_cost_per_classification.toFixed(6)),
    quality: (s.top1_accuracy ?? s.gold_top1_rate ?? 0) * 10,
  }));
  const paretoSet = new Set<string>();
  for (const a of scatterRaw) {
    let dominated = false;
    for (const b of scatterRaw) {
      if (a.model_id === b.model_id) continue;
      // b dominates a if b.cost <= a.cost AND b.quality >= a.quality with at
      // least one strict inequality
      if (
        b.cost <= a.cost &&
        b.quality >= a.quality &&
        (b.cost < a.cost || b.quality > a.quality)
      ) {
        dominated = true;
        break;
      }
    }
    if (!dominated) paretoSet.add(a.model_id);
  }
  const scatterData = scatterRaw.map((d) => ({
    ...d,
    isFrontier: paretoSet.has(d.model_id),
    label: `${d.name}: $${d.cost.toFixed(4)} / top-1 ${d.quality.toFixed(1)}/10${paretoSet.has(d.model_id) ? " · FRONTIER" : " · dominated"}`,
  }));

  // -- Per-prompt cost detail --
  const promptCosts = results.prompt_indices.map((pi) => {
    const baselineR = baseline_results.find((r) => r.prompt_index === pi);
    const modelRs = model_results.filter((r) => r.prompt_index === pi);
    const judgeC = evaluations.filter((e) => e.prompt_index === pi).reduce((s, e) => s + e.judge_cost, 0);
    return {
      promptIndex: pi,
      baselineCost: baselineR?.total_cost || 0,
      modelCost: modelRs.reduce((s, r) => s + r.total_cost, 0),
      judgeCost: judgeC,
      total: (baselineR?.total_cost || 0) + modelRs.reduce((s, r) => s + r.total_cost, 0) + judgeC,
    };
  });

  return (
    <div className="space-y-8">
      {/* Run selector */}
      <div className="flex items-center gap-4">
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
      </div>

      {evalCost && (
        <section className="rounded-lg border border-emerald-900/60 bg-emerald-950/20 p-5">
          <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
            <div>
              <h2 className="text-lg font-semibold text-emerald-100">Project costs</h2>
              <p className="mt-1 text-sm text-gray-400">
                Tracked and estimated project spend across fact extraction, retrieval embeddings, E2E Q&A, and classification matrix runs. Benchmark run costs are shown below.
              </p>
            </div>
            <div className="text-xs text-gray-500">
              {evalCost.totals.last_write ? `updated ${new Date(evalCost.totals.last_write).toLocaleString()}` : "no writes yet"}
            </div>
          </div>

          {spendTotals && (
            <>
              <div className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-5">
                <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
                  <div className="text-xs uppercase tracking-wider text-gray-500">Tracked + Estimated Spend</div>
                  <div className="mt-1 text-2xl font-semibold text-emerald-300">{usd(spendTotals.estimated_total_usd)}</div>
                </div>
                <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
                  <div className="text-xs uppercase tracking-wider text-gray-500">Fact Extraction</div>
                  <div className="mt-1 text-2xl font-semibold text-gray-100">{usd(spendTotals.fact_eval_cost_usd)}</div>
                  <div className="mt-1 text-xs text-gray-500">exact ledger</div>
                </div>
                <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
                  <div className="text-xs uppercase tracking-wider text-gray-500">Retrieval Embeddings</div>
                  <div className="mt-1 text-2xl font-semibold text-gray-100">{usd(spendTotals.retrieval_embedding_est_cost_usd)}</div>
                  <div className="mt-1 text-xs text-gray-500">estimated</div>
                </div>
                <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
                  <div className="text-xs uppercase tracking-wider text-gray-500">E2E Q&A</div>
                  <div className="mt-1 text-2xl font-semibold text-gray-100">{usd(spendTotals.e2e_est_cost_usd)}</div>
                  <div className="mt-1 text-xs text-gray-500">estimated</div>
                </div>
                <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
                  <div className="text-xs uppercase tracking-wider text-gray-500">Classification Matrix</div>
                  <div className="mt-1 text-2xl font-semibold text-gray-100">{usd(spendTotals.classification_est_cost_usd)}</div>
                  <div className="mt-1 text-xs text-gray-500">estimated</div>
                </div>
              </div>
              <p className="mt-2 text-xs text-gray-500">
                Estimates use ${spendTotals.embedding_cost_per_million_tokens.toFixed(4)}/1M embedding tokens and ${spendTotals.e2e_provider_call_est_usd.toFixed(4)} per E2E provider call when exact token usage is not stored.
              </p>
            </>
          )}

          <div className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-4">
            <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
              <div className="text-xs uppercase tracking-wider text-gray-500">Fact Eval Spend</div>
              <div className="mt-1 text-2xl font-semibold text-emerald-300">{usd(evalCost.totals.cost_usd)}</div>
            </div>
            <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
              <div className="text-xs uppercase tracking-wider text-gray-500">Calls</div>
              <div className="mt-1 text-2xl font-semibold text-gray-100">{compactInt(evalCost.totals.calls)}</div>
              <div className="mt-1 text-xs text-gray-500">{compactInt(evalCost.totals.failed)} failed</div>
            </div>
            <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
              <div className="text-xs uppercase tracking-wider text-gray-500">Prompt Tokens</div>
              <div className="mt-1 text-2xl font-semibold text-gray-100">{compactInt(evalCost.totals.prompt_tokens)}</div>
            </div>
            <div className="rounded border border-gray-800 bg-gray-950/70 p-4">
              <div className="text-xs uppercase tracking-wider text-gray-500">Completion Tokens</div>
              <div className="mt-1 text-2xl font-semibold text-gray-100">{compactInt(evalCost.totals.completion_tokens)}</div>
            </div>
          </div>

          <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,0.65fr)]">
            <div className="overflow-x-auto">
              <h3 className="mb-3 text-sm font-medium text-gray-200">Recent eval runs</h3>
              <table className="w-full min-w-[760px] text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-left text-gray-400">
                    <th className="py-2 pr-4">Run</th>
                    <th className="py-2 pr-4 text-right">Calls</th>
                    <th className="py-2 pr-4 text-right">CCs</th>
                    <th className="py-2 pr-4 text-right">Prompts</th>
                    <th className="py-2 pr-4 text-right">Cost</th>
                    <th className="py-2 pr-4 text-right">Avg score</th>
                    <th className="py-2 text-right">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {evalCost.runs.map((run) => (
                    <tr key={run.run_id} className="border-b border-gray-900">
                      <td className="max-w-[280px] truncate py-2 pr-4 font-mono text-xs text-gray-300" title={run.run_id}>
                        {run.run_id}
                      </td>
                      <td className="py-2 pr-4 text-right">{compactInt(run.calls)}</td>
                      <td className="py-2 pr-4 text-right">{compactInt(run.commodity_codes)}</td>
                      <td className="py-2 pr-4 text-right">{compactInt(run.prompt_versions)}</td>
                      <td className="py-2 pr-4 text-right text-emerald-300">{usd(run.cost_usd)}</td>
                      <td className="py-2 pr-4 text-right">{run.avg_score == null ? "-" : run.avg_score.toFixed(1)}</td>
                      <td className="py-2 text-right">{duration(run.duration_seconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="overflow-x-auto">
              <h3 className="mb-3 text-sm font-medium text-gray-200">Eval spend by model</h3>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-left text-gray-400">
                    <th className="py-2 pr-4">Model</th>
                    <th className="py-2 pr-4 text-right">Calls</th>
                    <th className="py-2 text-right">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {evalCost.model_totals.map((row) => (
                    <tr key={row.model} className="border-b border-gray-900">
                      <td className="py-2 pr-4 font-mono text-xs text-gray-300">{row.model}</td>
                      <td className="py-2 pr-4 text-right">{compactInt(row.calls)}</td>
                      <td className="py-2 text-right text-emerald-300">{usd(row.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        {evalCost.retrieval && evalCost.e2e && evalCost.classification && (
          <div className="mt-5 grid gap-5 lg:grid-cols-3">
            <div className="overflow-x-auto">
              <h3 className="mb-3 text-sm font-medium text-gray-200">Recent retrieval spend</h3>
              <table className="w-full min-w-[520px] text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-left text-gray-400">
                    <th className="py-2 pr-4">Run</th>
                    <th className="py-2 pr-4 text-right">Queries</th>
                    <th className="py-2 pr-4 text-right">Emb tokens</th>
                    <th className="py-2 text-right">Est cost</th>
                  </tr>
                </thead>
                <tbody>
                  {evalCost.retrieval.runs.slice(0, 8).map((run) => (
                    <tr key={run.id} className="border-b border-gray-900">
                      <td className="max-w-[180px] truncate py-2 pr-4 font-mono text-xs text-gray-300" title={run.run_label}>{run.run_label}</td>
                      <td className="py-2 pr-4 text-right">{compactInt(run.calls || run.n_queries)}</td>
                      <td className="py-2 pr-4 text-right">{compactInt(run.estimated_embedding_tokens)}</td>
                      <td className="py-2 text-right text-emerald-300">{usd(run.estimated_cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="overflow-x-auto">
              <h3 className="mb-3 text-sm font-medium text-gray-200">Recent E2E Q&A spend</h3>
              <table className="w-full min-w-[560px] text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-left text-gray-400">
                    <th className="py-2 pr-4">Run</th>
                    <th className="py-2 pr-4">Mode</th>
                    <th className="py-2 pr-4 text-right">Calls</th>
                    <th className="py-2 text-right">Est cost</th>
                  </tr>
                </thead>
                <tbody>
                  {evalCost.e2e.runs.slice(0, 8).map((run) => (
                    <tr key={run.id} className="border-b border-gray-900">
                      <td className="max-w-[170px] truncate py-2 pr-4 font-mono text-xs text-gray-300" title={run.run_label}>{run.run_label}</td>
                      <td className="py-2 pr-4 text-xs text-gray-300">{run.question_mode}</td>
                      <td className="py-2 pr-4 text-right">{compactInt(run.provider_calls_used)}</td>
                      <td className="py-2 text-right text-emerald-300">{usd(run.estimated_cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="overflow-x-auto">
              <h3 className="mb-3 text-sm font-medium text-gray-200">Classification matrix spend</h3>
              <table className="w-full min-w-[520px] text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-left text-gray-400">
                    <th className="py-2 pr-4">Run</th>
                    <th className="py-2 pr-4 text-right">Sessions</th>
                    <th className="py-2 text-right">Est cost</th>
                  </tr>
                </thead>
                <tbody>
                  {evalCost.classification.runs.slice(0, 8).map((run) => (
                    <tr key={`${run.run_label}-${run.model}-${run.strategy}-${run.prompt_mode}`} className="border-b border-gray-900">
                      <td className="max-w-[210px] truncate py-2 pr-4 font-mono text-xs text-gray-300" title={run.run_label}>{run.run_label}</td>
                      <td className="py-2 pr-4 text-right">{compactInt(run.sessions)}</td>
                      <td className="py-2 text-right text-emerald-300">{usd(run.estimated_cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
        </section>
      )}

      {/* Aggregate summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Total Benchmark Cost</div>
          <div className="text-2xl font-semibold mt-1 text-green-400">${totalCost.toFixed(4)}</div>
        </div>
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Reference Cost</div>
          <div className="text-2xl font-semibold mt-1 text-amber-400">${baselineInferenceCost.toFixed(4)}</div>
          <div className="text-xs text-gray-500 mt-1">{baseline_results.length} classifications</div>
        </div>
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Model Inference Cost</div>
          <div className="text-2xl font-semibold mt-1 text-blue-400">${modelInferenceCost.toFixed(4)}</div>
          <div className="text-xs text-gray-500 mt-1">{model_results.length} classifications</div>
        </div>
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Judge Cost (GPT-5.2)</div>
          <div className="text-2xl font-semibold mt-1 text-purple-400">${judgeCost.toFixed(4)}</div>
          <div className="text-xs text-gray-500 mt-1">{evaluations.length} evaluations</div>
        </div>
      </div>

      {/* Token summary */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Total Tokens</div>
          <div className="text-lg font-semibold mt-1">{(baselineTokens + modelTokens).toLocaleString()}</div>
        </div>
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Reference Tokens</div>
          <div className="text-lg font-semibold mt-1">{baselineTokens.toLocaleString()}</div>
        </div>
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Model Tokens</div>
          <div className="text-lg font-semibold mt-1">{modelTokens.toLocaleString()}</div>
        </div>
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Avg Tokens/Classification</div>
          <div className="text-lg font-semibold mt-1">
            {model_results.length > 0
              ? Math.round(modelTokens / model_results.length).toLocaleString()
              : "-"}
          </div>
        </div>
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Stacked cost breakdown */}
        <section className="bg-gray-900 rounded-lg p-4">
          <h3 className="text-sm font-medium mb-4">Cost Breakdown by Model</h3>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={costBreakdownData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="name" tick={{ fill: "#9ca3af", fontSize: 10 }} angle={-20} textAnchor="end" height={70} />
              <YAxis tick={{ fill: "#9ca3af" }} />
              <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "none" }} />
              <Legend />
              <Bar dataKey="Inference Cost ($)" stackId="cost" fill="#3b82f6" />
              <Bar dataKey="Judge Cost ($)" stackId="cost" fill="#8b5cf6" />
            </BarChart>
          </ResponsiveContainer>
        </section>

        {/* Cost per classification */}
        <section className="bg-gray-900 rounded-lg p-4">
          <h3 className="text-sm font-medium mb-4">Cost per Classification</h3>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={costPerClassData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="name" tick={{ fill: "#9ca3af", fontSize: 10 }} angle={-20} textAnchor="end" height={70} />
              <YAxis tick={{ fill: "#9ca3af" }} />
              <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "none" }} />
              <Bar dataKey="Cost/Classification ($)" fill="#10b981" />
            </BarChart>
          </ResponsiveContainer>
        </section>

        {/* Cost vs Quality scatter */}
        <section className="bg-gray-900 rounded-lg p-4 lg:col-span-2">
          <h3 className="text-sm font-medium mb-4">
            Cost vs Quality Tradeoff
            <span className="text-xs text-gray-500 ml-2">(lower-left = cheap + low quality, upper-left = cheap + high quality)</span>
          </h3>
          <ResponsiveContainer width="100%" height={350}>
            <ScatterChart margin={{ top: 10, right: 30, bottom: 10, left: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis
                type="number"
                dataKey="cost"
                name="Cost/Classification ($)"
                tick={{ fill: "#9ca3af" }}
                label={{ value: "Cost per Classification ($)", position: "insideBottom", offset: -5, fill: "#6b7280", fontSize: 12 }}
              />
              <YAxis
                type="number"
                dataKey="quality"
                name="Top-1 Accuracy"
                tick={{ fill: "#9ca3af" }}
                label={{ value: "Top-1 Accuracy (0-10)", angle: -90, position: "insideLeft", fill: "#6b7280", fontSize: 12 }}
              />
              <ZAxis range={[80, 80]} />
              <Tooltip
                contentStyle={{ backgroundColor: "#1f2937", border: "none" }}
                formatter={(value, name) => {
                  const numericValue = typeof value === "number" ? value : Number(value ?? 0);
                  const label = String(name ?? "");
                  return [
                    label.includes("ost") ? `$${numericValue.toFixed(4)}` : numericValue.toFixed(2),
                    label
                  ];
                }}
              />
              <Scatter data={scatterData} name="Models">
                {scatterData.map((d, i) => (
                  <Cell
                    key={i}
                    fill={d.isFrontier ? "#10b981" : "#6b7280"}
                    stroke={d.isFrontier ? "#059669" : "none"}
                    strokeWidth={d.isFrontier ? 2 : 0}
                  />
                ))}
              </Scatter>
            </ScatterChart>
          </ResponsiveContainer>
          <div className="flex items-center gap-4 mt-1 mb-2 text-xs">
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-500 border-2 border-emerald-600 inline-block" />
              <span className="text-emerald-300">On frontier</span>
              <span className="text-gray-500">(genuine tradeoff choice)</span>
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 rounded-full bg-gray-500 inline-block" />
              <span className="text-gray-400">Dominated</span>
              <span className="text-gray-500">(another model strictly better on cost + quality)</span>
            </span>
          </div>
          <div className="flex flex-wrap gap-3 mt-2 text-xs">
            {scatterData.map((d) => (
              <span key={d.model_id} className="flex items-center gap-1">
                <span
                  className="w-2.5 h-2.5 rounded-full inline-block"
                  style={{ backgroundColor: d.isFrontier ? "#10b981" : "#6b7280" }}
                />
                {d.name}
                {d.isFrontier && <span className="text-emerald-400 text-[10px]">◆</span>}
              </span>
            ))}
          </div>
        </section>
      </div>

      {/* Detailed cost table */}
      <section>
        <h2 className="text-lg font-semibold mb-4">Model Cost Detail</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-700 text-left text-gray-400">
                <th className="py-2 pr-4">Model</th>
                <th className="py-2 pr-4 text-right">Classifications</th>
                <th className="py-2 pr-4 text-right">Input Tokens</th>
                <th className="py-2 pr-4 text-right">Output Tokens</th>
                <th className="py-2 pr-4 text-right">Inference Cost</th>
                <th className="py-2 pr-4 text-right">Judge Cost</th>
                <th className="py-2 pr-4 text-right">Total Cost</th>
                <th className="py-2 text-right">Cost/Classification</th>
              </tr>
            </thead>
            <tbody>
              {costBreakdownData.map((row) => (
                <tr
                  key={row.model_id}
                  className={`border-b border-gray-800 ${row.model_id === baseline_results[0]?.model_id ? "bg-amber-950/20" : ""}`}
                >
                  <td className="py-3 pr-4 font-medium">
                    {row.name}
                    {row.model_id === baseline_results[0]?.model_id && (
                      <span className="ml-2 text-xs px-2 py-0.5 rounded bg-amber-900 text-amber-300">Reference</span>
                    )}
                  </td>
                  <td className="py-3 pr-4 text-right">{row.prompts}</td>
                  <td className="py-3 pr-4 text-right">{row.inputTokens.toLocaleString()}</td>
                  <td className="py-3 pr-4 text-right">{row.outputTokens.toLocaleString()}</td>
                  <td className="py-3 pr-4 text-right">${row["Inference Cost ($)"].toFixed(4)}</td>
                  <td className="py-3 pr-4 text-right text-purple-400">${row["Judge Cost ($)"].toFixed(4)}</td>
                  <td className="py-3 pr-4 text-right font-medium">${row["Total ($)"].toFixed(4)}</td>
                  <td className="py-3 text-right">
                    ${row.prompts > 0 ? (row["Total ($)"] / row.prompts).toFixed(4) : "0.0000"}
                  </td>
                </tr>
              ))}
              {/* Totals row */}
              <tr className="border-t-2 border-gray-600 font-semibold">
                <td className="py-3 pr-4">TOTAL</td>
                <td className="py-3 pr-4 text-right">
                  {costBreakdownData.reduce((s, r) => s + r.prompts, 0)}
                </td>
                <td className="py-3 pr-4 text-right">
                  {costBreakdownData.reduce((s, r) => s + r.inputTokens, 0).toLocaleString()}
                </td>
                <td className="py-3 pr-4 text-right">
                  {costBreakdownData.reduce((s, r) => s + r.outputTokens, 0).toLocaleString()}
                </td>
                <td className="py-3 pr-4 text-right">
                  ${(baselineInferenceCost + modelInferenceCost).toFixed(4)}
                </td>
                <td className="py-3 pr-4 text-right text-purple-400">
                  ${judgeCost.toFixed(4)}
                </td>
                <td className="py-3 pr-4 text-right text-green-400">
                  ${totalCost.toFixed(4)}
                </td>
                <td className="py-3 text-right">-</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      {/* Per-prompt cost breakdown */}
      <section>
        <h2 className="text-lg font-semibold mb-4">Cost per Prompt</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-700 text-left text-gray-400">
                <th className="py-2 pr-4">Prompt</th>
                <th className="py-2 pr-4 text-right">Reference Cost</th>
                <th className="py-2 pr-4 text-right">Model Cost</th>
                <th className="py-2 pr-4 text-right">Judge Cost</th>
                <th className="py-2 text-right">Total</th>
              </tr>
            </thead>
            <tbody>
              {promptCosts.map((pc) => (
                <tr key={pc.promptIndex} className="border-b border-gray-800">
                  <td className="py-2 pr-4 font-mono">#{pc.promptIndex}</td>
                  <td className="py-2 pr-4 text-right">${pc.baselineCost.toFixed(4)}</td>
                  <td className="py-2 pr-4 text-right">${pc.modelCost.toFixed(4)}</td>
                  <td className="py-2 pr-4 text-right text-purple-400">${pc.judgeCost.toFixed(4)}</td>
                  <td className="py-2 text-right font-medium">${pc.total.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
