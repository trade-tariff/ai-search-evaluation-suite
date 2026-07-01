import { useState } from "react";

type KgIncludes = {
  chapter_notes: boolean;
  section_notes: boolean;
  legacy_blob_notes: boolean;
  girs: boolean;
  atar_rationales: boolean;
  heading_rules: boolean;
  other_global: boolean;
};

type RetrievalCfg = {
  use_facts_leg: boolean;
  use_kg_context_leg: boolean;
  use_facts_vec_leg: boolean;
  use_kg_vec_leg: boolean;
  facts_cap: number;
  kg_cap: number;
  facts_vec_cap: number;
  kg_vec_cap: number;
};

interface PanelConfig {
  use_query_expansion: boolean;
  use_facets: boolean;
  kg_include: KgIncludes;
  retrieval: RetrievalCfg;
}

const ALL_OFF: KgIncludes = {
  chapter_notes: false, section_notes: false, legacy_blob_notes: false,
  girs: false, atar_rationales: false, heading_rules: false, other_global: false,
};
const ALL_ON: KgIncludes = {
  chapter_notes: true, section_notes: true, legacy_blob_notes: false,
  girs: true, atar_rationales: true, heading_rules: true, other_global: true,
};

const R_BASE: RetrievalCfg = {
  use_facts_leg: false, use_kg_context_leg: false,
  use_facts_vec_leg: false, use_kg_vec_leg: false,
  facts_cap: 0.5, kg_cap: 0.5, facts_vec_cap: 0.6, kg_vec_cap: 0.6,
};
const R_OFF = R_BASE;
const R_FACTS: RetrievalCfg = { ...R_BASE, use_facts_leg: true, use_facts_vec_leg: true };
const R_KG: RetrievalCfg = { ...R_BASE, use_kg_context_leg: true, use_kg_vec_leg: true };
const R_BOTH: RetrievalCfg = { ...R_BASE, use_facts_leg: true, use_kg_context_leg: true, use_facts_vec_leg: true, use_kg_vec_leg: true };

const PRESETS: { label: string; config: PanelConfig }[] = [
  { label: "Baseline (just AI Guided Search)", config: { use_query_expansion: false, use_facets: false, kg_include: { ...ALL_OFF }, retrieval: { ...R_OFF } } },
  { label: "+ Facts only", config: { use_query_expansion: false, use_facets: true, kg_include: { ...ALL_OFF }, retrieval: { ...R_FACTS } } },
  { label: "+ KG only (notes, GIRs, ATARs)", config: { use_query_expansion: false, use_facets: false, kg_include: { ...ALL_ON }, retrieval: { ...R_KG } } },
  { label: "+ Everything (facts + KG + expansion)", config: { use_query_expansion: true, use_facets: true, kg_include: { ...ALL_ON }, retrieval: { ...R_BOTH } } },
];

interface PanelResult {
  label: string;
  config: PanelConfig;
  turn: any;
}

