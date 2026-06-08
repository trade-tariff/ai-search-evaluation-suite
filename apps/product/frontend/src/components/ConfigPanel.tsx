import { useEffect, useState } from "react";
import { getConfig, updateConfig } from "../api";
import type { AppConfig, ModelConfig } from "../types";

interface Props {
  selectedModels: string[];
  onModelsChange: (ids: string[]) => void;
}

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google (Free Tier)",
  xai: "xAI Grok ($25 Free Credits)",
  groq: "Groq (Free Tier)",
  deepseek: "DeepSeek (Free Credits)",
  mistral: "Mistral (Free Tier)",
  openrouter: "OpenRouter (Free Models)",
  cerebras: "Cerebras (Free Tier)",
  sambanova: "SambaNova (Free Credits)",
};

// ── Effort-level grouping utilities ──────────────────────────

interface ModelFamily {
  modelId: string;
  primary: ModelConfig;
  members: ModelConfig[];
}

function getEffortOrder(m: ModelConfig): number {
  if (m.reasoning_effort === "low") return 0;
  if (m.reasoning_effort === "high") return 2;
  if (m.reasoning_effort === "xhigh") return 3;
  if (m.thinking_budget != null && m.thinking_budget > 0) return 4;
  return 1; // medium or standard (no modifier)
}

function getEffortLabel(m: ModelConfig): string {
  if (m.reasoning_effort === "xhigh") return "XHigh";
  if (m.reasoning_effort) {
    return m.reasoning_effort.charAt(0).toUpperCase() + m.reasoning_effort.slice(1);
  }
  if (m.thinking_budget != null && m.thinking_budget > 0) {
    return "Thinking";
  }
  return "Standard";
}

function getEffortChipClass(m: ModelConfig, selected: boolean): string {
  if (!selected) return "bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-500";
  if (m.reasoning_effort === "low") return "bg-green-700 border-green-600 text-white";
  if (m.reasoning_effort === "high") return "bg-amber-700 border-amber-600 text-white";
  if (m.reasoning_effort === "xhigh") return "bg-red-700 border-red-600 text-white";
  if (m.thinking_budget) return "bg-purple-700 border-purple-600 text-white";
  return "bg-blue-600 border-blue-500 text-white"; // medium/standard
}

function groupModelFamilies(models: ModelConfig[]): ModelFamily[] {
  const groups = new Map<string, ModelConfig[]>();
  const order: string[] = [];

  for (const m of models) {
    if (!groups.has(m.model_id)) {
      groups.set(m.model_id, []);
      order.push(m.model_id);
    }
    groups.get(m.model_id)!.push(m);
  }

  return order.map((modelId) => {
    const members = groups.get(modelId)!;
    const primary = members.find((m) => m.enabled) || members[0];
    members.sort((a, b) => getEffortOrder(a) - getEffortOrder(b));
    return { modelId, primary, members };
  });
}

