// Embeds the live end-to-end journey matrix served by the backend.
export default function E2EMatrixTab() {
  return (
    <div className="-mx-6 -my-6">
      <iframe
        src="/eval/e2e-matrix"
        title="End-to-end journey matrix"
        style={{ width: "100%", height: "calc(100vh - 120px)", border: "none", background: "#0b0f19" }}
      />
      <p className="text-xs text-gray-500 px-6 py-2">
        Live full-journey view from <code>kg.e2e_eval_runs</code> and <code>kg.e2e_eval_results</code>.
      </p>
    </div>
  );
}
