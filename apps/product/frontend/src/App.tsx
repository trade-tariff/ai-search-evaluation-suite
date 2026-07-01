import { useEffect, useState } from "react";
import { getConfig } from "./api";
import ConfigPanel from "./components/ConfigPanel";
import PromptsPanel from "./components/PromptsPanel";
import BenchmarkPanel from "./components/BenchmarkPanel";
import AnalysisPanel from "./components/AnalysisPanel";
import FinancialPanel from "./components/FinancialPanel";
import JudgePanel from "./components/JudgePanel";
import ReferencePanel from "./components/ReferencePanel";
import SimulatorPanel from "./components/SimulatorPanel";
import AtarPanel from "./components/AtarPanel";
import InterceptsPanel from "./components/InterceptsPanel";
import ComplexityPanel from "./components/ComplexityPanel";
import KnowledgePanel from "./components/KnowledgePanel";
import MatrixTab from "./components/MatrixTab";
import ExperimentsTab from "./components/ExperimentsTab";
import TraderJourneyTab from "./components/TraderJourneyTab";

const TABS = ["Trader Journey"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("Trader Journey");
  const [selectedPrompts, setSelectedPrompts] = useState<number[]>([]);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [opensearchLimit, setOpensearchLimit] = useState(80);

  // Hydrate saved default model selection so picks survive reloads.
  useEffect(() => {
    getConfig()
      .then((c) => {
        if (c.default_selected_model_ids && c.default_selected_model_ids.length > 0) {
          setSelectedModels(c.default_selected_model_ids);
        }
      })
      .catch(() => {});
  }, []);

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      <header className="border-b border-gray-800 px-6 py-4">
        <h1 className="text-xl font-semibold">
          Trade Tariff AI Assistant <span className="text-sm font-normal text-gray-400">(demo)</span>
        </h1>
      </header>

      <nav className="border-b border-gray-800 px-6 flex gap-1 max-w-full overflow-x-auto">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`shrink-0 px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
              tab === t
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-gray-400 hover:text-gray-200"
            }`}
          >
            {t}
          </button>
        ))}
      </nav>

      <main className="p-6 max-w-7xl mx-auto">
        <div className={tab === "Configuration" ? "" : "hidden"}>
          <ConfigPanel
            selectedModels={selectedModels}
            onModelsChange={setSelectedModels}
          />
        </div>
        <div className={tab === "Prompts" ? "" : "hidden"}>
          <PromptsPanel
            selected={selectedPrompts}
            onSelectionChange={setSelectedPrompts}
            opensearchLimit={opensearchLimit}
            onOpensearchLimitChange={setOpensearchLimit}
          />
        </div>
        <div className={tab === "ATaR" ? "" : "hidden"}>
          <AtarPanel />
        </div>
        <div className={tab === "Search References" ? "" : "hidden"}>
          <ReferencePanel />
        </div>
        <div className={tab === "Simulator" ? "" : "hidden"}>
          <SimulatorPanel />
        </div>
        <div className={tab === "Judge" ? "" : "hidden"}>
          <JudgePanel />
        </div>
        <div className={tab === "Benchmark" ? "" : "hidden"}>
          <BenchmarkPanel
            promptIndices={selectedPrompts}
            modelIds={selectedModels}
            opensearchLimit={opensearchLimit}
          />
        </div>
        {tab === "Analysis" && <AnalysisPanel />}
        {tab === "Financial" && <FinancialPanel />}
        {tab === "Intercepts" && <InterceptsPanel />}
        {tab === "Complexity" && <ComplexityPanel />}
        {tab === "Knowledge" && <KnowledgePanel />}
        {tab === "Experiments" && <ExperimentsTab />}
        {tab === "Trader Journey" && <TraderJourneyTab />}
        {tab === "Matrix" && <MatrixTab />}
      </main>
    </div>
  );
}
