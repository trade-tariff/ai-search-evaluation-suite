import { useEffect, useState } from "react";
import KnowledgeGraphView from "./KnowledgeGraphView";

type SubView = "coverage" | "facets" | "edges" | "commodity" | "graph";

interface CoverageStats {
  totals: {
    facet_defs: number;
    facets: number;
    codes_with_facets: number;
    edges: number;
    edge_links: number;
  };
  per_chapter: { chapter: string; codes_with_facets: number; total_facets: number; distinct_sources: number }[];
  per_source: { source_bucket: string; n: number }[];
  per_scope: { scope: string; n: number }[];
  per_use_scope: { kind: "fact" | "edge"; use_scope: string; n: number }[];
  per_evidence_role?: { kind: "fact" | "edge"; evidence_role: string; n: number }[];
}

interface FacetRow {
  id: number;
  commodity_code: string;
  facet_key: string;
  facet_value: string;
  source: string;
  confidence: number;
  evidence: string | null;
  facet_label: string | null;
  created_at: string;
  authority_tier?: number;
  use_scopes?: string[];
  evidence_roles?: string[];
  provenance?: Record<string, any> | null;
}

const TIER_INFO: Record<number, { label: string; color: string; description: string }> = {
  1: { label: "T1 Binding", color: "bg-red-700 text-white", description: "Binding legal rule (GIRs, chapter/section notes)" },
  2: { label: "T2 Ruling", color: "bg-orange-700 text-white", description: "Binding ruling (ATARs)" },
  3: { label: "T3 Guidance", color: "bg-amber-700 text-amber-50", description: "Authoritative interpretive guidance" },
  4: { label: "T4 Expert", color: "bg-emerald-700 text-white", description: "Curated expert classification (Search References, hand)" },
  5: { label: "T5 AI-Auth", color: "bg-blue-700 text-white", description: "AI-derived from authoritative source" },
  6: { label: "T6 AI-Desc", color: "bg-blue-900 text-blue-100", description: "AI-derived from descriptive text" },
  7: { label: "T7 Meta", color: "bg-gray-700 text-gray-200", description: "Footnotes / measure-attached metadata" },
  8: { label: "T8 Ext", color: "bg-gray-800 text-gray-400", description: "External / unverified" },
};

interface EdgeRow {
  id: string;
  type: string;
  scope: string;
  title: string;
  body: string;
  source: string;
  n_linked_codes: number;
  created_at: string;
  authority_tier?: number;
  use_scopes?: string[];
  evidence_roles?: string[];
  provenance?: Record<string, any> | null;
}

function TierBadge({ tier }: { tier?: number }) {
  const info = TIER_INFO[tier ?? 8] ?? TIER_INFO[8];
  return (
    <span
      className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${info.color}`}
      title={info.description}
    >
      {info.label}
    </span>
  );
}

interface CommodityView {
  code: string;
  description: string | null;
  facets: Array<Partial<FacetRow> & Pick<FacetRow, "id" | "facet_key" | "facet_value" | "source">>;
  edges: Array<Partial<EdgeRow> & Pick<EdgeRow, "id" | "type" | "scope" | "title" | "body" | "source">>;
}

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

export default function KnowledgePanel() {
  const [view, setView] = useState<SubView>("coverage");

  return (
    <div className="space-y-4">
      <div className="flex gap-1 border-b border-gray-800">
        {(["coverage", "facets", "edges", "commodity", "graph"] as SubView[]).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              view === v
                ? "border-emerald-500 text-emerald-400"
                : "border-transparent text-gray-400 hover:text-gray-200"
            }`}
          >
            {v.charAt(0).toUpperCase() + v.slice(1)}
          </button>
        ))}
      </div>
      {view === "coverage" && <CoverageView />}
      {view === "facets" && <FacetsView />}
      {view === "edges" && <EdgesView />}
      {view === "commodity" && <CommodityView />}
      {view === "graph" && <KnowledgeGraphView />}
    </div>
  );
}

// ---------------- Coverage ----------------

