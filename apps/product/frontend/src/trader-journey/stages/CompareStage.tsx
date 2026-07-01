import { useState } from "react";
import { api } from "../api";
import type { ClassifyTurn } from "../types";

// Per-edge-type config the comparison panels can independently toggle.
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
  chapter_notes: false,
  section_notes: false,
  legacy_blob_notes: false,
  girs: false,
  atar_rationales: false,
  heading_rules: false,
  other_global: false,
};
const ALL_ON: KgIncludes = {
  chapter_notes: true,
  section_notes: true,
  legacy_blob_notes: false,
  girs: true,
  atar_rationales: true,
  heading_rules: true,
  other_global: true,
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
  {
    label: "Basic search",
    config: { use_query_expansion: false, use_facets: false, kg_include: { ...ALL_OFF }, retrieval: { ...R_OFF } },
  },
  {
    label: "+ Product facts",
    config: { use_query_expansion: false, use_facets: true, kg_include: { ...ALL_OFF }, retrieval: { ...R_FACTS } },
  },
  {
    label: "+ Tariff rules and rulings",
    config: { use_query_expansion: false, use_facets: false, kg_include: { ...ALL_ON }, retrieval: { ...R_KG } },
  },
  {
    label: "+ Everything together",
    config: { use_query_expansion: true, use_facets: true, kg_include: { ...ALL_ON }, retrieval: { ...R_BOTH } },
  },
];

interface PanelResult {
  label: string;
  config: PanelConfig;
  turn: ClassifyTurn;
}

