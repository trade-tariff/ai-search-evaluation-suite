import { useEffect, useMemo, useRef, useState } from "react";
import CytoscapeComponent from "react-cytoscapejs";
import cytoscape from "cytoscape";
import coseBilkent from "cytoscape-cose-bilkent";
import fcose from "cytoscape-fcose";

cytoscape.use(coseBilkent as any);
cytoscape.use(fcose as any);

interface CyNode {
  data: { id: string; label: string; type: string; [k: string]: any };
}
interface CyEdge {
  data: { id: string; source: string; target: string; label: string; type: string };
}
interface GraphPayload {
  nodes: CyNode[];
  edges: CyEdge[];
  stats: { n_nodes?: number; n_edges?: number; reason?: string };
}

const ALL_RULE_TYPES = ["exclusion", "definition", "classification_order", "discriminator", "rationale", "duty_treatment", "other"];

export default function KnowledgeGraphView() {
  const [mode, setMode] = useState<"code" | "chapter" | "all">("code");
  const [focusCode, setFocusCode] = useState("6402200000");
  const [chapter, setChapter] = useState("64");
  const [maxNodes, setMaxNodes] = useState(300);
  const [activeRuleTypes, setActiveRuleTypes] = useState<string[]>([...ALL_RULE_TYPES]);
  const [includeGaps, setIncludeGaps] = useState(false);
  const [gapChapters, setGapChapters] = useState("22,64,73");
  const [data, setData] = useState<GraphPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<CyNode["data"] | null>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);

  async function load() {
    setLoading(true); setErr(null); setSelected(null);
    let url = "/api/kg/graph?max_nodes=" + maxNodes;
    if (mode === "code") url += `&focus_code=${encodeURIComponent(focusCode)}`;
    else if (mode === "chapter") url += `&chapter=${encodeURIComponent(chapter)}`;
    else {
      url += `&all_mode=true`;
      if (activeRuleTypes.length > 0 && activeRuleTypes.length < ALL_RULE_TYPES.length) {
        url += `&rule_types=${activeRuleTypes.join(",")}`;
      }
      if (includeGaps && gapChapters.trim()) {
        url += `&include_gaps=true&gap_chapters=${encodeURIComponent(gapChapters.trim())}`;
      }
    }
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error(await r.text());
      setData(await r.json());
    } catch (e: any) {
      setErr(e.message ?? String(e));
    } finally {
      setLoading(false);
    }
  }

  function toggleRuleType(t: string) {
    setActiveRuleTypes(rs => rs.includes(t) ? rs.filter(x => x !== t) : [...rs, t]);
  }
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const elements = useMemo(() => {
    if (!data) return [];
    return [...data.nodes, ...data.edges];
  }, [data]);

  // Level-of-detail thresholds. cytoscape's zoom is 1.0 = default; we cull labels at low zoom.
  const labelZoomThreshold = mode === "all" ? 1.2 : 0.5;
  const codeLabelZoom = mode === "all" ? 0.8 : 0.3;

  const style: cytoscape.StylesheetCSS[] = [
    {
      selector: "node",
      css: {
        // Labels only show above the zoom threshold; cytoscape supports zoom-conditional styles.
        label: "data(label)",
        "font-size": "10px",
        "text-wrap": "wrap",
        "text-max-width": "120px",
        color: "#e5e7eb",
        "text-outline-width": 1,
        "text-outline-color": "#0b1220",
        // Hide labels when zoomed out far enough that they'd just become noise.
        "min-zoomed-font-size": `${Math.round(8 / labelZoomThreshold)}px`,
      },
    },
    {
      selector: 'node[type="code"]',
      css: {
        "background-color": "#10b981",
        shape: "round-rectangle",
        width: 60, height: 28,
        "font-size": "11px",
        "font-weight": "bold" as any,
      },
    },
    {
      selector: 'node[type="code_gap"]',
      css: {
        "background-color": "#374151",
        "border-width": 1,
        "border-color": "#4b5563",
        color: "#9ca3af",
        shape: "round-rectangle",
        width: 60, height: 22,
        "font-size": "10px",
        opacity: 0.7,
      },
    },
    {
      selector: 'node[type="facet_bag"]',
      css: {
        "background-color": "#3b82f6",
        shape: "round-rectangle",
        width: 50, height: 24,
      },
    },
    {
      selector: 'node[type="rule"]',
      css: {
        "background-color": "#a855f7",
        shape: "ellipse",
        width: 30, height: 30,
      },
    },
    {
      selector: 'node[rule_type="exclusion"]',
      css: { "background-color": "#ef4444" },
    },
    {
      selector: 'node[rule_type="definition"]',
      css: { "background-color": "#f59e0b" },
    },
    {
      selector: 'node[rule_type="classification_order"]',
      css: { "background-color": "#8b5cf6" },
    },
    {
      selector: 'node[rule_type="discriminator"]',
      css: { "background-color": "#06b6d4" },
    },
    {
      selector: "node:selected",
      css: {
        "border-width": 3,
        "border-color": "#fbbf24",
      },
    },
    {
      selector: "edge",
      css: {
        width: 1,
        "line-color": "#374151",
        "curve-style": "bezier",
        "target-arrow-color": "#374151",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.6,
        opacity: 0.6,
      },
    },
    {
      selector: 'edge[type="has_facts"]',
      css: { "line-color": "#3b82f6", "target-arrow-color": "#3b82f6" },
    },
  ];

  return (
    <div className="space-y-4">
      <div className="bg-gray-900 border border-gray-800 p-4 rounded space-y-3">
        <div className="flex flex-wrap gap-3 items-end">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs text-gray-400 uppercase tracking-wider">Mode</span>
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as any)}
              className="bg-gray-800 border border-gray-700 px-2 py-1 text-sm rounded"
            >
              <option value="code">Focus on one commodity code</option>
              <option value="chapter">Whole chapter</option>
              <option value="all">Entire KG (all rules + linked codes)</option>
            </select>
          </label>
          {mode === "code" && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Code</span>
              <input
                type="text"
                value={focusCode}
                onChange={(e) => setFocusCode(e.target.value)}
                placeholder="6402200000"
                className="bg-gray-800 border border-gray-700 px-2 py-1 text-sm rounded w-40"
              />
            </label>
          )}
          {mode === "chapter" && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Chapter (2-digit)</span>
              <input
                type="text"
                value={chapter}
                onChange={(e) => setChapter(e.target.value)}
                placeholder="64"
                className="bg-gray-800 border border-gray-700 px-2 py-1 text-sm rounded w-20"
              />
            </label>
          )}
          {mode === "all" && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Max nodes</span>
              <input
                type="number"
                value={maxNodes}
                onChange={(e) => setMaxNodes(Math.max(50, Math.min(1500, Number(e.target.value) || 300)))}
                min={50}
                max={1500}
                className="bg-gray-800 border border-gray-700 px-2 py-1 text-sm rounded w-24"
              />
            </label>
          )}
          <button
            onClick={load}
            className="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 text-sm rounded"
            disabled={loading}
          >
            {loading ? "Loading..." : "Render"}
          </button>
          {data && (
            <span className="text-xs text-gray-400 ml-2">
              {data.stats.n_nodes ?? 0} nodes · {data.stats.n_edges ?? 0} edges
            </span>
          )}
        </div>

        {mode === "all" && (
          <div>
            <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">Rule type filters</div>
            <div className="flex flex-wrap gap-2">
              {ALL_RULE_TYPES.map(t => (
                <button
                  key={t}
                  onClick={() => toggleRuleType(t)}
                  className={`text-xs px-2 py-1 rounded ${
                    activeRuleTypes.includes(t)
                      ? "bg-emerald-700 text-white border border-emerald-500"
                      : "bg-gray-800 text-gray-500 border border-gray-700"
                  }`}
                >
                  {t.replace(/_/g, " ")}
                </button>
              ))}
              <button
                onClick={() => setActiveRuleTypes([...ALL_RULE_TYPES])}
                className="text-xs px-2 py-1 rounded bg-gray-800 text-gray-400 border border-gray-700 hover:text-white"
              >
                reset all
              </button>
              <button
                onClick={() => setActiveRuleTypes([])}
                className="text-xs px-2 py-1 rounded bg-gray-800 text-gray-400 border border-gray-700 hover:text-white"
              >
                clear
              </button>
            </div>
            <div className="mt-3 flex flex-wrap gap-3 items-center text-xs text-gray-300">
              <label className="flex items-center gap-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={includeGaps}
                  onChange={(e) => setIncludeGaps(e.target.checked)}
                  className="h-3 w-3"
                />
                Show coverage gaps (codes with no facts / no rule links)
              </label>
              {includeGaps && (
                <label className="flex items-center gap-1">
                  for chapters:
                  <input
                    type="text"
                    value={gapChapters}
                    onChange={(e) => setGapChapters(e.target.value)}
                    placeholder="22,64,73"
                    className="bg-gray-800 border border-gray-700 px-2 py-0.5 text-xs rounded w-32"
                  />
                </label>
              )}
            </div>
            <p className="text-xs text-gray-500 mt-2">
              Heads up: rendering ~500 nodes takes a few seconds for the force layout to settle. Drag to rearrange.
            </p>
          </div>
        )}
      </div>

      {err && <div className="text-red-400 text-sm">{err}</div>}

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <div className="lg:col-span-3 bg-gray-950 border border-gray-800 rounded relative" style={{ height: 820 }}>
          {elements.length > 0 && (
            <CytoscapeComponent
              elements={elements as any}
              stylesheet={style as any}
              layout={
                mode === "all"
                  ? {
                      // fcose tuned for *airing out* the graph. Higher repulsion + longer
                      // edges keeps clusters from huddling. Disconnected components are
                      // tiled at the edges so they don't collapse onto each other.
                      name: "fcose",
                      animate: false,
                      quality: "default",
                      randomize: true,
                      nodeRepulsion: 30000,        // was 4500 - cranked way up
                      idealEdgeLength: 220,         // was 70 - much longer connections
                      nodeSeparation: 220,          // was 60
                      gravity: 0.08,                // was 0.4 - barely any pull to centre
                      gravityRangeCompound: 1.5,
                      gravityCompound: 0.5,
                      packComponents: true,
                      tile: true,
                      tilingPaddingVertical: 80,    // was 5 - much more space between components
                      tilingPaddingHorizontal: 80,  // was 5
                      numIter: 2500,                // was 1000 - let it converge
                      fit: true,
                      padding: 40,
                    } as any
                  : ({
                      name: "cose-bilkent",
                      animate: false,
                      idealEdgeLength: 100,
                      nodeRepulsion: 6000,
                      nestingFactor: 0.1,
                      gravity: 0.25,
                      numIter: 1500,
                      tile: true,
                      fit: true,
                      padding: 30,
                      quality: "default",
                    } as any)
              }
              wheelSensitivity={0.3}
              minZoom={0.05}
              maxZoom={3}
              hideEdgesOnViewport={(data?.stats.n_nodes ?? 0) > 500}
              textureOnViewport={(data?.stats.n_nodes ?? 0) > 1000}
              motionBlur={false}
              style={{ width: "100%", height: "100%" }}
              cy={(cy: cytoscape.Core) => {
                cyRef.current = cy;
                cy.on("tap", "node", (evt: any) => {
                  setSelected(evt.target.data());
                });
                cy.on("tap", (evt: any) => {
                  if (evt.target === cy) setSelected(null);
                });
              }}
            />
          )}
          {/* Legend */}
          <div className="absolute top-2 right-2 bg-gray-900/90 border border-gray-800 p-2 rounded text-xs">
            <div className="font-bold uppercase text-gray-400 mb-1">Legend</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded" style={{ background: "#10b981" }} /> commodity code</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded" style={{ background: "#374151" }} /> code (no facts/rules)</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded" style={{ background: "#3b82f6" }} /> structured facts</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: "#a855f7" }} /> rule (other)</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: "#ef4444" }} /> exclusion</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: "#f59e0b" }} /> definition</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: "#8b5cf6" }} /> classification order</div>
            <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: "#06b6d4" }} /> discriminator</div>
          </div>
        </div>

        <aside className="bg-gray-900 border border-gray-800 p-3 rounded text-sm">
          <div className="text-xs uppercase tracking-wider font-bold text-gray-400 mb-2">
            {selected ? "Node detail" : "Click a node"}
          </div>
          {selected ? (
            <div className="space-y-2">
              <div>
                <div className="text-xs text-gray-500">id</div>
                <div className="font-mono text-xs break-all">{selected.id}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">type</div>
                <div className="font-mono">{selected.type}</div>
              </div>
              {selected.description && (
                <div>
                  <div className="text-xs text-gray-500">description</div>
                  <div className="text-xs">{selected.description}</div>
                </div>
              )}
              {selected.scope && (
                <div>
                  <div className="text-xs text-gray-500">scope</div>
                  <div className="font-mono">{selected.scope}</div>
                </div>
              )}
              {selected.rule_type && (
                <div>
                  <div className="text-xs text-gray-500">rule type</div>
                  <div className="font-mono">{selected.rule_type}</div>
                </div>
              )}
              {selected.source && (
                <div>
                  <div className="text-xs text-gray-500">source</div>
                  <div className="text-xs italic">{selected.source}</div>
                </div>
              )}
              {selected.body && (
                <div>
                  <div className="text-xs text-gray-500">body</div>
                  <p className="text-xs text-gray-300 whitespace-pre-wrap">{selected.body}</p>
                </div>
              )}
              {selected.summary && (
                <div>
                  <div className="text-xs text-gray-500">facets</div>
                  <p className="text-xs text-gray-300 font-mono">{selected.summary}</p>
                </div>
              )}
              {selected.n_facets !== undefined && (
                <div className="text-xs text-emerald-400">{selected.n_facets} structured facts</div>
              )}
            </div>
          ) : (
            <p className="text-xs text-gray-500">
              Tap any node to inspect. Pinch / scroll to zoom. Drag to pan. Drag a node to rearrange.
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}
