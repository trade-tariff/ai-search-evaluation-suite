import { useEffect, useMemo, useState } from "react";
import { getConfig, updateConfig } from "../api";
import type { AppConfig, ModelConfig, ReferenceConfig, ReferenceMode } from "../types";

const MODE_LABELS: Record<ReferenceMode, string> = {
  single: "Single model, single pass",
  multi_pass: "Single model, multi-pass",
  panel: "Panel (multi-model consensus)",
};

const MODE_DESCRIPTIONS: Record<ReferenceMode, string> = {
  single:
    "Point estimate. Runs the chosen model once per prompt. Cheapest, fastest, and a fine choice when you trust the reference model on its own.",
  multi_pass:
    "Reduces same-model variance. Runs the chosen model N times per prompt and majority-votes on the final commodity code. Low panel_agreement in a prompt = the reference is uncertain, which is itself a useful signal.",
  panel:
    "Reduces model bias. Runs several different reference models once each per prompt and aggregates via consensus. Use when one model's priors might be suspect (e.g. OpenAI-only reference for open-source candidates).",
};

function isTier1Paid(m: ModelConfig): boolean {
  return m.enabled && m.category === "tier1_paid";
}

export default function ReferencePanel() {
  const [cfg, setCfg] = useState<ReferenceConfig | null>(null);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const c: AppConfig = await getConfig();
        setModels(c.models || []);
        setCfg(
          c.reference_config || {
            mode: "single",
            model_id: "gpt-5.4-xhigh",
            passes: 3,
            panel_model_ids: ["gpt-5.4-xhigh", "gpt-5.2"],
          },
        );
      } catch (e) {
        setErr("Failed to load config: " + String(e));
      }
    })();
  }, []);

  const tier1 = useMemo(() => models.filter(isTier1Paid), [models]);
  const modelById = useMemo(() => {
    const m = new Map<string, ModelConfig>();
    for (const x of models) m.set(x.id, x);
    return m;
  }, [models]);

  const save = async (next: ReferenceConfig) => {
    setSaving(true);
    setErr("");
    setMsg("");
    try {
      await updateConfig({ reference_config: next });
      setCfg(next);
      setMsg("Saved.");
      setTimeout(() => setMsg(""), 1500);
    } catch (e) {
      setErr("Save failed: " + String(e));
    } finally {
      setSaving(false);
    }
  };

  if (err && !cfg) return <div className="text-red-400">{err}</div>;
  if (!cfg) return <div className="text-gray-500">Loading reference config...</div>;

  const setMode = (mode: ReferenceMode) => save({ ...cfg, mode });
  const setModelId = (model_id: string) => save({ ...cfg, model_id });
  const setPasses = (passes: number) =>
    save({ ...cfg, passes: Math.max(1, Math.min(9, Math.round(passes))) });
  const togglePanelMember = (id: string) => {
    const has = cfg.panel_model_ids.includes(id);
    const next = has
      ? cfg.panel_model_ids.filter((x) => x !== id)
      : [...cfg.panel_model_ids, id];
    save({ ...cfg, panel_model_ids: next });
  };

  // Estimate calls per prompt for the banner
  const callsPerPrompt =
    cfg.mode === "single" ? 1 : cfg.mode === "multi_pass" ? cfg.passes : cfg.panel_model_ids.length;

  const currentRef =
    cfg.mode === "panel"
      ? cfg.panel_model_ids.map((id) => modelById.get(id)?.name ?? id).join(" + ")
      : modelById.get(cfg.model_id)?.name ?? cfg.model_id;

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h2 className="text-lg font-semibold mb-1">Reference</h2>
        <p className="text-sm text-gray-400">
          The pinned gold anchor that runs for every prompt regardless of which
          candidates you select. The judge scores candidates against this
          anchor, and the fact store ensures every model faces the same
          committed facts - so convergence with the reference is the
          apples-to-apples signal you want.
        </p>
      </div>

      {/* Current summary banner */}
      <div className="rounded-lg bg-gray-900 border border-gray-800 p-4 flex items-center justify-between gap-4">
        <div>
          <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">Currently</div>
          <div className="text-sm font-medium">
            {MODE_LABELS[cfg.mode]} · <span className="text-emerald-300">{currentRef}</span>
          </div>
        </div>
        <div className="text-xs text-gray-500">
          <span className="font-mono text-gray-300">{callsPerPrompt}</span> reference call{callsPerPrompt === 1 ? "" : "s"} / prompt
        </div>
      </div>

      {/* Mode selector */}
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium mb-2">Mode</legend>
        {(["single", "multi_pass", "panel"] as ReferenceMode[]).map((m) => (
          <label
            key={m}
            className={`flex items-start gap-3 p-3 rounded border cursor-pointer ${
              cfg.mode === m
                ? "bg-blue-950/30 border-blue-700"
                : "bg-gray-900 border-gray-800 hover:border-gray-700"
            }`}
          >
            <input
              type="radio"
              name="mode"
              checked={cfg.mode === m}
              onChange={() => setMode(m)}
              disabled={saving}
              className="mt-0.5"
            />
            <div>
              <div className="text-sm font-medium">{MODE_LABELS[m]}</div>
              <div className="text-xs text-gray-400 mt-0.5">{MODE_DESCRIPTIONS[m]}</div>
            </div>
          </label>
        ))}
      </fieldset>

      {/* Mode-specific controls */}
      {cfg.mode === "single" && (
        <div className="rounded-lg bg-gray-900 border border-gray-800 p-4 space-y-3">
          <label className="text-sm font-medium block">Reference model</label>
          <select
            value={cfg.model_id}
            onChange={(e) => setModelId(e.target.value)}
            disabled={saving}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm w-full"
          >
            {tier1.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
                {m.reasoning_effort ? ` · ${m.reasoning_effort}` : ""}
                {" · $"}{m.input_cost_per_million}/{m.output_cost_per_million} per M
              </option>
            ))}
          </select>
          <p className="text-xs text-gray-500">
            Tip: pick a strong reasoner with high effort. gpt-5.4 xhigh is the default.
          </p>
        </div>
      )}

      {cfg.mode === "multi_pass" && (
        <div className="rounded-lg bg-gray-900 border border-gray-800 p-4 space-y-3">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-sm font-medium block mb-1">Reference model</label>
              <select
                value={cfg.model_id}
                onChange={(e) => setModelId(e.target.value)}
                disabled={saving}
                className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm w-full"
              >
                {tier1.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                    {m.reasoning_effort ? ` · ${m.reasoning_effort}` : ""}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-sm font-medium block mb-1">Passes</label>
              <input
                type="number"
                min={1}
                max={9}
                value={cfg.passes}
                onChange={(e) => setPasses(Number(e.target.value))}
                disabled={saving}
                className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm w-full"
              />
              <p className="text-xs text-gray-500 mt-1">
                {cfg.passes} call{cfg.passes === 1 ? "" : "s"} per prompt.
                {cfg.passes > 1 ? " Majority-votes on the final code, emits panel_agreement." : ""}
              </p>
            </div>
          </div>
        </div>
      )}

      {cfg.mode === "panel" && (
        <div className="rounded-lg bg-gray-900 border border-gray-800 p-4 space-y-3">
          <label className="text-sm font-medium block">Panel members ({cfg.panel_model_ids.length} selected, min 2)</label>
          <div className="max-h-80 overflow-y-auto grid grid-cols-2 gap-2">
            {tier1.map((m) => {
              const selected = cfg.panel_model_ids.includes(m.id);
              return (
                <label
                  key={m.id}
                  className={`flex items-center gap-2 p-2 rounded border text-sm cursor-pointer ${
                    selected
                      ? "bg-emerald-950/40 border-emerald-800"
                      : "bg-gray-800 border-gray-700 hover:border-gray-600"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={selected}
                    onChange={() => togglePanelMember(m.id)}
                    disabled={saving}
                  />
                  <span className="truncate">
                    {m.name}
                    {m.reasoning_effort ? <span className="text-gray-500"> · {m.reasoning_effort}</span> : null}
                  </span>
                </label>
              );
            })}
          </div>
          <p className="text-xs text-gray-500">
            Consensus is computed via pairwise cosine similarity + majority-vote on final codes. panel_agreement is exposed per prompt - low agreement flags genuinely ambiguous queries.
          </p>
          {cfg.panel_model_ids.length < 2 && (
            <p className="text-xs text-red-400">Select at least 2 models for panel mode.</p>
          )}
        </div>
      )}

      <div className="flex items-center gap-3 text-xs">
        {saving && <span className="text-gray-400">Saving...</span>}
        {msg && <span className="text-emerald-400">{msg}</span>}
        {err && <span className="text-red-400">{err}</span>}
      </div>
    </div>
  );
}
