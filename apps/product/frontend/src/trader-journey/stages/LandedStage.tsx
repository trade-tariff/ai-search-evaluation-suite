import { useState } from "react";
import { api } from "../api";
import { Money } from "../components/Money";
import type { JourneyState, LandedResult } from "../types";

interface Props {
  state: JourneyState;
  update: (patch: Partial<JourneyState>) => void;
  onNext: () => void;
  onBack: () => void;
}

export default function LandedStage({ state, update, onNext, onBack }: Props) {
  const [additional, setAdditional] = useState(state.additionalChargesGbp);
  const [incidental, setIncidental] = useState(state.incidentalCostsToUkGbp);
  const [usePva, setUsePva] = useState(state.usePostponedVat);
  const [result, setResult] = useState<LandedResult | null>(state.landedResult);
  const [phase, setPhase] = useState<"edit" | "review" | "result">(state.landedResult ? "result" : "edit");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const customsValue = state.customsValueGbp ?? 0;
  const duty = state.dutyResult?.customs_duty_gbp ?? 0;
  const excise = state.dutyResult?.excise_duty_gbp ?? 0;
  const vatRate = state.dutyResult?.vat_rate ?? state.vatRate;

  function costsAreValid(): boolean {
    return Number.isFinite(incidental) && incidental >= 0 && Number.isFinite(additional) && additional >= 0;
  }

  async function compute() {
    if (!costsAreValid()) {
      setError("Enter each cost as a valid non-negative amount.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await api.landed({
        customs_value_gbp: customsValue,
        customs_duty_gbp: duty,
        excise_duty_gbp: excise,
        vat_rate: vatRate,
        additional_charges_gbp: additional,
        incidental_costs_to_uk_gbp: incidental,
        use_postponed_vat: usePva,
        low_value_import: state.dutyResult?.low_value_regime ?? null,
      });
      setResult(res);
      update({
        additionalChargesGbp: additional,
        incidentalCostsToUkGbp: incidental,
        usePostponedVat: usePva,
        landedResult: res,
        declarationResult: null,
        filingIntent: null,
      });
      setPhase("result");
    } catch (err: any) {
      setError("We could not calculate your import costs. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  function reworkImportCosts() {
    setResult(null);
    update({ landedResult: null, declarationResult: null, filingIntent: null });
    setPhase("edit");
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold mb-2">4. Import costs</h2>
        <p className="text-gray-400 mb-4">
          VAT is calculated on the customs value plus import duty, excise, and transport and
          handling costs to your first UK destination. Add your UK-side costs to see your full
          cost of importing in one place.{" "}
          <a href="https://www.gov.uk/goods-sent-from-abroad/tax-and-duty" target="_blank" rel="noreferrer" className="text-blue-400 underline hover:text-blue-300">Read about tax and duty on goods sent to the UK on GOV.UK (opens in new tab)</a>.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {phase === "edit" && (
        <div className="tj-card space-y-4">
          <div>
            <label className="tj-label">Transport and handling to your first UK destination (GBP)</label>
            <span className="tj-hint">
              Optional. Freight, insurance and handling costs up to your first UK destination. Included in the import VAT calculation.
            </span>
            <input
              type="number"
              inputMode="decimal"
              className="tj-input"
              value={incidental}
              onChange={(e) => setIncidental(Number(e.target.value))}
              min="0"
              step="0.01"
            />
          </div>
          <div>
            <label className="tj-label">Other UK costs after arrival (GBP)</label>
            <span className="tj-hint">
              Optional. Broker fees, port handling, onward UK transport and similar costs after arrival. Not part of the VAT calculation.
            </span>
            <input
              type="number"
              inputMode="decimal"
              className="tj-input"
              value={additional}
              onChange={(e) => setAdditional(Number(e.target.value))}
              min="0"
              step="0.01"
            />
          </div>
          <label className="flex items-center justify-between gap-3 border border-gray-700 px-3 py-2 text-sm">
            <span>I use postponed VAT accounting (account for import VAT on my VAT return)</span>
            <input
              type="checkbox"
              checked={usePva}
              onChange={(e) => setUsePva(e.target.checked)}
              className="h-4 w-4"
            />
          </label>
          <button
            onClick={() => {
              if (!costsAreValid()) {
                setError("Enter each cost as a valid non-negative amount.");
                return;
              }
              setError(null);
              setPhase("review");
            }}
            className="tj-btn"
          >
            Check your import cost details
          </button>
        </div>
        )}

        <div className={phase === "edit" ? "space-y-4" : "space-y-4 lg:col-span-2"}>
          {error && (
            <div className="border-l-4 border-red-500 bg-gray-900 p-4">
              <strong>There is a problem.</strong> {error}
            </div>
          )}

          {phase === "review" && (
            <div className="tj-card">
              <h3 className="text-lg font-bold mb-3">Check your import cost details</h3>
              <table className="w-full text-sm">
                <tbody>
                  {[
                    ["Customs value", customsValue],
                    ["Customs duty", duty],
                    ["Excise duty", excise],
                    ["VAT rate", 0],
                    ["Transport and handling to first UK destination", incidental],
                    ["Other UK costs after arrival", additional],
                  ].map(([label, value]) => (
                    <tr key={String(label)} className="border-b border-gray-800">
                      <td className="py-2 text-gray-400">{label}</td>
                      <td className="py-2 text-right font-mono">
                        {String(label).startsWith("VAT rate") ? `${vatRate}%` : <Money value={Number(value)} />}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-3 text-sm text-gray-400">
                Postponed VAT accounting: <span className="text-gray-200">{usePva ? "Yes" : "No"}</span>
              </p>
              <div className="flex justify-between mt-6">
                <button onClick={reworkImportCosts} className="tj-btn-secondary">&larr; Change charges</button>
                <button onClick={() => compute()} className="tj-btn" disabled={loading}>
                  {loading ? "Calculating..." : "Calculate import costs"}
                </button>
              </div>
            </div>
          )}

          {phase === "result" && result && (
            <div className="tj-card">
              <div className="flex items-start justify-between gap-4 mb-3">
                <h3 className="text-lg font-bold">Total import cost</h3>
                <button onClick={reworkImportCosts} className="tj-btn-secondary text-xs">
                  Change charges
                </button>
              </div>
              <div className="text-4xl font-bold tabular-nums mb-4 text-emerald-400">
                <Money value={result.total_landed_cost_gbp} />
              </div>
              {result.vat_treatment === "supply_vat" && (
                <div className="border-l-4 border-blue-500 bg-gray-900 p-4 mb-4 text-sm text-gray-200">
                  <strong>Consignments of GBP 135 or less:</strong> no customs duty; VAT is handled
                  at the point of sale. The VAT shown is charged by the seller when you buy the
                  goods, not at import.{" "}
                  <a href="https://www.gov.uk/goods-sent-from-abroad/tax-and-duty" target="_blank" rel="noreferrer" className="text-blue-400 underline hover:text-blue-300">Read about tax and duty on goods sent from abroad on GOV.UK (opens in new tab)</a>.
                </div>
              )}
              {result.vat_treatment === "postponed" && (
                <div className="border-l-4 border-blue-500 bg-gray-900 p-4 mb-4 text-sm">
                  <div className="flex justify-between gap-4">
                    <span className="text-gray-400">Cash due at border</span>
                    <span className="font-bold tabular-nums"><Money value={result.cash_at_border_gbp ?? 0} /></span>
                  </div>
                  <div className="flex justify-between gap-4 mt-1">
                    <span className="text-gray-400">Total tax liability</span>
                    <span className="font-bold tabular-nums"><Money value={result.total_tax_liability_gbp ?? 0} /></span>
                  </div>
                  <p className="mt-2 text-xs text-gray-400">
                    With postponed VAT accounting you account for import VAT on your VAT return, so
                    it is not paid at the border.
                  </p>
                </div>
              )}
              <table className="w-full text-sm">
                <tbody>
                  {Object.entries(result.breakdown).map(([k, v]) => (
                    <tr key={k} className={k === "total_landed_cost_gbp" ? "font-bold border-t-2 border-gray-600" : ""}>
                      <td className="py-1 text-gray-400">{prettyKey(k)}</td>
                      <td className="py-1 text-right tabular-nums">
                        <Money value={v} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {result.notes.length > 0 && (
                <ul className="mt-4 text-xs text-gray-400 list-disc list-inside space-y-1">
                  {result.notes.map((n, i) => <li key={i}>{n}</li>)}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="flex justify-between">
        <button onClick={onBack} className="tj-btn-secondary">
          &larr; Back
        </button>
        <div className="flex items-center gap-3">
          {(!result || phase !== "result") && (
            <span className="text-xs text-gray-500">Calculate your import costs to continue</span>
          )}
          <button
            onClick={onNext}
            className="tj-btn"
            disabled={!result || phase !== "result"}
            title={!result || phase !== "result" ? "Calculate your import costs to continue" : undefined}
          >
            Next: Declaration &rarr;
          </button>
        </div>
      </div>
    </div>
  );
}

function prettyKey(k: string): string {
  const labels: Record<string, string> = {
    customs_value_gbp: "Customs value (GBP)",
    customs_duty_gbp: "Customs duty (GBP)",
    excise_duty_gbp: "Excise duty (GBP)",
    incidental_costs_to_uk_gbp: "Transport and handling to first UK destination (GBP)",
    vat_taxable_amount_gbp: "Amount VAT is charged on (GBP)",
    vat_gbp: "VAT (GBP)",
    additional_charges_gbp: "Other UK costs after arrival (GBP)",
    cash_at_border_gbp: "Cash due at border (GBP)",
    total_tax_liability_gbp: "Total tax liability (GBP)",
    total_landed_cost_gbp: "Total import cost (GBP)",
  };
  if (labels[k]) return labels[k];
  return k.replace(/_/g, " ").replace(/gbp/i, "(GBP)").replace(/^./, (m) => m.toUpperCase());
}