export default function ConfigPanel({ selectedModels, onModelsChange }: Props) {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({
    tier1_free: false,
    tier1_paid: false,
    tier2: true,
  });

  const hasKey = (m: ModelConfig, keysSet: Record<string, boolean>) => {
    if (m.provider === "openai_compatible") return keysSet[m.api_key_env || ""] ?? false;
    return keysSet[m.provider] ?? false;
  };

  useEffect(() => {
    getConfig().then((c) => {
      setConfig(c);
      // If App already hydrated selectedModels from default_selected_model_ids
      // (in App.tsx's useEffect), respect that. Otherwise auto-pick all enabled
      // models with keys on first load.
      if (selectedModels.length === 0) {
        const hasDefaults =
          (c.default_selected_model_ids?.length ?? 0) > 0;
        if (hasDefaults) {
          onModelsChange(c.default_selected_model_ids!);
        } else {
          onModelsChange(
            c.models
              .filter((m) => m.enabled && hasKey(m, c.api_keys_set))
              .map((m) => m.id),
          );
        }
      }
    });
  }, []);

  const saveSelectionAsDefault = async () => {
    setSaving(true);
    try {
      await updateConfig({ default_selected_model_ids: selectedModels });
      setMsg(`Saved ${selectedModels.length} model${selectedModels.length === 1 ? "" : "s"} as default`);
    } catch (e) {
      setMsg(`Error: ${e}`);
    }
    setSaving(false);
    setTimeout(() => setMsg(""), 3000);
  };

  const restoreDefault = async () => {
    try {
      const c = await getConfig();
      const ids = c.default_selected_model_ids ?? [];
      if (ids.length === 0) {
        setMsg("No saved defaults yet. Pick your models, then click 'Save as default'.");
      } else {
        onModelsChange(ids);
        setMsg(`Restored ${ids.length} saved default${ids.length === 1 ? "" : "s"}`);
      }
    } catch (e) {
      setMsg(`Error: ${e}`);
    }
    setTimeout(() => setMsg(""), 3000);
  };

  const saveKeys = async () => {
    setSaving(true);
    try {
      const filtered: Record<string, string> = {};
      for (const [k, v] of Object.entries(keys)) {
        if (v && v.trim()) filtered[k] = v.trim();
      }
      await updateConfig({ api_keys: filtered });
      setMsg("Saved");
      setKeys({});
      const c = await getConfig();
      setConfig(c);
    } catch (e) {
      setMsg(`Error: ${e}`);
    }
    setSaving(false);
    setTimeout(() => setMsg(""), 3000);
  };

  const toggleModel = (id: string) => {
    if (selectedModels.includes(id)) {
      onModelsChange(selectedModels.filter((m) => m !== id));
    } else {
      onModelsChange([...selectedModels, id]);
    }
  };

  const selectTier = (tier: string) => {
    if (!config) return;
    const families = groupModelFamilies(
      config.models.filter((m) => m.category === tier),
    );
    const allFamilyIds = new Set(families.flatMap((f) => f.members.map((m) => m.id)));
    const primaryIds = families.map((f) => f.primary.id);
    const otherSelected = selectedModels.filter((id) => !allFamilyIds.has(id));
    const allPrimariesSelected = primaryIds.every((id) => selectedModels.includes(id));
    if (allPrimariesSelected) {
      onModelsChange(otherSelected);
    } else {
      onModelsChange([...otherSelected, ...primaryIds]);
    }
  };

  const toggleCollapse = (tier: string) => {
    setCollapsed((prev) => ({ ...prev, [tier]: !prev[tier] }));
  };

  if (!config) return <div className="text-gray-400">Loading config...</div>;

  const tier1Paid = groupModelFamilies(config.models.filter((m) => m.category === "tier1_paid"));
  const tier1Free = groupModelFamilies(config.models.filter((m) => m.category === "tier1_free"));
  const tier2 = groupModelFamilies(config.models.filter((m) => m.category === "tier2"));

  const renderModelFamily = (family: ModelFamily) => {
    const { primary, members } = family;
    const hasVariants = members.length > 1;
    const keySet = config ? hasKey(primary, config.api_keys_set) : false;
    const anySelected = members.some((m) => selectedModels.includes(m.id));

    const toggleFamily = () => {
      if (anySelected) {
        const memberIds = new Set(members.map((m) => m.id));
        onModelsChange(selectedModels.filter((id) => !memberIds.has(id)));
      } else {
        onModelsChange([...selectedModels, primary.id]);
      }
    };

    return (
      <div
        key={family.modelId}
        className={`bg-gray-900 rounded-lg p-4 cursor-pointer border transition-colors ${
          anySelected
            ? "border-blue-500"
            : "border-gray-800 hover:border-gray-600"
        } ${!keySet ? "opacity-50" : ""}`}
        onClick={toggleFamily}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <input
              type="checkbox"
              checked={anySelected}
              onChange={toggleFamily}
              onClick={(e) => e.stopPropagation()}
              className="h-4 w-4"
            />
            <div>
              <div className="font-medium">
                {primary.name}
                {primary.is_panel && (
                  <span className="ml-2 text-xs px-2 py-0.5 rounded bg-purple-900 text-purple-300">
                    Panel
                  </span>
                )}
                {/* The legacy is_baseline badge was removed when reference
                    selection moved to ReferenceConfig (see Reference tab).
                    is_baseline is no longer consulted at runtime. */}
                {!keySet && (
                  <span className="ml-2 text-xs px-2 py-0.5 rounded bg-red-900 text-red-300">
                    No API Key
                  </span>
                )}
              </div>
              <div className="text-xs text-gray-400 mt-1">
                {primary.provider} / {primary.model_id}
              </div>
            </div>
          </div>
          <div className="text-right text-xs text-gray-400">
            {primary.input_cost_per_million === 0 && primary.output_cost_per_million === 0 ? (
              <div className="text-green-400 font-medium">Free</div>
            ) : (
              <div>
                In: ${primary.input_cost_per_million}/M | Out: ${primary.output_cost_per_million}/M
              </div>
            )}
            {!hasVariants && primary.reasoning_effort && (
              <div className="mt-1">Reasoning: {primary.reasoning_effort}</div>
            )}
          </div>
        </div>
        {hasVariants && (
          <div className="flex items-center gap-2 mt-3 ml-8">
            <span className="text-xs text-gray-500">Effort:</span>
            {members.map((m) => (
              <button
                key={m.id}
                onClick={(e) => {
                  e.stopPropagation();
                  toggleModel(m.id);
                }}
                className={`px-3 py-1 text-xs rounded-full border transition-colors ${getEffortChipClass(
                  m,
                  selectedModels.includes(m.id),
                )}`}
              >
                {getEffortLabel(m)}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  };

  const renderTierSection = (
    tier: string,
    families: ModelFamily[],
    title: string,
    titleClass: string,
    description: string,
    descClass: string,
  ) => {
    if (families.length === 0) return null;
    // Count of model IDs actually selected in this tier (not families).
    // With effort-level pickers, one family can contribute multiple selected
    // IDs (e.g. GPT-5.4 medium + high + xhigh = 3 selected models, 1 family).
    const tierMemberIds = new Set(families.flatMap((f) => f.members.map((m) => m.id)));
    const tierAllCount = tierMemberIds.size;
    const tierSelectedCount = selectedModels.filter((id) => tierMemberIds.has(id)).length;
    const isCollapsed = collapsed[tier];

    return (
      <section>
        <div className="flex items-center justify-between mb-2">
          <button
            onClick={() => toggleCollapse(tier)}
            className="flex items-center gap-2 text-left"
          >
            <span className="text-gray-400 text-sm w-4">
              {isCollapsed ? "+" : "-"}
            </span>
            <h2 className={`text-lg font-semibold ${titleClass}`}>
              {title} ({tierSelectedCount}/{tierAllCount})
            </h2>
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              selectTier(tier);
            }}
            className="px-3 py-1.5 text-sm bg-gray-800 hover:bg-gray-700 rounded"
          >
            {families.every((f) => selectedModels.includes(f.primary.id))
              ? "Deselect All"
              : "Select All"}
          </button>
        </div>
        <p className={`text-xs ${descClass} mb-3 ml-6`}>{description}</p>
        {!isCollapsed && (
          <div className="space-y-2">{families.map(renderModelFamily)}</div>
        )}
      </section>
    );
  };

  return (
    <div className="space-y-8">
      {/* API Keys */}
      <section>
        <h2 className="text-lg font-semibold mb-4">API Keys</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {Object.entries(PROVIDER_LABELS).map(([key, label]) => (
            <div key={key} className="bg-gray-900 rounded-lg p-4">
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm font-medium">{label}</label>
                {config.api_keys_set[key] ? (
                  <span className="text-xs px-2 py-0.5 rounded bg-green-900 text-green-300">
                    Set
                  </span>
                ) : (
                  <span className="text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-400">
                    Not set
                  </span>
                )}
              </div>
              <input
                type="password"
                placeholder={
                  config.api_keys_set[key]
                    ? config.api_keys[key] || "********"
                    : "Enter API key..."
                }
                value={keys[key] || ""}
                onChange={(e) => setKeys({ ...keys, [key]: e.target.value })}
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
              />
            </div>
          ))}
        </div>
        <div className="mt-4 flex items-center gap-4">
          <button
            onClick={saveKeys}
            disabled={saving}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded text-sm font-medium disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save API Keys"}
          </button>
          {msg && <span className="text-sm text-green-400">{msg}</span>}
        </div>
      </section>

      {/* Free first, Paid second, T2 third */}
      {renderTierSection(
        "tier1_free",
        tier1Free,
        "Tier 1 Free",
        "text-green-400",
        "Google Gemini via AI Studio - free API access with rate limits.",
        "text-green-500",
      )}

      {/* Selection persistence - save/restore the user's preferred model set */}
      <section className="bg-gray-900 rounded-lg p-4 border border-gray-800">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h3 className="text-sm font-medium">Selection</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              {selectedModels.length} model{selectedModels.length === 1 ? "" : "s"} currently selected.
              Each effort level (Low / Medium / High / XHigh) is its own model; the benchmark fans
              out one API call per selected model per prompt.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {msg && <span className="text-xs text-emerald-400">{msg}</span>}
            <button
              onClick={restoreDefault}
              className="text-xs px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 border border-gray-700"
              title="Restore the saved default selection"
            >
              Restore saved
            </button>
            <button
              onClick={saveSelectionAsDefault}
              disabled={saving || selectedModels.length === 0}
              className="text-xs px-3 py-1.5 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed"
              title="Save the current selection so it auto-loads on next app start"
            >
              {saving ? "Saving..." : "Save as default"}
            </button>
          </div>
        </div>
      </section>

      {renderTierSection(
        "tier1_paid",
        tier1Paid,
        "Tier 1 Paid",
        "",
        "OpenAI, Anthropic, xAI - paid API keys required.",
        "text-gray-400",
      )}

      {renderTierSection(
        "tier2",
        tier2,
        "Tier 2 - Other Providers",
        "text-gray-400",
        "Open-source models and other providers (Llama, Mistral, Gemma, DeepSeek, Qwen). Disabled by default - may require approval.",
        "text-yellow-500",
      )}
    </div>
  );
}
