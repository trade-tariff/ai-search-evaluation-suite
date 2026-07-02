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
import E2EMatrixTab from "./components/E2EMatrixTab";
import QAMatrixTab from "./components/QAMatrixTab";
import ExperimentsTab from "./components/ExperimentsTab";

// Three entry points: try new things (Experiment), run/read the measurements
// (Evaluate), set up the pipeline (Configure). Tab components are unchanged -
// this is presentation-only grouping.
const NAV_GROUPS = [
  { label: "Experiment", tabs: ["Experiments", "ATaR", "Intercepts", "Complexity", "Knowledge"] },
  { label: "Evaluate", tabs: ["Benchmark", "Analysis", "Matrix", "Q&A Matrix", "E2E Matrix", "Financial"] },
  { label: "Configure", tabs: ["Configuration", "Prompts", "Search References", "Simulator", "Judge"] },
] as const;
type Tab = (typeof NAV_GROUPS)[number]["tabs"][number];

export default function App() {
  const [tab, setTab] = useState<Tab>("Experiments");
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
          AI Search Evaluation Suite
        </h1>
      </header>

      <nav className="border-b border-gray-800 px-6 flex max-w-full overflow-x-auto" aria-label="Primary">
        {NAV_GROUPS.map((group, gi) => (
          <div
            key={group.label}
            className={`shrink-0 flex flex-col ${gi > 0 ? "ml-3 pl-3 border-l border-gray-800/80" : ""}`}
          >
            <span className="px-1 pt-1.5 text-[10px] font-semibold uppercase tracking-widest text-gray-600 select-none">
              {group.label}
            </span>
            <div className="flex gap-1">
              {group.tabs.map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  aria-current={tab === t ? "page" : undefined}
                  className={`shrink-0 px-3 pb-2.5 pt-1 text-sm font-medium border-b-2 transition-colors cursor-pointer ${
                    tab === t
                      ? "border-blue-500 text-blue-400"
                      : "border-transparent text-gray-400 hover:text-gray-200"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
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
        {tab === "Matrix" && <MatrixTab />}
        {tab === "Q&A Matrix" && <QAMatrixTab />}
        {tab === "E2E Matrix" && <E2EMatrixTab />}
      </main>
    </div>
  );
}
