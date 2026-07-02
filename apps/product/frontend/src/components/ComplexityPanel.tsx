/**
 * Complexity panel — system-wide views, server-rendered as PNGs.
 *
 * Browser-side recharts choked on 14k+ DOM nodes (slow + crashy). The two
 * heavy charts (chapter scatter + per-template density) are now rendered
 * with matplotlib in the backend and served as PNGs. The frontend is just
 * an <img> tag plus the lightweight Gold-recall audit panel (which has
 * tiny per-chapter bars — fine in recharts).
 */
import { useEffect, useState } from "react";
import { GoldRecallPanel } from "./InterceptsPanel";

type SavedRunStub = {
  id: string;
  name: string;
  saved_at: string;
  n_terms: number;
  kind?: string | null;
};

let _runsCache: SavedRunStub[] | null = null;

export default function ComplexityPanel() {
  const [runs, setRuns] = useState<SavedRunStub[]>(_runsCache ?? []);
  const [runsLoaded, setRunsLoaded] = useState(_runsCache != null);
  const [selectedSweepId, setSelectedSweepId] = useState<string>("");
  const [imgKey, setImgKey] = useState(0);

  useEffect(() => {
    if (_runsCache) return;
    fetch("/api/intercepts/runs")
      .then((r) => r.json())
      .then((all: SavedRunStub[]) => {
        _runsCache = all;
        setRuns(all);
      })
      .catch(() => {})
      .finally(() => setRunsLoaded(true));
  }, []);

  // Auto-pick the most recent commodity sweep once runs land.
  useEffect(() => {
    if (selectedSweepId || !runs.length) return;
    const commodityRuns = runs.filter(
      (r) =>
        r.kind === "commodity_classification" ||
        r.kind === "commodity_classification_gold_recall"
    );
    if (commodityRuns.length) setSelectedSweepId(commodityRuns[0].id);
  }, [runs, selectedSweepId]);

  const commodityRuns = runs.filter(
    (r) =>
      r.kind === "commodity_classification" ||
      r.kind === "commodity_classification_gold_recall"
  );

  const scatterUrl = selectedSweepId
    ? `/api/complexity/charts/scatter?sweep_id=${selectedSweepId}&v=${imgKey}`
    : null;
  const densityUrl = selectedSweepId
    ? `/api/complexity/charts/density?sweep_id=${selectedSweepId}&v=${imgKey}`
    : null;

  return (
    <div className="space-y-4">
      <div className="bg-gray-900 border border-gray-800 rounded p-4">
        <div className="flex items-baseline justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-gray-100">
              Classification complexity
            </h2>
            <div className="text-xs text-gray-500 mt-1 max-w-3xl">
              System-wide views: how the LLM's classification workload distributes
              across the tariff, how often retrieval gets it right, and how the
              HMRC intercept-list templates sit against the commodity baseline.
              Charts are server-rendered as PNGs so the page stays responsive
              even with 14k+ datapoints.
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs text-gray-400 shrink-0">
            <label className="flex items-center gap-2">
              Commodity sweep:
              <select
                value={selectedSweepId}
                onChange={(e) => setSelectedSweepId(e.target.value)}
                disabled={!runsLoaded}
                className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm max-w-xs"
              >
                <option value="">
                  {runsLoaded ? "Choose a run…" : "Loading runs…"}
                </option>
                {commodityRuns.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name} ({r.n_terms.toLocaleString()} codes)
                  </option>
                ))}
              </select>
            </label>
            <button
              onClick={() => setImgKey((k) => k + 1)}
              className="px-2 py-1 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded text-xs"
              title="Bypass the cached PNG and re-render"
            >
              Refresh
            </button>
          </div>
        </div>
      </div>

      {scatterUrl && (
        <div className="bg-gray-900 border border-gray-800 rounded p-4">
          <h3 className="text-sm font-semibold text-gray-100 mb-1">
            Composite complexity by chapter
          </h3>
          <div className="text-[11px] text-gray-500 mb-2">
            Grey = commodity codes in the sweep. Coloured circles = HMRC's 728
            curated intercept list (legacy template, never re-classified).
            Purple rings = bucket-B commodities flagged Context dependent (walked
            path has legal/lab/expert predicates). MISS gutter on the right holds
            intercept terms where retrieval returned zero candidates.
          </div>
          <img
            src={scatterUrl}
            alt="Chapter scatter"
            className="w-full h-auto rounded border border-gray-800"
            loading="lazy"
          />
        </div>
      )}

      {densityUrl && (
        <div className="bg-gray-900 border border-gray-800 rounded p-4">
          <h3 className="text-sm font-semibold text-gray-100 mb-1">
            Per-template density vs commodity baseline
          </h3>
          <div className="text-[11px] text-gray-500 mb-2">
            Each series normalised to within-series density so populations of very
            different sizes can be compared. If the three template distributions
            sit on top of the grey commodity baseline, there's no clean composite
            threshold that separates HMRC templates — which matches the earlier
            threshold-search (F1) result.
          </div>
          <img
            src={densityUrl}
            alt="Per-template density"
            className="w-full h-auto rounded border border-gray-800"
            loading="lazy"
          />
        </div>
      )}

      <GoldRecallPanel />
    </div>
  );
}
