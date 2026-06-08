import { useEffect, useState } from "react";
import {
  getPrompts,
  getPromptDetail,
  searchProbe,
  searchPreview,
  savePrompt,
  type PreviewResponse,
  type PreviewCandidate,
  type SearchProbe,
} from "../api";
import type { PromptInfo, GoldFact } from "../types";

interface Props {
  selected: number[];
  onSelectionChange: (indices: number[]) => void;
  opensearchLimit: number;
  onOpensearchLimitChange: (limit: number) => void;
}

const LIMIT_PRESETS = [10, 20, 40, 60, 80];

export default function PromptsPanel({
  selected,
  onSelectionChange,
  opensearchLimit,
  onOpensearchLimitChange,
}: Props) {
  const [prompts, setPrompts] = useState<PromptInfo[]>([]);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  // Prompt authoring state
  const [probe, setProbe] = useState<SearchProbe | null>(null);
  const [newQuery, setNewQuery] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [authorMsg, setAuthorMsg] = useState("");

  useEffect(() => {
    getPrompts().then((p) => {
      setPrompts(p);
      if (selected.length === 0) {
        onSelectionChange(p.map((q) => q.index));
      }
    });
    searchProbe().then(setProbe).catch(() => setProbe({ ok: false, error: "probe failed" }));
  }, []);

  const runPreview = async () => {
    if (!newQuery.trim()) return;
    setPreviewing(true);
    setAuthorMsg("");
    try {
      const p = await searchPreview(newQuery.trim(), 80);
      setPreview(p);
    } catch (e) {
      setAuthorMsg(`Preview failed: ${String(e)}`);
      setPreview(null);
    } finally {
      setPreviewing(false);
    }
  };

  const commitSave = async () => {
    if (!preview) return;
    setSaving(true);
    setAuthorMsg("");
    try {
      const { index, total } = await savePrompt(preview);
      setAuthorMsg(`Saved as prompt #${index} (${total} total). Auto-selected for next run.`);
      // Refresh prompts list and auto-select the new one
      const refreshed = await getPrompts();
      setPrompts(refreshed);
      onSelectionChange([...selected, index]);
      setNewQuery("");
      setPreview(null);
    } catch (e) {
      setAuthorMsg(`Save failed: ${String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const toggleAll = () => {
    if (selected.length === prompts.length) {
      onSelectionChange([]);
    } else {
      onSelectionChange(prompts.map((p) => p.index));
    }
  };

  const toggle = (idx: number) => {
    if (selected.includes(idx)) {
      onSelectionChange(selected.filter((i) => i !== idx));
    } else {
      onSelectionChange([...selected, idx]);
    }
  };

  const showDetail = async (idx: number) => {
    const d = await getPromptDetail(idx);
    setDetail(d);
  };

  const allResults = (detail as { top_results: Array<{ commodity_code: string; description: string; score: number }> })?.top_results || [];

  return (
    <div className="space-y-4">
      {/* Prompt authoring - hybrid retrieval preview (OpenSearch + pgvector + RRF) */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="flex items-end justify-between mb-3 gap-2 flex-wrap">
          <div>
            <h3 className="text-sm font-medium">Add a prompt</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Hybrid retrieval against the local UK tariff DB (local OpenSearch keyword leg + pgvector cosine, RRF-fused).
            </p>
          </div>
          <div className="text-xs">
            {probe?.ok ? (
              <div className="text-right">
                <div className="text-emerald-400">
                  Local DB: {probe.embedded?.toLocaleString()} embedded commodities
                </div>
                {probe.opensearch?.configured && (
                  <div className={probe.opensearch.ok ? "text-emerald-400" : "text-amber-300"}>
                    OpenSearch: {probe.opensearch.ok ? `${probe.opensearch.count?.toLocaleString()} indexed` : "fallback to Postgres FTS"}
                  </div>
                )}
              </div>
            ) : (
              <span className="text-red-400">
                Local DB unavailable{probe?.error ? `: ${probe.error}` : ""}
              </span>
            )}
          </div>
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={newQuery}
            onChange={(e) => setNewQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !previewing && newQuery.trim()) runPreview(); }}
            placeholder="e.g. bulk wheat flour supplier Europe"
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm"
            disabled={!probe?.ok || previewing}
          />
          <button
            onClick={runPreview}
            disabled={!probe?.ok || previewing || !newQuery.trim()}
            className="px-4 py-2 text-sm bg-blue-700 hover:bg-blue-600 rounded disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {previewing ? "Searching..." : "Preview"}
          </button>
          {preview && (
            <button
              onClick={commitSave}
              disabled={saving}
              className="px-4 py-2 text-sm bg-emerald-700 hover:bg-emerald-600 rounded disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {saving ? "Saving..." : `Save (${preview.formatted_results.length} results)`}
            </button>
          )}
        </div>
        {authorMsg && (
          <div className="mt-2 text-xs text-emerald-400">{authorMsg}</div>
        )}
        {preview && (
          <div className="mt-3 rounded bg-gray-950/60 border border-gray-800 p-2 max-h-64 overflow-y-auto">
            <div className="text-xs text-gray-400 mb-2">
              Top 10 of {preview.formatted_results.length} candidates:
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="py-1 pr-2 w-8">#</th>
                  <th className="py-1 pr-2">Code</th>
                  <th className="py-1 pr-2">Description</th>
                  <th className="py-1 pr-2 text-right w-20">RRF score</th>
                </tr>
              </thead>
              <tbody>
                {preview.formatted_results.slice(0, 10).map((r: PreviewCandidate, i: number) => (
                  <tr key={`${r.commodity_code}-${i}`} className="border-b border-gray-900/70">
                    <td className="py-1 pr-2 font-mono text-gray-600">{i + 1}</td>
                    <td className="py-1 pr-2 font-mono text-emerald-300">{r.commodity_code}</td>
                    <td className="py-1 pr-2 text-gray-300 truncate max-w-[520px]">{r.description}</td>
                    <td className="py-1 pr-2 text-right font-mono text-gray-500">{r.score.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">
          Test Prompts ({selected.length}/{prompts.length} selected)
        </h2>
        <button
          onClick={toggleAll}
          className="px-3 py-1.5 text-sm bg-gray-800 hover:bg-gray-700 rounded"
        >
          {selected.length === prompts.length ? "Deselect All" : "Select All"}
        </button>
      </div>

      {/* OpenSearch Results Limit */}
      <div className="bg-gray-900 rounded-lg p-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-medium">OpenSearch Results per Prompt</h3>
            <p className="text-xs text-gray-400 mt-1">
              How many of the 80 search results to include in the LLM prompt.
              Fewer = faster/cheaper. More = better context.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {LIMIT_PRESETS.map((n) => (
              <button
                key={n}
                onClick={() => onOpensearchLimitChange(n)}
                className={`px-3 py-1.5 text-sm rounded transition-colors ${
                  opensearchLimit === n
                    ? "bg-blue-600 text-white"
                    : "bg-gray-800 hover:bg-gray-700 text-gray-300"
                }`}
              >
                {n}
              </button>
            ))}
            <input
              type="number"
              min={1}
              max={80}
              value={opensearchLimit}
              onChange={(e) => onOpensearchLimitChange(Math.min(80, Math.max(1, parseInt(e.target.value) || 10)))}
              className="w-16 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-center focus:outline-none focus:border-blue-500"
            />
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Prompt list */}
        <div className="space-y-2 max-h-[600px] overflow-y-auto">
          {prompts.map((p) => (
            <div
              key={p.index}
              className={`bg-gray-900 rounded-lg p-3 flex items-center gap-3 cursor-pointer border transition-colors ${
                selected.includes(p.index)
                  ? "border-blue-500"
                  : "border-gray-800"
              }`}
            >
              <input
                type="checkbox"
                checked={selected.includes(p.index)}
                onChange={() => toggle(p.index)}
                className="h-4 w-4 shrink-0"
              />
              <div className="flex-1 min-w-0" onClick={() => showDetail(p.index)}>
                <div className="text-sm font-medium truncate flex items-center gap-2">
                  <span className="truncate">#{p.index}: {p.raw_query}</span>
                  {p.gold_code && (
                    <span
                      className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-yellow-900/60 text-yellow-300 font-mono"
                      title={`Ground-truth code: ${p.gold_code}`}
                    >
                      gold {p.gold_code}
                    </span>
                  )}
                  {p.has_oracle_text && (
                    <span
                      className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-purple-900/60 text-purple-300"
                      title="Has authoritative oracle text - simulator will use it as ground truth for unknown facts."
                    >
                      oracle
                    </span>
                  )}
                  {(p.gold_facts_count ?? 0) > 0 && (
                    <span
                      className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/60 text-emerald-300"
                      title="Pre-seeded gold facts - the simulator starts with these committed."
                    >
                      {p.gold_facts_count} facts
                    </span>
                  )}
                  {p.source && (
                    <span
                      className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono"
                      title={`Source: ${p.source}`}
                    >
                      {p.source}
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-400">
                  {p.result_count} search results
                </div>
              </div>
            </div>
          ))}
        </div>

        {/* Detail panel */}
        <div className="bg-gray-900 rounded-lg p-4">
          {detail ? (
            <>
              <h3 className="font-medium mb-2">
                #{(detail as { index: number }).index}:{" "}
                {(detail as { raw_query: string }).raw_query}
              </h3>
              <div className="text-sm text-gray-400 mb-3">
                Expanded: {(detail as { processed_query: string }).processed_query}
              </div>
              {(() => {
                const d = detail as {
                  gold_code?: string | null;
                  oracle_text?: string | null;
                  gold_facts?: GoldFact[];
                  source?: string | null;
                };
                const facts = d.gold_facts || [];
                if (!d.gold_code && !d.oracle_text && facts.length === 0) return null;
                return (
                  <div className="mb-3 rounded border border-yellow-900/60 bg-yellow-950/20 p-2 space-y-2">
                    <div className="text-xs font-medium text-yellow-300">
                      Ground truth
                      {d.source && (
                        <span className="ml-2 font-mono text-[10px] text-yellow-500/80">{d.source}</span>
                      )}
                    </div>
                    {d.gold_code && (
                      <div className="text-xs">
                        <span className="text-gray-500">Gold code: </span>
                        <span className="font-mono text-yellow-300">{d.gold_code}</span>
                      </div>
                    )}
                    {facts.length > 0 && (
                      <div>
                        <div className="text-xs text-gray-500 mb-1">Pre-seeded facts ({facts.length}):</div>
                        <div className="space-y-1">
                          {facts.map((f, i) => (
                            <div key={i} className="text-xs flex gap-2 items-start">
                              <span className="font-mono text-emerald-400 shrink-0">{f.slot}</span>
                              <span className="text-gray-500">=</span>
                              <span className="text-gray-300">{f.answer}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {d.oracle_text && (
                      <details>
                        <summary className="text-xs text-purple-300 cursor-pointer hover:text-purple-200">
                          Oracle text ({d.oracle_text.length} chars)
                        </summary>
                        <div className="mt-1 text-xs text-gray-400 whitespace-pre-wrap max-h-48 overflow-y-auto bg-gray-950/40 p-2 rounded">
                          {d.oracle_text}
                        </div>
                      </details>
                    )}
                  </div>
                );
              })()}
              <div className="flex items-center justify-between mb-2">
                <h4 className="text-sm font-medium">
                  All {allResults.length} OpenSearch Results
                </h4>
                <span className="text-xs text-gray-500">
                  Sending top {opensearchLimit} to LLM
                </span>
              </div>
              <div className="space-y-1 text-xs max-h-[500px] overflow-y-auto">
                {allResults.map(
                  (r: { commodity_code: string; description: string; score: number }, i: number) => (
                    <div
                      key={i}
                      className={`rounded px-2 py-1 flex justify-between ${
                        i < opensearchLimit
                          ? "bg-gray-800"
                          : "bg-gray-800/40 text-gray-600"
                      }`}
                    >
                      <span className="text-gray-500 w-6 shrink-0">{i + 1}.</span>
                      <span className="font-mono">{r.commodity_code}</span>
                      <span className={`truncate ml-2 flex-1 ${i < opensearchLimit ? "text-gray-400" : "text-gray-600"}`}>
                        {r.description.replace(/<br>/g, " ").slice(0, 80)}
                      </span>
                      <span className="text-gray-500 ml-2">
                        {r.score.toFixed(3)}
                      </span>
                    </div>
                  ),
                )}
              </div>
            </>
          ) : (
            <div className="text-gray-500 text-sm">
              Click a prompt to see details
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
