import { useEffect, useMemo, useState } from "react";
import { getConfig, updateConfig } from "../api";
import type { AppConfig, ModelConfig, SimulatorConfigT } from "../types";

const REASONING_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh"];

function supportsSimulator(model: ModelConfig): boolean {
  return (
    model.enabled &&
    model.provider === "openai" &&
    (model.id.startsWith("gpt-5") || model.id.startsWith("o"))
  );
}

export default function SimulatorPanel() {
  const [config, setConfig] = useState<SimulatorConfigT | null>(null);
  const [draft, setDraft] = useState<SimulatorConfigT | null>(null);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    getConfig().then((c: AppConfig) => {
      setConfig(c.simulator_config || null);
      setDraft(c.simulator_config || null);
      setModels((c.models || []).filter(supportsSimulator));
    });
  }, []);

  const modelById = useMemo(() => {
    const map = new Map<string, ModelConfig>();
    for (const model of models) map.set(model.id, model);
    return map;
  }, [models]);

  const hasChanges =
    config && draft && JSON.stringify(config) !== JSON.stringify(draft);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      await updateConfig({ simulator_config: draft });
      setConfig(draft);
      setMsg("Saved");
    } catch (e) {
      setMsg(`Error: ${e}`);
    }
    setSaving(false);
    setTimeout(() => setMsg(""), 3000);
  };

  const reset = () => {
    if (config) setDraft({ ...config });
  };

  const selectModel = (id: string) => {
    if (!draft) return;
    const match = modelById.get(id);
    if (!match) return;
    setDraft({
      ...draft,
      model: match.id,
      input_cost_per_million: match.input_cost_per_million,
      output_cost_per_million: match.output_cost_per_million,
    });
  };

  if (!draft) return <div className="text-gray-400">Loading simulator config...</div>;

  const selectedModel = modelById.get(draft.model);

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Simulator</h2>
          <p className="text-sm text-gray-400 mt-1">
            Controls the synthetic trader that answers model follow-up
            questions during benchmark runs.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {msg && (
            <span
              className={`text-sm ${msg.startsWith("Error") ? "text-red-400" : "text-green-400"}`}
            >
              {msg}
            </span>
          )}
          {hasChanges && (
            <button
              onClick={reset}
              className="px-3 py-1.5 text-sm bg-gray-800 hover:bg-gray-700 rounded"
            >
              Discard
            </button>
          )}
          <button
            onClick={save}
            disabled={saving || !hasChanges}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded text-sm font-medium disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      </div>

      <section className="bg-gray-900 rounded-lg p-5">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="font-medium">Trader Simulator</h3>
            <p className="text-sm text-gray-400 mt-1">
              When enabled, every candidate sees answers from the same shared
              fact store instead of falling back to the first answer option.
            </p>
          </div>
          <button
            onClick={() => setDraft({ ...draft, enabled: !draft.enabled })}
            aria-label={draft.enabled ? "Disable simulator" : "Enable simulator"}
            aria-pressed={draft.enabled}
            className={`relative w-12 h-6 rounded-full transition-colors ${
              draft.enabled ? "bg-blue-600" : "bg-gray-700"
            }`}
          >
            <span
              className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
                draft.enabled ? "translate-x-6" : ""
              }`}
            />
          </button>
        </div>
      </section>

      <section
        className={`bg-gray-900 rounded-lg p-5 space-y-4 ${!draft.enabled ? "opacity-50 pointer-events-none" : ""}`}
      >
        <div>
          <label className="block text-sm text-gray-400 mb-1">Model</label>
          <select
            value={draft.model}
            onChange={(e) => selectModel(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
          >
            {models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.name}
                {model.reasoning_effort ? ` · ${model.reasoning_effort}` : ""}
              </option>
            ))}
          </select>
          <p className="text-xs text-gray-500 mt-1">
            OpenAI reasoning-capable models only. The simulator uses the
            OpenAI client path in the backend today.
          </p>
        </div>

        {selectedModel && (
          <div className="flex items-center gap-4 text-sm text-gray-400">
            <span>
              Input: <span className="text-gray-200">${selectedModel.input_cost_per_million}/M tokens</span>
            </span>
            <span>
              Output: <span className="text-gray-200">${selectedModel.output_cost_per_million}/M tokens</span>
            </span>
          </div>
        )}

        <div>
          <label className="block text-sm text-gray-400 mb-2">Reasoning Effort</label>
          <div className="flex items-center gap-2 flex-wrap">
            {REASONING_EFFORT_OPTIONS.map((level) => (
              <button
                key={level}
                onClick={() => setDraft({ ...draft, reasoning_effort: level })}
                className={`px-3 py-1.5 text-sm rounded-full border transition-colors ${
                  draft.reasoning_effort === level
                    ? level === "low" ? "bg-green-700 border-green-600 text-white"
                    : level === "medium" ? "bg-blue-600 border-blue-500 text-white"
                    : level === "high" ? "bg-amber-700 border-amber-600 text-white"
                    : "bg-red-700 border-red-600 text-white"
                    : "bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-500"
                }`}
              >
                {level === "xhigh" ? "XHigh" : level.charAt(0).toUpperCase() + level.slice(1)}
              </button>
            ))}
          </div>
          <p className="text-xs text-gray-500 mt-1">
            Lower effort is usually enough. The simulator is resolving short
            multiple-choice questions, not writing the final classification.
          </p>
        </div>

        <div className="max-w-xs">
          <label className="block text-sm text-gray-400 mb-1">Temperature</label>
          <input
            type="number"
            min={0}
            max={2}
            step={0.1}
            value={draft.temperature}
            onChange={(e) =>
              setDraft({ ...draft, temperature: Math.max(0, Math.min(2, parseFloat(e.target.value) || 0)) })
            }
            className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
          />
          <p className="text-xs text-gray-500 mt-1">
            Only used if reasoning effort is blank in the backend path.
          </p>
        </div>
      </section>
    </div>
  );
}