function CoverageView() {
  const [stats, setStats] = useState<CoverageStats | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api<CoverageStats>("/api/kg/coverage").then(setStats).catch((e) => setErr(e.message));
  }, []);
  if (err) return <div className="text-red-400">Error: {err}</div>;
  if (!stats) return <div className="text-gray-400">Loading...</div>;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <Stat label="Facet definitions" value={stats.totals.facet_defs} />
        <Stat label="Total facts" value={stats.totals.facets} />
        <Stat label="Codes with facts" value={stats.totals.codes_with_facets} />
        <Stat label="KG edges" value={stats.totals.edges} />
        <Stat label="Explicit edge links" value={stats.totals.edge_links} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 border border-gray-800 p-4 rounded">
          <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400 mb-3">Coverage by chapter</h3>
          <table className="w-full text-sm">
            <thead className="text-gray-500 text-xs">
              <tr>
                <th className="text-left p-1">Chapter</th>
                <th className="text-right p-1">Codes</th>
                <th className="text-right p-1">Facts</th>
                <th className="text-right p-1">Sources</th>
              </tr>
            </thead>
            <tbody>
              {stats.per_chapter.map((r) => (
                <tr key={r.chapter} className="border-t border-gray-800">
                  <td className="p-1 font-mono">{r.chapter}</td>
                  <td className="p-1 text-right">{r.codes_with_facets}</td>
                  <td className="p-1 text-right">{r.total_facets}</td>
                  <td className="p-1 text-right">{r.distinct_sources}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-4">
          <div className="bg-gray-900 border border-gray-800 p-4 rounded">
            <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400 mb-3">Facts by source</h3>
            <ul className="space-y-1 text-sm">
              {stats.per_source.map((r) => (
                <li key={r.source_bucket} className="flex justify-between border-b border-gray-800 pb-1">
                  <span className="text-gray-200">{r.source_bucket}</span>
                  <span className="font-mono">{r.n}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="bg-gray-900 border border-gray-800 p-4 rounded">
            <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400 mb-3">KG edges by scope</h3>
            <ul className="space-y-1 text-sm">
              {stats.per_scope.map((r) => (
                <li key={r.scope} className="flex justify-between border-b border-gray-800 pb-1">
                  <span className="font-mono text-gray-200">{r.scope}</span>
                  <span className="font-mono">{r.n}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="bg-gray-900 border border-gray-800 p-4 rounded">
            <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400 mb-3">Use labels</h3>
            <ul className="space-y-1 text-sm">
              {(stats.per_use_scope || []).map((r) => (
                <li key={`${r.kind}-${r.use_scope}`} className="flex justify-between border-b border-gray-800 pb-1">
                  <span className="text-gray-200">
                    <span className="text-gray-500">{r.kind}</span>{" "}
                    <UseScopeChip scope={r.use_scope} />
                  </span>
                  <span className="font-mono">{r.n}</span>
                </li>
              ))}
              {(stats.per_use_scope || []).length === 0 && (
                <li className="text-gray-500 italic">No use labels recorded yet.</li>
              )}
            </ul>
          </div>
          <div className="bg-gray-900 border border-gray-800 p-4 rounded">
            <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400 mb-3">Evidence roles</h3>
            <ul className="space-y-1 text-sm">
              {(stats.per_evidence_role || []).map((r) => (
                <li key={`${r.kind}-${r.evidence_role}`} className="flex justify-between border-b border-gray-800 pb-1">
                  <span className="text-gray-200">
                    <span className="text-gray-500">{r.kind}</span>{" "}
                    <EvidenceRoleChip role={r.evidence_role} />
                  </span>
                  <span className="font-mono">{r.n}</span>
                </li>
              ))}
              {(stats.per_evidence_role || []).length === 0 && (
                <li className="text-gray-500 italic">No evidence roles recorded yet.</li>
              )}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------- Facets ----------------

function FacetsView() {
  const [chapter, setChapter] = useState("");
  const [source, setSource] = useState("");
  const [useScope, setUseScope] = useState("");
  const [evidenceRole, setEvidenceRole] = useState("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<{ total: number; rows: FacetRow[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [definitions, setDefinitions] = useState<{ key: string; label: string; uses: number }[]>([]);
  const [editing, setEditing] = useState<number | null>(null);
  const [editValue, setEditValue] = useState("");
  const [auditFor, setAuditFor] = useState<number | null>(null);
  const [auditEntries, setAuditEntries] = useState<any[]>([]);

  async function viewAudit(id: number) {
    setAuditFor(id);
    const r = await fetch(`/api/kg/audit/commodity_facets/${id}`);
    if (!r.ok) {
      setAuditEntries([]);
      return;
    }
    const entries = await r.json();
    setAuditEntries(Array.isArray(entries) ? entries : []);
  }

  async function refresh() {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (chapter) params.set("chapter", chapter);
      if (source) params.set("source", source);
      if (useScope) params.set("use_scope", useScope);
      if (evidenceRole) params.set("evidence_role", evidenceRole);
      if (q) params.set("q", q);
      params.set("limit", "200");
      const d = await api<{ total: number; rows: FacetRow[] }>(`/api/kg/facets?${params}`);
      setData(d);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    refresh();
    api<{ key: string; label: string; uses: number }[]>("/api/kg/facet_definitions").then(setDefinitions).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function deleteFacet(id: number) {
    if (!confirm("Delete this fact?")) return;
    await fetch(`/api/kg/facets/${id}`, { method: "DELETE" });
    refresh();
  }

  async function promoteFacet(id: number) {
    await fetch(`/api/kg/facets/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "verified", confidence: 1.0 }),
    });
    refresh();
  }

  async function saveEdit(id: number) {
    await fetch(`/api/kg/facets/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ facet_value: editValue }),
    });
    setEditing(null);
    refresh();
  }

  return (
    <div className="space-y-4">
      <div className="bg-gray-900 border border-gray-800 p-4 rounded flex flex-wrap gap-3 items-end">
        <Input label="Chapter" value={chapter} onChange={setChapter} placeholder="64" width="w-20" />
        <Select label="Source" value={source} onChange={setSource} options={[
          { value: "", label: "all" },
          { value: "hand", label: "hand" },
          { value: "description_llm", label: "description_llm" },
          { value: "atar", label: "atar" },
          { value: "verified", label: "verified" },
        ]} />
        <Select label="Use label" value={useScope} onChange={setUseScope} options={USE_SCOPE_OPTIONS} />
        <Select label="Evidence role" value={evidenceRole} onChange={setEvidenceRole} options={EVIDENCE_ROLE_OPTIONS} />
        <Input label="Search" value={q} onChange={setQ} placeholder="code, key, or value" width="w-64" />
        <button onClick={refresh} className="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 text-sm rounded">
          {loading ? "..." : "Apply"}
        </button>
        {data && <span className="text-xs text-gray-400 ml-2">Showing {data.rows.length} of {data.total}</span>}
      </div>

      <details className="bg-gray-900 border border-gray-800 p-4 rounded">
        <summary className="cursor-pointer text-sm text-gray-400 uppercase tracking-wider font-bold">
          {definitions.length} facet definitions
        </summary>
        <div className="mt-3 grid grid-cols-2 lg:grid-cols-4 gap-2 text-xs">
          {definitions.slice(0, 60).map((d) => (
            <button
              key={d.key}
              onClick={() => { setQ(""); setSource(""); /* filter by facet_key */
                api<{ total: number; rows: FacetRow[] }>(`/api/kg/facets?facet_key=${encodeURIComponent(d.key)}&limit=200`).then(setData);
              }}
              className="bg-gray-800 border border-gray-700 px-2 py-1 text-left hover:bg-gray-700 rounded"
            >
              <span className="font-mono text-emerald-400">{d.key}</span>
              <span className="text-gray-500 ml-1">×{d.uses}</span>
            </button>
          ))}
        </div>
      </details>

      {auditFor !== null && (
        <div className="bg-gray-900 border border-emerald-700 p-3 rounded">
          <div className="flex justify-between items-center mb-2">
            <span className="text-xs text-emerald-400 uppercase tracking-wider font-bold">
              Audit log — facet #{auditFor} ({auditEntries.length} entries)
            </span>
            <button onClick={() => setAuditFor(null)} className="text-xs text-gray-400">close</button>
          </div>
          <ul className="space-y-1 text-xs">
            {auditEntries.map((a) => (
              <li key={a.id} className="border-b border-gray-800 pb-1">
                <span className="text-emerald-300">{a.action.toUpperCase()}</span>
                {" "}by <span className="font-mono">{a.actor}</span>
                {" "}at <span className="text-gray-400">{new Date(a.created_at).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-800 text-gray-400 text-xs">
            <tr>
              <th className="p-2 text-left">Code</th>
              <th className="p-2 text-left">Facet</th>
              <th className="p-2 text-left">Value</th>
              <th className="p-2 text-left">Tier / Source</th>
              <th className="p-2 text-right">Conf.</th>
              <th className="p-2 text-left">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(data?.rows || []).map((f) => (
              <tr key={f.id} className="border-t border-gray-800 hover:bg-gray-800/30">
                <td className="p-2 font-mono text-emerald-300">{f.commodity_code}</td>
                <td className="p-2"><span className="font-mono text-gray-300">{f.facet_key}</span></td>
                <td className="p-2">
                  {editing === f.id ? (
                    <div className="flex gap-1">
                      <input
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        className="bg-gray-800 border border-gray-700 px-2 py-1 text-xs rounded text-white"
                        autoFocus
                      />
                      <button onClick={() => saveEdit(f.id)} className="text-emerald-400 text-xs">save</button>
                      <button onClick={() => setEditing(null)} className="text-gray-500 text-xs">cancel</button>
                    </div>
                  ) : (
                    <span className="font-mono text-gray-200">{f.facet_value}</span>
                  )}
                </td>
                <td className="p-2">
                  <div className="flex flex-wrap items-center gap-1">
                    <TierBadge tier={f.authority_tier} />
                    <SourceBadge source={f.source} />
                    <UseScopeChips scopes={f.use_scopes} />
                    <EvidenceRoleChips roles={f.evidence_roles} />
                  </div>
                </td>
                <td className="p-2 text-right font-mono text-xs text-gray-400">
                  {typeof f.confidence === "number" ? f.confidence.toFixed(2) : "n/a"}
                </td>
                <td className="p-2 text-xs">
                  <button onClick={() => { setEditing(f.id); setEditValue(f.facet_value); }} className="text-blue-400 mr-2">edit</button>
                  {f.source !== "verified" && (
                    <button onClick={() => promoteFacet(f.id)} className="text-emerald-400 mr-2" title="Mark as verified">verify</button>
                  )}
                  <button onClick={() => deleteFacet(f.id)} className="text-red-400 mr-2">delete</button>
                  <button onClick={() => viewAudit(f.id)} className="text-gray-400" title="Audit log">log</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------- Edges ----------------

function EdgesView() {
  const [scope, setScope] = useState("");
  const [useScope, setUseScope] = useState("");
  const [evidenceRole, setEvidenceRole] = useState("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<{ total: number; rows: EdgeRow[] } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  async function refresh() {
    const params = new URLSearchParams();
    if (scope) params.set("scope", scope);
    if (useScope) params.set("use_scope", useScope);
    if (evidenceRole) params.set("evidence_role", evidenceRole);
    if (q) params.set("q", q);
    params.set("limit", "200");
    setData(await api<{ total: number; rows: EdgeRow[] }>(`/api/kg/edges?${params}`));
  }
  useEffect(() => { refresh(); }, []);

  async function deleteEdge(id: string) {
    if (!confirm(`Delete edge ${id}?`)) return;
    await fetch(`/api/kg/edges/${encodeURIComponent(id)}`, { method: "DELETE" });
    refresh();
  }

  return (
    <div className="space-y-4">
      <div className="bg-gray-900 border border-gray-800 p-4 rounded flex flex-wrap gap-3 items-end">
        <Input label="Scope" value={scope} onChange={setScope} placeholder="chapter:64" width="w-40" />
        <Select label="Use label" value={useScope} onChange={setUseScope} options={USE_SCOPE_OPTIONS} />
        <Select label="Evidence role" value={evidenceRole} onChange={setEvidenceRole} options={EVIDENCE_ROLE_OPTIONS} />
        <Input label="Search" value={q} onChange={setQ} placeholder="title or body" width="w-64" />
        <button onClick={refresh} className="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 text-sm rounded">
          Apply
        </button>
        {data && <span className="text-xs text-gray-400 ml-2">Showing {data.rows.length} of {data.total}</span>}
      </div>

      <div className="space-y-2">
        {(data?.rows || []).map((e) => (
          <div key={e.id} className="bg-gray-900 border border-gray-800 p-3 rounded">
            <div className="flex justify-between gap-3 items-start">
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2 flex-wrap">
                  <TierBadge tier={e.authority_tier} />
                  <span className="font-mono text-xs text-gray-500">{e.id}</span>
                  <span className="font-mono text-xs bg-gray-800 px-1.5 py-0.5 rounded">{e.scope}</span>
                  <span className="font-mono text-xs text-gray-500">{e.type}</span>
                  <UseScopeChips scopes={e.use_scopes} />
                  <EvidenceRoleChips roles={e.evidence_roles} />
                  {e.n_linked_codes > 0 && (
                    <span className="text-xs text-emerald-400">→ {e.n_linked_codes} codes</span>
                  )}
                </div>
                <div className="font-semibold mt-1">{e.title}</div>
                <p className="text-sm text-gray-400 mt-1 whitespace-pre-wrap">
                  {expanded === e.id ? e.body : (e.body.slice(0, 200) + (e.body.length > 200 ? "..." : ""))}
                </p>
                {e.body.length > 200 && (
                  <button onClick={() => setExpanded(expanded === e.id ? null : e.id)} className="text-xs text-blue-400 mt-1">
                    {expanded === e.id ? "collapse" : "expand"}
                  </button>
                )}
                <p className="text-xs text-gray-500 italic mt-1">Source: {e.source}</p>
              </div>
              <button onClick={() => deleteEdge(e.id)} className="text-red-400 text-xs">delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------- Commodity ----------------

function CommodityView() {
  const [code, setCode] = useState("6402200000");
  const [data, setData] = useState<CommodityView | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      const d = await api<CommodityView>(`/api/kg/commodity/${encodeURIComponent(code)}`);
      setData(d);
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => { load(); }, []);

  return (
    <div className="space-y-4">
      <div className="bg-gray-900 border border-gray-800 p-4 rounded flex gap-3 items-end">
        <Input label="Commodity code (10-digit, with or without dots)" value={code} onChange={setCode} placeholder="6402200000" width="w-64" />
        <button onClick={load} className="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 text-sm rounded">
          Look up
        </button>
      </div>

      {err && <div className="text-red-400">{err}</div>}
      {data && (
        <div className="space-y-4">
          <div className="bg-gray-900 border border-gray-800 p-4 rounded">
            <div className="font-mono text-xl text-emerald-300">{data.code}</div>
            <div className="text-gray-300 mt-1">{data.description ?? "(not in active goods_nomenclatures)"}</div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-gray-900 border border-gray-800 p-4 rounded">
              <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400 mb-3">
                Structured facts ({data.facets.length})
              </h3>
              {data.facets.length === 0 ? (
                <p className="text-sm text-gray-500 italic">No facts for this code yet.</p>
              ) : (
                <table className="w-full text-sm">
                  <tbody>
                    {data.facets.map((f) => (
                      <tr key={f.id} className="border-b border-gray-800">
                        <td className="py-1 font-mono text-gray-300">{f.facet_key}</td>
                        <td className="py-1 font-mono">{f.facet_value}</td>
                        <td className="py-1">
                          <div className="flex flex-wrap justify-end gap-1">
                            <SourceBadge source={f.source} />
                            <UseScopeChips scopes={f.use_scopes} />
                            <EvidenceRoleChips roles={f.evidence_roles} />
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="bg-gray-900 border border-gray-800 p-4 rounded">
              <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400 mb-3">
                Applicable KG edges ({data.edges.length})
              </h3>
              {data.edges.length === 0 ? (
                <p className="text-sm text-gray-500 italic">No KG edges apply.</p>
              ) : (
                <ul className="space-y-3 text-sm">
                  {data.edges.map((e) => (
                    <li key={e.id}>
                      <div className="flex gap-2 items-baseline">
                        <span className="font-mono text-xs bg-gray-800 px-1.5 py-0.5 rounded">{e.scope}</span>
                        <UseScopeChips scopes={e.use_scopes} />
                        <EvidenceRoleChips roles={e.evidence_roles} />
                        <span className="font-semibold">{e.title}</span>
                      </div>
                      <p className="text-gray-400 text-xs mt-1 whitespace-pre-wrap">{e.body.slice(0, 240)}{e.body.length > 240 ? "..." : ""}</p>
                      <p className="text-xs text-gray-500 italic mt-1">{e.source}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- Common ----------------

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-gray-900 border border-gray-800 p-3 rounded">
      <div className="text-xs text-gray-500 uppercase tracking-wider">{label}</div>
      <div className="text-2xl font-bold text-emerald-400 mt-1">{value.toLocaleString()}</div>
    </div>
  );
}

function SourceBadge({ source }: { source: string }) {
  const colour =
    source === "hand" ? "bg-blue-900 text-blue-200"
    : source === "verified" ? "bg-emerald-900 text-emerald-200"
    : source === "description_llm" ? "bg-amber-900 text-amber-200"
    : source.startsWith("atar:") ? "bg-purple-900 text-purple-200"
    : "bg-gray-800 text-gray-300";
  return <span className={`text-xs font-mono px-1.5 py-0.5 rounded ${colour}`}>{source}</span>;
}

const USE_SCOPE_LABELS: Record<string, string> = {
  retrieval: "Retrieval",
  classification: "Classification",
  qa: "Q&A",
  audit: "Audit",
};

const USE_SCOPE_OPTIONS = [
  { value: "", label: "all" },
  ...Object.entries(USE_SCOPE_LABELS).map(([value, label]) => ({ value, label })),
];

function UseScopeChip({ scope }: { scope: string }) {
  const label = USE_SCOPE_LABELS[scope] || scope;
  const colour =
    scope === "retrieval" ? "bg-sky-950 text-sky-200 border-sky-800"
    : scope === "classification" ? "bg-emerald-950 text-emerald-200 border-emerald-800"
    : scope === "qa" ? "bg-teal-950 text-teal-200 border-teal-800"
    : "bg-gray-800 text-gray-300 border-gray-700";
  return (
    <span className={`text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border ${colour}`}>
      {label}
    </span>
  );
}

function UseScopeChips({ scopes }: { scopes?: string[] }) {
  const clean = Array.from(new Set((scopes || []).filter(Boolean)));
  if (!clean.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {clean.map((scope) => <UseScopeChip key={scope} scope={scope} />)}
    </span>
  );
}

const EVIDENCE_ROLE_LABELS: Record<string, string> = {
  alias: "Alias",
  product_identity: "Product identity",
  material_composition: "Material / composition",
  form_presentation: "Form / presentation",
  function_use: "Function / use",
  packaging_quantity: "Packaging / quantity",
  composition_threshold: "Composition threshold",
  additional_code: "Additional code",
  origin_or_region: "Origin / region",
  legal_definition: "Legal definition",
  legal_inclusion: "Legal inclusion",
  legal_exclusion: "Legal exclusion",
  classification_order: "Classification order",
  classification_rationale: "Rationale",
  interpretive_guidance: "Guidance",
  heading_guidance: "Heading guidance",
  footnote: "Footnote",
  index_text: "Index text",
  unknown: "Unknown",
};

const EVIDENCE_ROLE_OPTIONS = [
  { value: "", label: "all" },
  ...Object.entries(EVIDENCE_ROLE_LABELS).map(([value, label]) => ({ value, label })),
];

function EvidenceRoleChip({ role }: { role: string }) {
  const label = EVIDENCE_ROLE_LABELS[role] || role;
  const colour =
    role === "alias" ? "bg-sky-950 text-sky-200 border-sky-800"
    : role.startsWith("legal_") || role === "classification_order" ? "bg-red-950 text-red-200 border-red-800"
    : role === "footnote" ? "bg-purple-950 text-purple-200 border-purple-800"
    : role.includes("value") ? "bg-indigo-950 text-indigo-200 border-indigo-800"
    : role.includes("guidance") || role === "classification_rationale" ? "bg-blue-950 text-blue-200 border-blue-800"
    : "bg-gray-800 text-gray-300 border-gray-700";
  return (
    <span className={`text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border ${colour}`}>
      {label}
    </span>
  );
}

function EvidenceRoleChips({ roles }: { roles?: string[] }) {
  const clean = Array.from(new Set((roles || []).filter(Boolean)));
  if (!clean.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {clean.map((role) => <EvidenceRoleChip key={role} role={role} />)}
    </span>
  );
}

function Input({ label, value, onChange, placeholder, width = "w-40" }: any) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-xs text-gray-400 uppercase tracking-wider">{label}</span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`bg-gray-800 border border-gray-700 px-2 py-1 text-sm rounded ${width}`}
      />
    </label>
  );
}

function Select({ label, value, onChange, options }: any) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-xs text-gray-400 uppercase tracking-wider">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-800 border border-gray-700 px-2 py-1 text-sm rounded"
      >
        {options.map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}