export default function CompareTab() {
  const [query, setQuery] = useState("flip flops");
  const [panels, setPanels] = useState(PRESETS);
  const [results, setResults] = useState<PanelResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    if (!query.trim()) return;
    setLoading(true); setErr(null); setResults(null);
    try {
      const r = await fetch("/api/classify/compare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query.trim(), panels }),
      });
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      setResults(d.panels);
    } catch (e: any) { setErr(e.message ?? String(e)); }
    finally { setLoading(false); }
  }

  function updateKg(i: number, key: keyof KgIncludes, value: boolean) {
    setPanels(ps => ps.map((p, idx) => idx === i ? { ...p, config: { ...p.config, kg_include: { ...p.config.kg_include, [key]: value } } } : p));
  }
  function updateFlag(i: number, key: "use_facets" | "use_query_expansion", value: boolean) {
    setPanels(ps => ps.map((p, idx) => idx === i ? { ...p, config: { ...p.config, [key]: value } } : p));
  }
  function updateRetrieval(i: number, key: keyof RetrievalCfg, value: boolean | number) {
    setPanels(ps => ps.map((p, idx) => idx === i ? { ...p, config: { ...p.config, retrieval: { ...p.config.retrieval, [key]: value } } } : p));
  }

  return (
    <div className="space-y-4">
      <div className="bg-gray-900 border border-gray-800 p-4 rounded">
        <p className="text-sm text-gray-400 mb-3">
          Same query through four side-by-side AI Guided Search configs. Watch what changes when you flip
          structured facts, KG edges, decomposed chapter/section notes, GIRs, ATAR rationales and
          query expansion on and off. The LLM call is the same gpt-5.5 for all four; the only thing that
          changes is the augmentation context. Sequential execution: 4 × ~13s ≈ 50-60s total.
        </p>
        <div className="flex gap-3 items-end">
          <label className="flex-1">
            <div className="text-xs uppercase tracking-wider text-gray-500 mb-1">Query</div>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="flip flops, office chair, red wine, kitchen knife, iphone charger..."
              className="w-full bg-gray-800 border border-gray-700 px-3 py-2 text-sm rounded text-white"
            />
          </label>
          <button onClick={run} disabled={loading || !query.trim()} className="bg-emerald-600 hover:bg-emerald-700 disabled:bg-gray-700 text-white px-4 py-2 text-sm rounded">
            {loading ? "Running 4 panels..." : "Run comparison"}
          </button>
        </div>
      </div>

      {err && <div className="bg-red-900/30 border border-red-800 p-3 rounded text-red-300 text-sm">{err}</div>}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {panels.map((p, i) => (
          <PanelCard
            key={i}
            label={p.label}
            config={p.config}
            result={results?.[i]}
            onFlag={(k: "use_facets" | "use_query_expansion", v: boolean) => updateFlag(i, k, v)}
            onKg={(k: keyof KgIncludes, v: boolean) => updateKg(i, k, v)}
            onRetrieval={(k: keyof RetrievalCfg, v: boolean | number) => updateRetrieval(i, k, v)}
          />
        ))}
      </div>
    </div>
  );
}

