import { useEffect, useRef, useState } from "react";
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

// Four entry points. Experiment leads: try new things (retrieval lab,
// rulings, intercepts, complexity, knowledge graph). Results = read the
// measurements: the gold-set eval, where prompts have known correct
// answers, so we score retrieval + Q&A directly against gold (plus cost,
// time and rank). Model Comparison lives here too - the Gold Eval jobs take
// a SINGLE model and sweep pipeline config (persona, question mode,
// retrieval base), so the fan-out is the only thing that puts models
// head-to-head, and its shared fact store is what keeps that comparison
// honest. Configure = set up the pipeline (rendered as a dropdown so the
// bar does not overflow). Legacy = the reference-model scoring path
// (reference panel, consensus, LLM judge) - superseded by gold mode, but
// still the right tool for prompts WITHOUT gold answers and for judge
// diagnostics. Tab ids are stable internal keys (panel switching below);
// labels are what the user sees. Domain terms the team already uses
// (ATaR, Intercepts) stay recognisable in the label.
const NAV_GROUPS = [
  {
    label: "Experiment",
    tabs: [
      { id: "Experiments", label: "Retrieval Lab" },
      { id: "ATaR", label: "Rulings (ATaR)" },
      { id: "Intercepts", label: "Intercepts" },
      { id: "Complexity", label: "Complexity" },
      { id: "Knowledge", label: "Knowledge Graph" },
    ],
  },
  {
    label: "Results",
    tabs: [
      { id: "E2E Matrix", label: "Gold Eval - E2E" },
      { id: "Q&A Matrix", label: "Gold Eval - Q&A" },
      { id: "Benchmark", label: "Model Comparison" },
      { id: "Matrix", label: "Retrieval Matrix" },
      { id: "Financial", label: "Costs" },
    ],
  },
  {
    label: "Configure",
    collapsed: true,
    tabs: [
      { id: "Configuration", label: "Models & Keys" },
      { id: "Prompts", label: "Gold Prompts" },
      { id: "Search References", label: "Reference Model" },
      { id: "Simulator", label: "Trader Simulator" },
      { id: "Judge", label: "LLM Judge" },
    ],
  },
  {
    label: "Legacy",
    tabs: [
      { id: "Analysis", label: "Benchmark Results" },
    ],
  },
] as const;
type Tab = (typeof NAV_GROUPS)[number]["tabs"][number]["id"];

export default function App() {
  const [tab, setTab] = useState<Tab>("Experiments");
  const [selectedPrompts, setSelectedPrompts] = useState<number[]>([]);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [opensearchLimit, setOpensearchLimit] = useState(80);
  const [configOpen, setConfigOpen] = useState(false);
  const configMenuRef = useRef<HTMLDivElement>(null);

  // Close the Configure dropdown on outside click or Escape.
  useEffect(() => {
    if (!configOpen) return;
    const onMouseDown = (e: MouseEvent) => {
      if (configMenuRef.current && !configMenuRef.current.contains(e.target as Node)) {
        setConfigOpen(false);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setConfigOpen(false);
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [configOpen]);

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

      <nav className="border-b border-gray-800 px-6 flex max-w-full" aria-label="Primary">
        {NAV_GROUPS.map((group, gi) => {
          const isCollapsed = "collapsed" in group && group.collapsed;
          const groupActive = group.tabs.some((t) => t.id === tab);
          return (
            <div
              key={group.label}
              className={`shrink-0 flex flex-col ${gi > 0 ? "ml-3 pl-3 border-l border-gray-800/80" : ""}`}
            >
              <span className="px-1 pt-1.5 text-[10px] font-semibold uppercase tracking-widest text-gray-600 select-none">
                {group.label}
              </span>
              {isCollapsed ? (
                <div className="relative flex" ref={configMenuRef}>
                  <button
                    onClick={() => setConfigOpen((o) => !o)}
                    aria-haspopup="menu"
                    aria-expanded={configOpen}
                    className={`shrink-0 flex items-center gap-1 px-3 pb-2.5 pt-1 text-sm font-medium border-b-2 transition-colors cursor-pointer ${
                      groupActive
                        ? "border-blue-500 text-blue-400"
                        : "border-transparent text-gray-400 hover:text-gray-200"
                    }`}
                  >
                    {group.label}
                    <svg
                      className={`h-3 w-3 transition-transform ${configOpen ? "rotate-180" : ""}`}
                      viewBox="0 0 12 12"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      aria-hidden="true"
                    >
                      <path d="M2.5 4.5 L6 8 L9.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  {configOpen && (
                    <div
                      role="menu"
                      className="absolute left-0 top-full z-50 min-w-[11rem] bg-gray-900 border border-gray-800 rounded shadow-lg"
                    >
                      {group.tabs.map((t) => (
                        <button
                          key={t.id}
                          role="menuitem"
                          onClick={() => {
                            setTab(t.id);
                            setConfigOpen(false);
                          }}
                          className={`block w-full text-left px-3 py-2 text-sm hover:bg-gray-800 cursor-pointer ${
                            tab === t.id ? "text-blue-400" : "text-gray-300"
                          }`}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex gap-1">
                  {group.tabs.map((t) => (
                    <button
                      key={t.id}
                      onClick={() => setTab(t.id)}
                      aria-current={tab === t.id ? "page" : undefined}
                      className={`shrink-0 px-3 pb-2.5 pt-1 text-sm font-medium border-b-2 transition-colors cursor-pointer ${
                        tab === t.id
                          ? "border-blue-500 text-blue-400"
                          : "border-transparent text-gray-400 hover:text-gray-200"
                      }`}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
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