export default function CompareStage() {
  const [query, setQuery] = useState("flip flops");
  const [panels, setPanels] = useState<{ label: string; config: PanelConfig }[]>(PRESETS);
  const [results, setResults] = useState<PanelResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    setResults(null);
    try {
      const r = await api.classifyCompare(query.trim(), panels);
      setResults(r.panels as PanelResult[]);
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setLoading(false);
    }
  }

  function updatePanel(i: number, patch: Partial<PanelConfig>) {
    setPanels((ps) => ps.map((p, idx) => (idx === i ? { ...p, config: { ...p.config, ...patch } } : p)));
  }
  function updateKg(i: number, key: keyof KgIncludes, value: boolean) {
    setPanels((ps) =>
      ps.map((p, idx) =>
        idx === i ? { ...p, config: { ...p.config, kg_include: { ...p.config.kg_include, [key]: value } } } : p
      )
    );
  }
  function updateRetrieval(i: number, key: keyof RetrievalCfg, value: boolean | number) {
    setPanels((ps) =>
      ps.map((p, idx) =>
        idx === i ? { ...p, config: { ...p.config, retrieval: { ...p.config.retrieval, [key]: value } } } : p
      )
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold mb-2">Compare search set-ups</h2>
        <p className="text-gray-400 mb-4">
          The same description, run through four different set-ups side by side, so you
          can see how adding extra tariff data changes the suggested codes. All four
          panels use the same AI model, so any difference comes from the data we add.
        </p>
      </div>

      <div className="tj-card">
        <label className="tj-label" htmlFor="cmpq">Query</label>
        <div className="flex gap-3">
          <input
            id="cmpq"
            className="tj-input flex-1"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g. flip flops, office chair, red wine, kitchen knife"
          />
          <button onClick={run} className="tj-btn" disabled={loading || !query.trim()}>
            {loading ? "Running 4 panels..." : "Run comparison"}
          </button>
        </div>
        <p className="text-xs text-gray-400 mt-2">
          Each panel below has its own per-edge-type toggles. Defaults match the labelled preset.
        </p>
      </div>

      {error && (
        <div className="border-l-4 border-red-500 bg-gray-900 p-4">
          <strong>Error:</strong> {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {panels.map((p, i) => (
          <PanelCard
            key={i}
            label={p.label}
            config={p.config}
            result={results?.[i]}
            onUseFacets={(v) => updatePanel(i, { use_facets: v })}
            onUseExpansion={(v) => updatePanel(i, { use_query_expansion: v })}
            onKgToggle={(k, v) => updateKg(i, k, v)}
            onRetrievalToggle={(k, v) => updateRetrieval(i, k, v)}
          />
        ))}
      </div>
    </div>
  );
}

function PanelCard({
  label, config, result, onUseFacets, onUseExpansion, onKgToggle, onRetrievalToggle,
}: {
  label: string;
  config: PanelConfig;
  result?: PanelResult;
  onUseFacets: (v: boolean) => void;
  onUseExpansion: (v: boolean) => void;
  onKgToggle: (k: keyof KgIncludes, v: boolean) => void;
  onRetrievalToggle: (k: keyof RetrievalCfg, v: boolean | number) => void;
}) {
  const a = result?.turn.augmentation_summary;
  const debug = (a as any)?.debug || {};
  const expanded = debug.expanded_query;
  const mode = result?.turn.mode;
  const legs: string[] = debug.retrieval_legs || [];
  return (
    <div className="tj-card">
      <div className="flex justify-between items-baseline mb-2">
        <h3 className="font-bold">{label}</h3>
        {mode && <ModeBadge mode={mode} />}
      </div>

      <details className="text-xs mb-3">
        <summary className="cursor-pointer text-gray-400 underline">Toggles</summary>
        <div className="mt-2 grid grid-cols-2 gap-1 bg-gray-900 p-2">
          <Toggle label="Query expansion (AI-441)" value={config.use_query_expansion} onChange={onUseExpansion} />
          <Toggle label="Facts in prompt (STRUCTURED_FACTS)" value={config.use_facets} onChange={onUseFacets} />
          <Toggle label="Chapter notes (decomposed)" value={config.kg_include.chapter_notes} onChange={(v) => onKgToggle("chapter_notes", v)} />
          <Toggle label="Section notes (decomposed)" value={config.kg_include.section_notes} onChange={(v) => onKgToggle("section_notes", v)} />
          <Toggle label="GIRs 1-6" value={config.kg_include.girs} onChange={(v) => onKgToggle("girs", v)} />
          <Toggle label="ATAR rationales" value={config.kg_include.atar_rationales} onChange={(v) => onKgToggle("atar_rationales", v)} />
          <Toggle label="Heading rules" value={config.kg_include.heading_rules} onChange={(v) => onKgToggle("heading_rules", v)} />
          <Toggle label="Other global rules" value={config.kg_include.other_global} onChange={(v) => onKgToggle("other_global", v)} />
          <Toggle label="Legacy blob notes" value={config.kg_include.legacy_blob_notes} onChange={(v) => onKgToggle("legacy_blob_notes", v)} />
        </div>
        <div className="mt-1 grid grid-cols-2 gap-1 bg-gray-900 p-2 border-t-2 border-emerald-600">
          <div className="col-span-2 text-[10px] uppercase tracking-wider text-emerald-400 font-bold">Retrieval legs (boost candidates from facts/KG)</div>
          <Toggle label={`Facts FTS (cap ${config.retrieval.facts_cap})`} value={config.retrieval.use_facts_leg} onChange={(v) => onRetrievalToggle("use_facts_leg", v)} />
          <Toggle label={`KG FTS (cap ${config.retrieval.kg_cap})`} value={config.retrieval.use_kg_context_leg} onChange={(v) => onRetrievalToggle("use_kg_context_leg", v)} />
          <Toggle label={`Facts semantic (cap ${config.retrieval.facts_vec_cap})`} value={config.retrieval.use_facts_vec_leg} onChange={(v) => onRetrievalToggle("use_facts_vec_leg", v)} />
          <Toggle label={`KG semantic (cap ${config.retrieval.kg_vec_cap})`} value={config.retrieval.use_kg_vec_leg} onChange={(v) => onRetrievalToggle("use_kg_vec_leg", v)} />
        </div>
      </details>

      {result ? (
        <>
          <details className="mb-3">
            <summary className="cursor-pointer text-xs text-gray-400 underline">Run details</summary>
            <div className="mt-2 grid grid-cols-3 gap-2 text-xs mb-3">
              <Stat label="Codes with facts" value={`${a?.candidates_with_facets ?? 0}/${a?.total_candidates ?? 0}`} />
              <Stat label="KG edges" value={`${a?.kg_edges_applied ?? 0}`} />
              <Stat label="Prompt" value={`${(debug.prompt_chars ?? 0).toLocaleString()}ch`} />
            </div>
            <div className="text-xs text-gray-400 mb-2">
              Latency: {debug.latency_ms ?? "?"}ms
              {legs.length > 0 && (
                <>
                  {" · legs: "}
                  {legs.map((l) => (
                    <span key={l} className={`mr-1 ${l === "facts" || l === "kg_context" ? "text-emerald-400 font-semibold" : "text-gray-400"}`}>{l}</span>
                  ))}
                </>
              )}
              {expanded && expanded !== result?.turn.qa_history && (
                <>
                  {" · expanded query: "}
                  <em>{expanded}</em>
                </>
              )}
            </div>
          </details>

          {mode === "questions" && result.turn.question && (
            <div className="bg-gray-900 p-3 mb-2 text-sm">
              <div className="text-xs text-gray-400 font-bold uppercase tracking-widest mb-1">Would ask you</div>
              <div className="font-semibold">{result.turn.question.question}</div>
              <ul className="text-xs mt-1 text-gray-400">
                {result.turn.question.options.slice(0, 4).map((o, i) => <li key={i}>· {o}</li>)}
                {result.turn.question.options.length > 4 && <li>· ...and {result.turn.question.options.length - 4} more</li>}
              </ul>
            </div>
          )}

          {mode === "answers" && result.turn.answers.length > 0 && (
            <div>
              <div className="text-xs text-gray-400 font-bold uppercase tracking-widest mb-1">Top suggestions</div>
              <ul className="text-sm space-y-1">
                {result.turn.answers.slice(0, 5).map((ans, i) => (
                  <li key={i} className={`flex justify-between gap-2 border-b border-gray-800 pb-1 ${i === 0 ? "font-semibold" : ""}`}>
                    <span className="font-mono text-blue-400">{ans.commodity_code}</span>
                    <span className="flex-1 text-xs">{ans.description.length > 60 ? ans.description.slice(0, 60) + "..." : ans.description}</span>
                    <span className={`text-xs ${ans.confidence === "Strong" ? "text-emerald-400 font-bold" : "text-gray-400"}`}>{ans.confidence}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {mode === "error" && (
            <div className="text-xs text-red-400">{result.turn.error_message}</div>
          )}
        </>
      ) : (
        <div className="text-sm text-gray-400 italic">Run comparison to populate this panel.</div>
      )}
    </div>
  );
}

function ModeBadge({ mode }: { mode: string }) {
  const cls = mode === "answers"
    ? "bg-emerald-600 text-white"
    : mode === "questions" ? "bg-amber-500/20 text-gray-100"
    : "bg-gray-700 text-gray-100";
  const text = mode === "answers" ? "MATCHES FOUND" : mode === "questions" ? "NEEDS AN ANSWER" : "NO RESULT";
  return <span className={`text-xs font-bold px-2 py-0.5 ${cls}`}>{text}</span>;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-gray-700 p-1.5 text-center">
      <div className="text-[10px] text-gray-400 uppercase">{label}</div>
      <div className="font-bold text-sm">{value}</div>
    </div>
  );
}

function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-1 cursor-pointer hover:bg-gray-900 p-0.5 rounded">
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
        className="h-3 w-3"
      />
      <span className="text-xs">{label}</span>
    </label>
  );
}