function PanelCard({ label, config, result, onFlag, onKg, onRetrieval }: any) {
  const a = result?.turn?.augmentation_summary;
  const debug = a?.debug || {};
  const expanded = debug.expanded_query;
  const mode = result?.turn?.mode;
  const legs: string[] = debug.retrieval_legs || [];
  return (
    <div className="bg-gray-900 border border-gray-800 p-3 rounded">
      <div className="flex justify-between items-baseline mb-2">
        <h3 className="font-bold text-sm">{label}</h3>
        {mode && <ModeBadge mode={mode} />}
      </div>

      <details className="text-xs mb-2">
        <summary className="cursor-pointer text-gray-400 underline">Per-edge-type toggles</summary>
        <div className="grid grid-cols-2 gap-1 bg-gray-800 p-2 mt-1 rounded text-gray-300">
          <Toggle label="Query expansion (AI-441)" value={config.use_query_expansion} onChange={(v: boolean) => onFlag("use_query_expansion", v)} />
          <Toggle label="Facts in prompt (STRUCTURED_FACTS)" value={config.use_facets} onChange={(v: boolean) => onFlag("use_facets", v)} />
          <Toggle label="Chapter notes (decomposed)" value={config.kg_include.chapter_notes} onChange={(v: boolean) => onKg("chapter_notes", v)} />
          <Toggle label="Section notes (decomposed)" value={config.kg_include.section_notes} onChange={(v: boolean) => onKg("section_notes", v)} />
          <Toggle label="GIRs 1-6" value={config.kg_include.girs} onChange={(v: boolean) => onKg("girs", v)} />
          <Toggle label="ATAR rationales" value={config.kg_include.atar_rationales} onChange={(v: boolean) => onKg("atar_rationales", v)} />
          <Toggle label="Heading rules" value={config.kg_include.heading_rules} onChange={(v: boolean) => onKg("heading_rules", v)} />
          <Toggle label="Other global" value={config.kg_include.other_global} onChange={(v: boolean) => onKg("other_global", v)} />
          <Toggle label="Legacy blob notes" value={config.kg_include.legacy_blob_notes} onChange={(v: boolean) => onKg("legacy_blob_notes", v)} />
        </div>
        <div className="grid grid-cols-2 gap-1 bg-gray-800 p-2 mt-1 rounded text-gray-300 border-t border-gray-700">
          <div className="col-span-2 text-[10px] uppercase tracking-wider text-emerald-400">Retrieval legs (boost candidates)</div>
          <Toggle label={`Facts FTS (cap ${config.retrieval.facts_cap})`} value={config.retrieval.use_facts_leg} onChange={(v: boolean) => onRetrieval("use_facts_leg", v)} />
          <Toggle label={`KG FTS (cap ${config.retrieval.kg_cap})`} value={config.retrieval.use_kg_context_leg} onChange={(v: boolean) => onRetrieval("use_kg_context_leg", v)} />
          <Toggle label={`Facts semantic (cap ${config.retrieval.facts_vec_cap})`} value={config.retrieval.use_facts_vec_leg} onChange={(v: boolean) => onRetrieval("use_facts_vec_leg", v)} />
          <Toggle label={`KG semantic (cap ${config.retrieval.kg_vec_cap})`} value={config.retrieval.use_kg_vec_leg} onChange={(v: boolean) => onRetrieval("use_kg_vec_leg", v)} />
        </div>
      </details>

      {result ? (
        <>
          <div className="grid grid-cols-3 gap-2 text-xs mb-2">
            <Stat label="Facts" value={`${a?.candidates_with_facets ?? 0}/${a?.total_candidates ?? 0}`} />
            <Stat label="KG edges" value={`${a?.kg_edges_applied ?? 0}`} />
            <Stat label="Prompt" value={`${(debug.prompt_chars ?? 0).toLocaleString()}ch`} />
          </div>
          <div className="text-xs text-gray-500 mb-2">
            Latency: {debug.latency_ms ?? "?"}ms
            {legs.length > 0 && (
              <> · legs: {legs.map((l) => (
                <span key={l} className={`mr-1 ${l === "facts" || l === "kg_context" ? "text-emerald-400" : "text-gray-400"}`}>{l}</span>
              ))}</>
            )}
            {expanded && <> · <em className="text-amber-400">expanded: "{expanded}"</em></>}
          </div>
          {mode === "questions" && result.turn.question && (
            <div className="bg-yellow-900/20 border border-yellow-800/40 p-2 rounded mb-2 text-xs">
              <div className="font-bold text-yellow-300 mb-1">Q: {result.turn.question.question}</div>
              <ul className="text-gray-400 list-disc list-inside">
                {result.turn.question.options.slice(0, 4).map((o: string, i: number) => <li key={i}>{o}</li>)}
                {result.turn.question.options.length > 4 && <li>...+{result.turn.question.options.length - 4} more</li>}
              </ul>
            </div>
          )}
          {mode === "answers" && result.turn.answers?.length > 0 && (
            <div>
              <div className="text-xs uppercase tracking-wider text-gray-500 mb-1">Top suggestions</div>
              <ul className="text-xs space-y-1">
                {result.turn.answers.slice(0, 5).map((ans: any, i: number) => (
                  <li key={i} className={`flex justify-between gap-2 border-b border-gray-800 pb-1 ${i === 0 ? "font-semibold" : ""}`}>
                    <span className="font-mono text-emerald-400">{ans.commodity_code}</span>
                    <span className="flex-1 text-gray-400 truncate">{(ans.description || "").slice(0, 50)}</span>
                    <ConfBadge level={ans.confidence} />
                  </li>
                ))}
              </ul>
            </div>
          )}
          {mode === "error" && <div className="text-xs text-red-400">{result.turn.error_message}</div>}
        </>
      ) : (
        <div className="text-xs text-gray-500 italic py-4 text-center">Click "Run comparison" to populate.</div>
      )}
    </div>
  );
}

function ModeBadge({ mode }: { mode: string }) {
  const cls = mode === "answers" ? "bg-emerald-700 text-white"
    : mode === "questions" ? "bg-yellow-700 text-yellow-100"
    : "bg-gray-700 text-gray-300";
  return <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${cls}`}>{mode.toUpperCase()}</span>;
}

function ConfBadge({ level }: { level: string }) {
  const cls = level === "Strong" ? "text-emerald-400 font-bold"
    : level === "Good" ? "text-blue-400"
    : "text-gray-500";
  return <span className={`text-[10px] ${cls}`}>{level}</span>;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-800 border border-gray-700 p-1.5 text-center rounded">
      <div className="text-[10px] text-gray-500 uppercase">{label}</div>
      <div className="font-bold text-sm">{value}</div>
    </div>
  );
}

function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-1 cursor-pointer hover:bg-gray-900 p-0.5 rounded">
      <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} className="h-3 w-3" />
      <span className="text-[11px]">{label}</span>
    </label>
  );
}
