// Embeds the bundled retrieval matrix snapshot served by the backend.
export default function MatrixTab() {
  return (
    <div className="-mx-6 -my-6">
      <iframe
        src="/eval/matrix"
        title="Retrieval matrix"
        style={{ width: "100%", height: "calc(100vh - 120px)", border: "none", background: "#0b0f19" }}
      />
      <p className="text-xs text-gray-500 px-6 py-2">
        CSV export:
        <a className="text-blue-400 hover:text-blue-300 ml-1" href="/eval/matrix.csv">/eval/matrix.csv</a>.
      </p>
    </div>
  );
}
