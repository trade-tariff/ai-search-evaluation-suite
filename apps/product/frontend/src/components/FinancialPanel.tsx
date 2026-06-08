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
import { getBenchmarkResults, listRuns, getRunResults } from "../api";
import type { RunListItem } from "../api";
import type { BenchmarkResults } from "../types";

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"];

function runLabel(r: RunListItem) {
  return `${new Date(r.timestamp).toLocaleString()} - ${r.prompt_count}p x ${r.model_count}m - OS:${r.opensearch_limit}`;
}

export default function FinancialPanel() {
  const [results, setResults] = useState<BenchmarkResults | null>(null);
  const [savedRuns, setSavedRuns] = useState<RunListItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>("current");
  const [err, setErr] = useState("");

  useEffect(() => {
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
    quality: s.top1_accuracy * 10,
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
