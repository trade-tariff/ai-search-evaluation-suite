import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Money } from "../components/Money";
import type {
  CommodityRequirements,
  Country,
  DutyInputInferenceResult,
  DutyResult,
  DutyWarning,
  JourneyState,
  MeursingInputs,
} from "../types";

interface Props {
  state: JourneyState;
  update: (patch: Partial<JourneyState>) => void;
  onNext: () => void;
  onBack: () => void;
}

// Sub-wizard steps. Order is the canonical flow; some are skipped based on
// the commodity's requirements (no excise for non-alcoholic Ch 22, etc).
type Sub =
  | "date"
  | "destination"
  | "country"
  | "proof"
  | "quantity"
  | "meursing"
  | "excise_abv"
  | "excise_volume"
  | "excise_sticks"
  | "excise_weight"
  | "excise_retail"
  | "excise_spr"
  | "excise_draught"
  | "vat"
  | "review"
  | "result";

const SUB_LABELS: Record<Sub, string> = {
  date: "Import date",
  destination: "Import destination",
  country: "Country of origin",
  proof: "Proof of origin",
  quantity: "Supplementary units",
  meursing: "Additional code",
  excise_abv: "Excise: ABV",
  excise_volume: "Excise: volume",
  excise_sticks: "Excise: cigarettes",
  excise_weight: "Excise: net weight",
  excise_retail: "Excise: retail value",
  excise_spr: "Excise: Small Producer Relief",
  excise_draught: "Excise: Draught Relief",
  vat: "VAT rate",
  review: "Review",
  result: "Result",
};

function localDateInputValue(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

// Humanize backend provenance strings for trader-facing display.
const prettySource = (s?: string) =>
  s?.replace(/LLM prefill extractor/i, "Suggested by AI from your description");

export default function DutyStage({ state, update, onNext, onBack }: Props) {
  const commodity = state.finalCommodity;

  const [reqs, setReqs] = useState<CommodityRequirements | null>(null);
  const [countries, setCountries] = useState<Country[]>([]);
  const [sub, setSub] = useState<Sub>(state.dutyResult ? "result" : "date");
  const [importDate, setImportDate] = useState<string>(
    state.importDate ?? localDateInputValue()
  );
  const [destination] = useState<"GB" | "XI">("GB");
  const [country, setCountry] = useState(state.countryOfOrigin);
  const [hasProof, setHasProof] = useState(state.hasProofOfOrigin);
  const [units, setUnits] = useState<string>(
    state.quantityUnits !== null ? String(state.quantityUnits) : ""
  );
  const [abv, setAbv] = useState<string>(
    state.abv !== null ? String(state.abv) : ""
  );
  const [exciseVolume, setExciseVolume] = useState<string>(
    state.exciseVolumeLitres !== null ? String(state.exciseVolumeLitres) : ""
  );
  // Tobacco excise inputs (not persisted in JourneyState - re-asked on revisit).
  const [sticks, setSticks] = useState<string>("");
  const [retailValue, setRetailValue] = useState<string>("");
  const [netWeight, setNetWeight] = useState<string>("");
  const [isSpr, setIsSpr] = useState(state.isSmallProducer);
  const [isDraught, setIsDraught] = useState(state.isDraught);
  const [meursing, setMeursing] = useState<MeursingInputs>(
    state.meursingInputs || state.selectedExample?.seed?.meursing_inputs || {
      starch_glucose_pct: null,
      sucrose_invert_isoglucose_pct: null,
      milk_fat_pct: null,
      milk_protein_pct: null,
    }
  );
  // null = "use the rate for this commodity" - sent as vat_rate: null so the
  // backend seeds the commodity's real VAT measure. 0 only when explicitly picked.
  const [vatRate, setVatRate] = useState<number | null>(
    state.dutyResult ? state.vatRate : null
  );
  const [result, setResult] = useState<DutyResult | null>(state.dutyResult);
  const [inference, setInference] = useState<DutyInputInferenceResult | null>(state.dutyInference);
  const [explainer, setExplainer] = useState<string | null>(state.dutyExplainerText);
  const [loading, setLoading] = useState(false);
  const [prefillLoading, setPrefillLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const touchedFields = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (commodity) {
      api.dutyRequirements(commodity.commodity_code)
        .then(setReqs)
        .catch((err) => {
          setError(`Could not load duty requirements: ${err instanceof Error ? err.message : String(err)}`);
        });
      setPrefillLoading(true);
      api.dutyInfer({
        commodity_code: commodity.commodity_code,
        query: state.query,
        qa_history: state.qaHistory,
        customs_value_gbp: state.customsValueGbp,
        known_inputs: knownDutyInputs(state),
      })
        .then((inf) => {
          setInference(inf);
          const got = inf.inferred || {};
          if (!touchedFields.current.has("country") && !country && typeof got.country_of_origin === "string") setCountry(got.country_of_origin);
          if (!touchedFields.current.has("quantity") && units === "" && typeof got.quantity_units === "number") setUnits(String(got.quantity_units));
          if (!touchedFields.current.has("quantity") && !state.quantityUnitType && typeof got.quantity_unit_type === "string") {
            update({ quantityUnitType: got.quantity_unit_type });
          }
          if (!touchedFields.current.has("abv") && abv === "" && typeof got.abv === "number") setAbv(String(got.abv));
          if (!touchedFields.current.has("excise_volume") && exciseVolume === "" && typeof got.excise_volume_litres === "number") setExciseVolume(String(got.excise_volume_litres));
          if (!touchedFields.current.has("proof") && typeof got.has_proof_of_origin === "boolean") setHasProof(got.has_proof_of_origin);
          if (!touchedFields.current.has("excise_spr") && typeof got.is_small_producer === "boolean") setIsSpr(got.is_small_producer);
          if (!touchedFields.current.has("excise_draught") && typeof got.is_draught === "boolean") setIsDraught(got.is_draught);
          // vat_rate is deliberately not prefilled from inference: null means the
          // backend seeds the commodity's real VAT measure, which is authoritative.
          update({ dutyInference: inf });
        })
        .catch((err) => {
          setError(`Could not prefill duty inputs. You can still answer manually. ${err instanceof Error ? err.message : String(err)}`);
        })
        .finally(() => setPrefillLoading(false));
    }
    api.countries()
      .then(setCountries)
      .catch((err) => {
        setError(`Could not load country list: ${err instanceof Error ? err.message : String(err)}`);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [commodity?.commodity_code]);

  function markTouched(field: string) {
    touchedFields.current.add(field);
  }

  // Which substeps are active for this commodity + country combo.
  const activeSteps: Sub[] = useMemo(() => {
    const skipped = new Set(inference?.skipped_questions || []);
    if (!reqs) return result ? ["date", "result"] : ["date"];
    const countryHasPref = countryHasAnyPreference(country, countries, commodity?.commodity_code, reqs);
    const arr: Sub[] = ["date"];
    if (!skipped.has("country") || !country) arr.push("country");
    if (reqs.has_any_preference && countryHasPref && !skipped.has("proof")) arr.push("proof");
    if (reqs.needs_supplementary_units && (!skipped.has("quantity") || !units)) arr.push("quantity");
    if (reqs.needs_meursing_code) arr.push("meursing");
    if (reqs.needs_excise) {
      const cat = reqs.excise_category ?? "alcohol";
      if (cat === "tobacco") {
        const wanted = new Set(reqs.excise_required_inputs ?? []);
        if (wanted.has("sticks")) arr.push("excise_sticks");
        if (wanted.has("net_weight_kg")) arr.push("excise_weight");
        if (wanted.has("retail_value_gbp")) arr.push("excise_retail");
      } else if (cat === "fuel") {
        if (!skipped.has("excise_volume") && !canUseQuantityAsExciseVolume(reqs.supplementary_unit_type, units, exciseVolume)) {
          arr.push("excise_volume");
        }
      } else {
        if (!skipped.has("excise_abv") || !abv) arr.push("excise_abv");
        if (!skipped.has("excise_volume") && !canUseQuantityAsExciseVolume(reqs.supplementary_unit_type, units, exciseVolume)) {
          arr.push("excise_volume");
        }
        if (!skipped.has("excise_spr")) arr.push("excise_spr");
        if (!skipped.has("excise_draught")) arr.push("excise_draught");
      }
    }
    if (!skipped.has("vat")) arr.push("vat");
    arr.push("review");
    if (result) arr.push("result");
    return arr;
  }, [reqs, inference, country, countries, commodity?.commodity_code, result, units, abv, exciseVolume]);

  const prefilledRows = useMemo(() => {
    if (!inference?.skipped_questions.length) return [];
    const quantityUnitType = reqs?.supplementary_unit_type || state.quantityUnitType || "";
    return inference.skipped_questions.map((key) => {
      const field = fieldForSkippedKey(key);
      return {
        key,
        label: labelForSkippedKey(key),
        value: valueForSkippedKey(key, {
          importDate,
          destination,
          country,
          hasProof,
          units,
          quantityUnitType,
          abv,
          exciseVolume,
          isSpr,
          isDraught,
          meursing,
          vatRate,
          defaultVatRate: reqs?.default_vat_rate ?? null,
        }),
        source: field ? inference.sources?.[field] : undefined,
      };
    });
  }, [
    inference,
    reqs?.supplementary_unit_type,
    reqs?.default_vat_rate,
    state.quantityUnitType,
    importDate,
    destination,
    country,
    hasProof,
    units,
    abv,
    exciseVolume,
    isSpr,
    isDraught,
    meursing,
    vatRate,
  ]);

  function nextSub() {
    const i = activeSteps.indexOf(sub);
    if (i === -1) {
      setSub("review");
      return;
    }
    if (i < activeSteps.length - 1) setSub(activeSteps[i + 1]);
  }
  function prevSub() {
    const i = activeSteps.indexOf(sub);
    if (i === -1) {
      setSub("review");
      return;
    }
    if (i > 0) setSub(activeSteps[i - 1]);
    else onBack();
  }

  // overrideCommodity: re-run for a child code picked from a "needs more
  // detail" result - updates finalCommodity as part of the same calculation.
  async function calculate(overrideCommodity?: { commodity_code: string; description: string }) {
    const target = overrideCommodity ?? commodity;
    if (!target || !country || state.customsValueGbp === null || !reqs) {
      setError("Complete the required duty inputs before calculating.");
      return;
    }
    const exciseCategory = reqs.excise_category ?? "alcohol";
    const exciseVolumeForCalc = exciseVolume || (reqs.supplementary_unit_type === "LTR" ? units : "");
    if (reqs.needs_supplementary_units && (units === "" || Number(units) <= 0 || !Number.isFinite(Number(units)))) {
      setError("Enter a quantity greater than zero.");
      return;
    }
    if (reqs.needs_excise && exciseCategory === "alcohol" && (abv === "" || Number(abv) <= 0 || !Number.isFinite(Number(abv)))) {
      setError("Enter the alcohol by volume percentage before calculating excise.");
      return;
    }
    if (reqs.needs_excise && exciseCategory !== "tobacco" && (exciseVolumeForCalc === "" || Number(exciseVolumeForCalc) <= 0 || !Number.isFinite(Number(exciseVolumeForCalc)))) {
      setError("Enter the product volume in litres before calculating excise.");
      return;
    }
    if (reqs.needs_meursing_code && !meursingComplete(meursing)) {
      setError("Enter all Meursing/additional-code composition percentages before calculating.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      // Tobacco inputs are sent as null when blank - the backend annotates
      // with an excise_missing warning rather than us blocking the calculation.
      const excise_inputs = !reqs.needs_excise
        ? null
        : exciseCategory === "tobacco"
        ? {
            sticks: sticks === "" ? null : Number(sticks),
            retail_value_gbp: retailValue === "" ? null : Number(retailValue),
            net_weight_kg: netWeight === "" ? null : Number(netWeight),
          }
        : exciseCategory === "fuel"
        ? { volume_litres: Number(exciseVolumeForCalc) || 0 }
        : {
            abv: Number(abv) || 0,
            volume_litres: Number(exciseVolumeForCalc) || 0,
            is_small_producer: isSpr,
            is_draught: isDraught,
          };
      const dRes = await api.duty({
        commodity_code: target.commodity_code,
        country_of_origin: country,
        customs_value_gbp: state.customsValueGbp,
        import_destination: destination,
        import_date: importDate,
        quantity_units: units === "" ? null : Number(units),
        quantity_unit_type: reqs.supplementary_unit_type,
        has_proof_of_origin: hasProof,
        excise_inputs,
        meursing_inputs: reqs.needs_meursing_code ? meursing : null,
        // null = backend seeds the commodity's real VAT measure (type 305).
        vat_rate: vatRate,
      });
      setResult(dRes);
      // Fire-and-forget the AI explainer; show a placeholder until it arrives.
      setExplainer(null);
      update({
        ...(overrideCommodity ? { finalCommodity: overrideCommodity } : {}),
        importDestination: destination,
        importDate,
        countryOfOrigin: country,
        hasProofOfOrigin: hasProof,
        quantityUnits: units === "" ? null : Number(units),
        quantityUnitType: reqs.supplementary_unit_type,
        abv: abv === "" ? null : Number(abv),
        exciseVolumeLitres: exciseVolume === "" ? null : Number(exciseVolume),
        isSmallProducer: isSpr,
        isDraught,
        meursingInputs: reqs.needs_meursing_code ? meursing : null,
        // Store the resolved rate (seeded by the backend when vatRate is null)
        // so downstream stages (Import costs) use the real figure.
        vatRate: dRes.vat_rate,
        dutyResult: dRes,
        dutyExplainerText: null,
        landedResult: null,
        declarationResult: null,
        filingIntent: null,
      });
      api.dutyExplain(dRes)
        .then((e) => {
          setExplainer(e.text);
          update({ dutyExplainerText: e.text });
        })
        .catch(() => {
          const text = "Plain-English summary is unavailable; the calculated duty breakdown above is still complete.";
          setExplainer(text);
          update({ dutyExplainerText: text });
        });
      setSub("result");
    } catch (err: any) {
      setError(err.message ?? String(err));
    } finally {
      setLoading(false);
    }
  }

  function reworkDutyInputs() {
    setResult(null);
    setExplainer(null);
    update({
      dutyResult: null,
      dutyExplainerText: null,
      landedResult: null,
      declarationResult: null,
      filingIntent: null,
    });
    setSub("review");
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold mb-2">3. Duty details</h2>
        <p className="text-gray-400 mb-4">
          Answer a few short questions about your import - when it arrives, where it
          is from, and how much you are bringing in - and we will work out the customs
          duty, excise and VAT rate that apply. The next step turns this into your
          total import cost.
        </p>
      </div>

      {commodity && state.customsValueGbp !== null && (
        <div className="border-l-4 border-blue-500 bg-gray-900 p-3 text-sm flex flex-wrap gap-x-6 gap-y-1">
          <span>
            <span className="text-gray-400">Commodity:</span>{" "}
            <strong className="font-mono">{commodity.commodity_code}</strong>
          </span>
          <span>
            <span className="text-gray-400">Customs value:</span>{" "}
            <strong><Money value={state.customsValueGbp} /></strong>
          </span>
          {country && (
            <span>
              <span className="text-gray-400">Origin:</span> <strong>{country}</strong>
            </span>
          )}
        </div>
      )}

      <SubProgress current={sub} active={activeSteps} onJump={(s) => setSub(s)} />

      {prefillLoading && (
        <div className="border-l-4 border-blue-500 bg-gray-900 p-3 text-sm text-gray-300">
          Filling in answers from your description and earlier steps...
        </div>
      )}

      {inference && !prefillLoading && prefilledRows.length > 0 && (
        <div className="border-l-4 border-emerald-500 bg-gray-900 p-3 text-sm text-gray-300">
          <div className="mb-2 font-semibold text-gray-100">Answers we filled in for you</div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {prefilledRows.map((row) => (
              <div key={row.key} className="border border-gray-700 bg-gray-950 p-2">
                <div className="text-xs uppercase tracking-wider text-gray-500">{row.label}</div>
                <div className="mt-0.5 font-mono text-gray-100">{row.value || "-"}</div>
                {row.source && <div className="mt-1 text-xs text-emerald-400">{prettySource(row.source)}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      {error && (
        <div className="border-l-4 border-red-500 bg-gray-900 p-4">
          <strong>Error:</strong> {error}
        </div>
      )}

      <div className="tj-card">
        {sub === "date" && (
          <SubDate
            value={importDate}
            onChange={(v: string) => {
              markTouched("date");
              setImportDate(v);
            }}
            canContinue={Boolean(reqs)}
            continueLabel={reqs ? "Continue" : "Loading duty data..."}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "destination" && (
          <SubDestination onContinue={nextSub} onBack={prevSub} />
        )}
        {sub === "country" && (
          <SubCountry
            value={country}
            countries={countries}
            onChange={(v: string) => {
              markTouched("country");
              setCountry(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "proof" && (
          <SubProof
            value={hasProof}
            onChange={(v: boolean) => {
              markTouched("proof");
              setHasProof(v);
            }}
            country={country}
            countries={countries}
            commodityCode={commodity?.commodity_code ?? ""}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "quantity" && reqs && (
          <SubQuantity
            value={units}
            unitType={reqs.supplementary_unit_type!}
            onChange={(v: string) => {
              markTouched("quantity");
              setUnits(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "meursing" && (
          <SubMeursing
            value={meursing}
            onChange={(v: MeursingInputs) => {
              markTouched("meursing");
              setMeursing(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "excise_abv" && (
          <SubExciseAbv
            value={abv}
            onChange={(v: string) => {
              markTouched("abv");
              setAbv(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "excise_volume" && (
          <SubExciseVolume
            value={exciseVolume}
            category={reqs?.excise_category ?? "alcohol"}
            onChange={(v: string) => {
              markTouched("excise_volume");
              setExciseVolume(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "excise_sticks" && (
          <SubExciseSticks
            value={sticks}
            onChange={(v: string) => {
              markTouched("excise_sticks");
              setSticks(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "excise_weight" && (
          <SubExciseWeight
            value={netWeight}
            onChange={(v: string) => {
              markTouched("excise_weight");
              setNetWeight(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "excise_retail" && (
          <SubExciseRetail
            value={retailValue}
            onChange={(v: string) => {
              markTouched("excise_retail");
              setRetailValue(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "excise_spr" && (
          <SubExciseSpr
            value={isSpr}
            onChange={(v: boolean) => {
              markTouched("excise_spr");
              setIsSpr(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "excise_draught" && (
          <SubExciseDraught
            value={isDraught}
            onChange={(v: boolean) => {
              markTouched("excise_draught");
              setIsDraught(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "vat" && (
          <SubVat
            value={vatRate}
            defaultVatRate={reqs?.default_vat_rate ?? null}
            onChange={(v: number | null) => {
              markTouched("vat");
              setVatRate(v);
            }}
            onContinue={nextSub}
            onBack={prevSub}
          />
        )}
        {sub === "review" && (
          <SubReview
            state={{
              importDate,
              destination,
              country,
              hasProof,
              units,
              abv,
              exciseVolume,
              sticks,
              retailValue,
              netWeight,
              isSpr,
              isDraught,
              meursing,
              vatRate,
            }}
            reqs={reqs}
            inference={inference}
            showProof={activeSteps.includes("proof")}
            loading={loading}
            onCalculate={() => calculate()}
            onBack={prevSub}
            onJump={setSub}
          />
        )}
        {sub === "result" && result && (
          <SubResult
            result={result}
            explainer={explainer}
            loading={loading}
            onNext={onNext}
            onEdit={reworkDutyInputs}
            onPickChild={(child) => calculate(child)}
          />
        )}
      </div>
    </div>
  );
}

// --- Sub-step sub-components -------------------------------------------

function SubProgress({
  current,
  active,
  onJump,
}: {
  current: Sub;
  active: Sub[];
  onJump: (s: Sub) => void;
}) {
  const currentIdx = active.indexOf(current);
  return (
    <ol className="flex flex-wrap gap-1 text-xs">
      {active.map((s, i) => {
        const isCurrent = s === current;
        const isPast = i < currentIdx;
        return (
          <li key={s}>
            <button
              onClick={() => isPast && onJump(s)}
              className={`px-2 py-1 border ${
                isCurrent
                  ? "border-gray-600 bg-blue-600 text-white font-semibold"
                  : isPast
                  ? "border-emerald-600 text-emerald-400 hover:bg-gray-900 cursor-pointer"
                  : "border-gray-700 text-gray-400"
              }`}
              disabled={!isPast}
            >
              {i + 1}. {SUB_LABELS[s]}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function NavButtons({
  onBack,
  onContinue,
  canContinue = true,
  continueLabel = "Continue",
}: {
  onBack: () => void;
  onContinue: () => void;
  canContinue?: boolean;
  continueLabel?: string;
}) {
  return (
    <div className="flex justify-between mt-6">
      <button className="tj-btn-secondary" onClick={onBack}>
        &larr; Back
      </button>
      <button className="tj-btn" onClick={onContinue} disabled={!canContinue}>
        {continueLabel}
      </button>
    </div>
  );
}

function SubDate({ value, onChange, onContinue, onBack, canContinue = true, continueLabel = "Continue" }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">When are the goods being imported?</h3>
      <p className="tj-hint">
        The duty rate that applies depends on the date the goods enter the UK. For most
        traders this is today or in the next few weeks. Some measures (anti-dumping,
        quotas, suspensions) come in and out of force on specific dates.
      </p>
      <label className="tj-label mt-3" htmlFor="duty-import-date">Import date</label>
      <input
        id="duty-import-date"
        type="date"
        className="tj-input max-w-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={canContinue} continueLabel={continueLabel} />
    </>
  );
}

function SubDestination({ onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">Where in the UK are the goods being imported to?</h3>
      <p className="tj-hint">
        The duty regime is different for Northern Ireland because of the Windsor Framework
        and the UK Internal Market Scheme. This service currently covers imports into
        England, Scotland and Wales only.
      </p>
      <div className="space-y-2 mt-3">
        <label className="flex items-center gap-2">
          <input type="radio" checked readOnly className="h-4 w-4" />
          <span>England, Scotland or Wales (GB)</span>
        </label>
        <label className="flex items-center gap-2 opacity-50">
          <input type="radio" disabled className="h-4 w-4" />
          <span>Northern Ireland (XI) - coming in a later iteration</span>
        </label>
      </div>
      <NavButtons onBack={onBack} onContinue={onContinue} />
    </>
  );
}

function SubCountry({ value, countries, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">What country are the goods from?</h3>
      <p className="tj-hint">
        The country of origin is where the goods were made or substantially transformed,
        not where they shipped from. The UK's preference agreements with that country
        determine the duty rate.
      </p>
      <label className="tj-label mt-3" htmlFor="duty-country-of-origin">Country of origin</label>
      <select
        id="duty-country-of-origin"
        className="tj-input max-w-md"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">-- Select --</option>
        {countries.map((c: Country) => (
          <option key={c.code} value={c.code}>
            {c.name} ({c.code})
          </option>
        ))}
      </select>
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={!!value} />
    </>
  );
}

function SubProof({ value, onChange, country, countries, commodityCode, onContinue, onBack }: any) {
  const c = countries.find((x: Country) => x.code === country);
  return (
    <>
      <h3 className="text-xl font-bold mb-2">Do you have a valid proof of origin?</h3>
      <p className="tj-hint">
        The UK may offer a reduced (preferential) duty rate for goods made in {c?.name}. To pay the lower
        preferential duty rate on {commodityCode} instead of the standard rate, you (or the
        exporter) must hold a valid proof of origin: a statement on invoice,
        EUR.1 movement certificate, REX number, or a certificate of origin
        depending on the agreement. HMRC can ask to see it.{" "}
        <a href="https://www.gov.uk/guidance/check-your-goods-meet-the-rules-of-origin" target="_blank" rel="noreferrer" className="text-blue-400 underline">Check the rules of origin on GOV.UK (opens in new tab)</a>.
      </p>
      <div className="space-y-2 mt-3">
        <label className="flex items-center gap-2">
          <input type="radio" checked={value} onChange={() => onChange(true)} className="h-4 w-4" />
          <span>Yes - I have a valid proof of origin</span>
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" checked={!value} onChange={() => onChange(false)} className="h-4 w-4" />
          <span>No - or I'm not sure</span>
        </label>
      </div>
      <NavButtons onBack={onBack} onContinue={onContinue} />
    </>
  );
}

function SubQuantity({ value, unitType, onChange, onContinue, onBack }: any) {
  const unitLabel: Record<string, string> = {
    PR: "pairs",
    LTR: "litres",
    NAR: "items",
    LPA: "litres of pure alcohol",
    KGM: "kilograms",
    HLT: "hectolitres",
  };
  return (
    <>
      <h3 className="text-xl font-bold mb-2">How much are you importing?</h3>
      <p className="tj-hint">
        Some duty measures (especially excise and anti-dumping) are charged per unit
        rather than on the customs value. For this commodity, the unit is {unitLabel[unitType] ?? unitType}.
      </p>
      <label className="tj-label mt-3">Quantity ({unitLabel[unitType] ? `${unitType} - ${unitLabel[unitType]}` : unitType})</label>
      <input
        type="number"
        inputMode="decimal"
        className="tj-input max-w-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        min="0"
        step="0.01"
        autoFocus
      />
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={value !== "" && Number(value) > 0} />
    </>
  );
}

function SubMeursing({ value, onChange, onContinue, onBack }: {
  value: MeursingInputs;
  onChange: (v: MeursingInputs) => void;
  onContinue: () => void;
  onBack: () => void;
}) {
  function setField(key: keyof MeursingInputs, raw: string) {
    onChange({ ...value, [key]: raw === "" ? null : Number(raw), additional_code: null });
  }
  const complete = meursingComplete(value);
  const lookupPath = complete ? meursingLookupPath(value) : null;
  const fields: { key: keyof MeursingInputs; label: string; hint: string }[] = [
    { key: "starch_glucose_pct", label: "Starch or glucose (%)", hint: "Total starch/glucose by weight" },
    { key: "sucrose_invert_isoglucose_pct", label: "Sucrose, invert sugar or isoglucose (%)", hint: "Total sugar band used by Meursing lookup" },
    { key: "milk_fat_pct", label: "Milk fat (%)", hint: "Milk-fat percentage by weight" },
    { key: "milk_protein_pct", label: "Milk protein (%)", hint: "Milk-protein percentage by weight" },
  ];
  return (
    <>
      <h3 className="text-xl font-bold mb-2">Composition for additional-code check</h3>
      <p className="tj-hint">
        Some processed foods containing milk and sugar need a Meursing/additional code
        for NI/EU-style declaration handoff. These percentages are collected once and
        carried into the duty result and declaration draft.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
        {fields.map((f) => (
          <div key={f.key}>
            <label className="tj-label">{f.label}</label>
            <span className="tj-hint">{f.hint}</span>
            <input
              type="number"
              inputMode="decimal"
              className="tj-input"
              value={(value[f.key] as number | null | undefined) ?? ""}
              onChange={(e) => setField(f.key, e.target.value)}
              min="0"
              max="100"
              step="0.01"
            />
          </div>
        ))}
      </div>
      <div className="mt-3 border-l-4 border-emerald-500 bg-gray-900 p-3 text-xs text-gray-300">
        {value.additional_code ? (
          <>
            Prefilled Meursing/additional code: <strong className="font-mono text-emerald-400">{value.additional_code}</strong>.
            Edit any percentage to clear this and recompute from the current composition during duty calculation.
          </>
        ) : complete && lookupPath ? (
          <>
            We will work out the additional code from these percentages when we calculate your duty.
            <details className="mt-1">
              <summary className="cursor-pointer text-gray-500">Technical details</summary>
              <span className="font-mono text-emerald-400">{lookupPath}</span>
            </details>
          </>
        ) : (
          "Enter the four percentages so we can work out the additional code."
        )}
      </div>
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={complete} />
    </>
  );
}

function SubExciseAbv({ value, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">What's the alcohol content?</h3>
      <p className="tj-hint">
        UK excise duty bands (Alcohol Duty Reform 2023): below 1.2% is no duty,
        1.2-3.4% is the reduced rate, 3.5-8.4% is the standard rate (with different
        sub-rates for beer/cider/wine), 8.5-22% is the higher rate, and 22% upward is
        the spirits rate.
      </p>
      <label className="tj-label mt-3">ABV (%)</label>
      <input
        type="number"
        inputMode="decimal"
        className="tj-input max-w-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        min="0"
        max="100"
        step="0.1"
        autoFocus
      />
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={value !== "" && Number(value) > 0} />
    </>
  );
}

function SubExciseVolume({ value, category, onChange, onContinue, onBack }: any) {
  const isFuel = category === "fuel";
  return (
    <>
      <h3 className="text-xl font-bold mb-2">
        {isFuel
          ? "What volume of fuel is being imported?"
          : "What volume of alcoholic product is being imported?"}
      </h3>
      <p className="tj-hint">
        {isFuel
          ? "Fuel duty is charged per litre of product. Use the total liquid volume being imported."
          : "Excise is charged on litres of product and ABV. Use the total liquid volume, not litres of pure alcohol."}
      </p>
      <label className="tj-label mt-3">Product volume (litres)</label>
      <input
        type="number"
        inputMode="decimal"
        className="tj-input max-w-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        min="0"
        step="0.01"
        autoFocus
      />
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={value !== "" && Number(value) > 0} />
    </>
  );
}

function SubExciseSticks({ value, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">How many cigarettes are you importing?</h3>
      <p className="tj-hint">
        Cigarette duty is charged per 1,000 cigarettes (sticks) plus a percentage
        of the UK retail selling price, with a minimum amount per 1,000. Count
        individual cigarettes, not packs or cartons.
      </p>
      <label className="tj-label mt-3">Number of cigarettes (sticks)</label>
      <input
        type="number"
        inputMode="numeric"
        className="tj-input max-w-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        min="0"
        step="1"
        autoFocus
      />
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={value !== "" && Number(value) > 0} />
    </>
  );
}

function SubExciseWeight({ value, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">What is the net weight of the tobacco?</h3>
      <p className="tj-hint">
        Duty on cigars, hand-rolling and other smoking tobacco is charged per
        kilogram of product. Use the weight of the tobacco itself, not including
        packaging.
      </p>
      <label className="tj-label mt-3">Net weight (kg)</label>
      <input
        type="number"
        inputMode="decimal"
        className="tj-input max-w-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        min="0"
        step="0.01"
        autoFocus
      />
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={value !== "" && Number(value) > 0} />
    </>
  );
}

function SubExciseRetail({ value, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">What is the total UK retail value of the cigarettes?</h3>
      <p className="tj-hint">
        Part of cigarette duty is calculated as a percentage of the recommended
        UK retail selling price. Enter the total retail value of everything you
        are importing, in pounds.
      </p>
      <label className="tj-label mt-3">UK retail value (GBP)</label>
      <input
        type="number"
        inputMode="decimal"
        className="tj-input max-w-xs"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        min="0"
        step="0.01"
        autoFocus
      />
      <NavButtons onBack={onBack} onContinue={onContinue} canContinue={value !== "" && Number(value) > 0} />
    </>
  );
}

function SubExciseSpr({ value, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">Is the producer eligible for Small Producer Relief?</h3>
      <p className="tj-hint">
        Small Producer Relief applies to producers making under 4,500 hectolitres of
        pure alcohol a year, on products below 8.5% ABV. The relief reduces the duty
        rate on a taper - this demo applies a flat 50% discount for illustration.
        Spirits (22%+ ABV) never qualify.
      </p>
      <div className="space-y-2 mt-3">
        <label className="flex items-center gap-2">
          <input type="radio" checked={value} onChange={() => onChange(true)} className="h-4 w-4" />
          <span>Yes - the producer is registered as a small producer</span>
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" checked={!value} onChange={() => onChange(false)} className="h-4 w-4" />
          <span>No</span>
        </label>
      </div>
      <NavButtons onBack={onBack} onContinue={onContinue} />
    </>
  );
}

function SubExciseDraught({ value, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">Is this draught alcohol?</h3>
      <p className="tj-hint">
        Draught Relief applies to alcohol packaged in containers of 20 litres or more
        designed for dispensing on-trade (typically beer or cider kegs). The relief is
        a 9.2% discount on the standard rate. Spirits do not qualify.
      </p>
      <div className="space-y-2 mt-3">
        <label className="flex items-center gap-2">
          <input type="radio" checked={value} onChange={() => onChange(true)} className="h-4 w-4" />
          <span>Yes - container 20L or more, for on-trade dispense</span>
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" checked={!value} onChange={() => onChange(false)} className="h-4 w-4" />
          <span>No - packaged in bottles, cans or smaller containers</span>
        </label>
      </div>
      <NavButtons onBack={onBack} onContinue={onContinue} />
    </>
  );
}

function SubVat({ value, defaultVatRate, onChange, onContinue, onBack }: any) {
  return (
    <>
      <h3 className="text-xl font-bold mb-2">Which VAT rate applies?</h3>
      <p className="tj-hint">
        Most goods are at standard rate (20%). A few categories (e.g. children's
        clothing, books, most food, some energy-saving materials) are zero-rated.
        A small number (e.g. domestic fuel) are at the 5% reduced rate. If you are
        not sure, keep the suggested rate - it comes from the UK Tariff's VAT
        measure for this commodity. We collect the rate here; the VAT amount is
        calculated in Import costs.{" "}
        <a href="https://www.gov.uk/guidance/rates-of-vat-on-different-goods-and-services" target="_blank" rel="noreferrer" className="text-blue-400 underline">Check VAT rates for different goods on GOV.UK (opens in new tab)</a>.
      </p>
      <div className="space-y-2 mt-3">
        <label className="flex items-center gap-2">
          <input
            type="radio"
            checked={value === null}
            onChange={() => onChange(null)}
            className="h-4 w-4"
          />
          <span>
            Use the rate for this commodity
            {typeof defaultVatRate === "number" ? ` (${defaultVatRate}%)` : ""} - recommended
          </span>
        </label>
        {[
          { v: 20, label: "Standard rate (20%) - most goods" },
          { v: 5, label: "Reduced rate (5%) - e.g. domestic fuel" },
          { v: 0, label: "Zero rate (0%) - e.g. children's clothing, most food, books" },
        ].map((opt) => (
          <label key={opt.v} className="flex items-center gap-2">
            <input
              type="radio"
              checked={value === opt.v}
              onChange={() => onChange(opt.v)}
              className="h-4 w-4"
            />
            <span>{opt.label}</span>
          </label>
        ))}
      </div>
      <NavButtons onBack={onBack} onContinue={onContinue} />
    </>
  );
}

function SubReview({ state, reqs, inference, showProof, loading, onCalculate, onBack, onJump }: any) {
  const rows: { key: Sub; label: string; value: string }[] = [
    { key: "date", label: "Import date", value: state.importDate },
    { key: "destination", label: "Import destination", value: state.destination },
    { key: "country", label: "Country of origin", value: state.country },
  ];
  if (showProof) {
    rows.push({ key: "proof", label: "Proof of origin held?", value: state.hasProof ? "Yes" : "No" });
  }
  if (reqs?.needs_supplementary_units)
    rows.push({ key: "quantity", label: `Quantity (${reqs.supplementary_unit_type})`, value: state.units });
  if (reqs?.needs_meursing_code)
    rows.push({ key: "meursing", label: "Meursing/additional-code composition", value: formatMeursing(state.meursing) });
  if (reqs?.needs_excise) {
    const cat = reqs.excise_category ?? "alcohol";
    if (cat === "tobacco") {
      const wanted = new Set(reqs.excise_required_inputs ?? []);
      if (wanted.has("sticks"))
        rows.push({ key: "excise_sticks", label: "Cigarettes (sticks)", value: state.sticks });
      if (wanted.has("net_weight_kg"))
        rows.push({ key: "excise_weight", label: "Net weight (kg)", value: state.netWeight ? `${state.netWeight} kg` : "" });
      if (wanted.has("retail_value_gbp"))
        rows.push({ key: "excise_retail", label: "UK retail value", value: state.retailValue ? `£${state.retailValue}` : "" });
    } else if (cat === "fuel") {
      rows.push({
        key: "excise_volume",
        label: "Fuel volume (litres)",
        value: state.exciseVolume || (reqs.supplementary_unit_type === "LTR" ? state.units : ""),
      });
    } else {
      rows.push({
        key: "excise_volume",
        label: "Excise volume (litres)",
        value: state.exciseVolume || (reqs.supplementary_unit_type === "LTR" ? state.units : ""),
      });
      rows.push({ key: "excise_abv", label: "ABV", value: `${state.abv}%` });
      rows.push({ key: "excise_spr", label: "Small Producer Relief?", value: state.isSpr ? "Yes" : "No" });
      rows.push({ key: "excise_draught", label: "Draught Relief?", value: state.isDraught ? "Yes" : "No" });
    }
  }
  rows.push({
    key: "vat",
    label: "VAT rate",
    value: state.vatRate === null
      ? `Commodity rate${reqs ? ` (${reqs.default_vat_rate}%)` : ""}`
      : `${state.vatRate}%`,
  });
  return (
    <>
      <h3 className="text-xl font-bold mb-2">Check your answers</h3>
      <p className="tj-hint">
        Confirm before we calculate. Click any row to edit it.
      </p>
      <table className="w-full text-sm mt-3">
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-b border-gray-800">
              <td className="py-2 text-gray-400 w-1/2">
                {r.label}
                {inference?.sources?.[fieldForStep(r.key)] && (
                  <span className="block text-xs text-emerald-400 mt-0.5">
                    {prettySource(inference.sources[fieldForStep(r.key)])}
                  </span>
                )}
              </td>
              <td className="py-2 font-mono">{r.value || "-"}</td>
              <td className="py-2 text-right">
                <button onClick={() => onJump(r.key)} className="text-blue-400 underline">
                  Change
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex justify-between mt-6">
        <button className="tj-btn-secondary" onClick={onBack}>
          &larr; Back
        </button>
        <button className="tj-btn" onClick={onCalculate} disabled={loading}>
          {loading ? "Calculating..." : "Calculate duty"}
        </button>
      </div>
    </>
  );
}

function fieldForStep(step: Sub): string {
  return {
    date: "import_date",
    destination: "import_destination",
    country: "country_of_origin",
    proof: "has_proof_of_origin",
    quantity: "quantity_units",
    meursing: "meursing_inputs",
    excise_abv: "abv",
    excise_volume: "excise_volume_litres",
    excise_sticks: "sticks",
    excise_weight: "net_weight_kg",
    excise_retail: "retail_value_gbp",
    excise_spr: "is_small_producer",
    excise_draught: "is_draught",
    vat: "vat_rate",
    review: "review",
    result: "result",
  }[step];
}

function fieldForSkippedKey(key: string): string | null {
  return {
    date: "import_date",
    import_date: "import_date",
    destination: "import_destination",
    import_destination: "import_destination",
    country: "country_of_origin",
    country_of_origin: "country_of_origin",
    origin: "country_of_origin",
    proof: "has_proof_of_origin",
    has_proof_of_origin: "has_proof_of_origin",
    quantity: "quantity_units",
    quantity_units: "quantity_units",
    meursing: "meursing_inputs",
    meursing_inputs: "meursing_inputs",
    excise_abv: "abv",
    abv: "abv",
    excise_volume: "excise_volume_litres",
    excise_volume_litres: "excise_volume_litres",
    excise_spr: "is_small_producer",
    is_small_producer: "is_small_producer",
    excise_draught: "is_draught",
    is_draught: "is_draught",
    vat: "vat_rate",
    vat_rate: "vat_rate",
  }[key] ?? null;
}

function labelForSkippedKey(key: string): string {
  return {
    date: "Import date",
    import_date: "Import date",
    destination: "Import destination",
    import_destination: "Import destination",
    country: "Country of origin",
    country_of_origin: "Country of origin",
    origin: "Country of origin",
    proof: "Proof of origin",
    has_proof_of_origin: "Proof of origin",
    quantity: "Supplementary units",
    quantity_units: "Supplementary units",
    meursing: "Additional-code composition",
    meursing_inputs: "Additional-code composition",
    excise_abv: "ABV",
    abv: "ABV",
    excise_volume: "Excise volume",
    excise_volume_litres: "Excise volume",
    excise_spr: "Small Producer Relief",
    is_small_producer: "Small Producer Relief",
    excise_draught: "Draught Relief",
    is_draught: "Draught Relief",
    vat: "VAT rate",
    vat_rate: "VAT rate",
  }[key] ?? key.replace(/_/g, " ");
}

function valueForSkippedKey(
  key: string,
  values: {
    importDate: string;
    destination: "GB" | "XI";
    country: string;
    hasProof: boolean;
    units: string;
    quantityUnitType: string;
    abv: string;
    exciseVolume: string;
    isSpr: boolean;
    isDraught: boolean;
    meursing: MeursingInputs;
    vatRate: number | null;
    defaultVatRate: number | null;
  }
): string {
  switch (key) {
    case "date":
    case "import_date":
      return values.importDate;
    case "destination":
    case "import_destination":
      return values.destination === "XI" ? "XI - Northern Ireland" : "GB - Great Britain";
    case "country":
    case "country_of_origin":
    case "origin":
      return values.country;
    case "proof":
    case "has_proof_of_origin":
      return values.hasProof ? "Yes" : "No";
    case "quantity":
    case "quantity_units":
      return [values.units, values.quantityUnitType].filter(Boolean).join(" ");
    case "meursing":
    case "meursing_inputs":
      return formatMeursing(values.meursing);
    case "excise_abv":
    case "abv":
      return values.abv ? `${values.abv}%` : "";
    case "excise_volume":
    case "excise_volume_litres":
      return values.exciseVolume ? `${values.exciseVolume} L` : "";
    case "excise_spr":
    case "is_small_producer":
      return values.isSpr ? "Yes" : "No";
    case "excise_draught":
    case "is_draught":
      return values.isDraught ? "Yes" : "No";
    case "vat":
    case "vat_rate":
      return values.vatRate === null
        ? `Commodity rate${values.defaultVatRate !== null ? ` (${values.defaultVatRate}%)` : ""}`
        : `${values.vatRate}%`;
    default:
      return "";
  }
}

function SubResult({
  result,
  explainer,
  loading,
  onNext,
  onEdit,
  onPickChild,
}: {
  result: DutyResult;
  explainer: string | null;
  loading: boolean;
  onNext: () => void;
  onEdit: () => void;
  onPickChild: (child: { commodity_code: string; description: string }) => void;
}) {
  return (
    <>
      <h3 className="text-xl font-bold mb-3">
        {result.needs_more_detail ? "Your code needs one more level of detail" : "Duty calculated"}
      </h3>

      {result.needs_more_detail && (
        <div className="border-l-4 border-amber-500 bg-gray-900 p-4 mb-4">
          <p className="text-sm text-gray-100 mb-3">{result.needs_more_detail.message}</p>
          <div className="space-y-2">
            {result.needs_more_detail.children.map((child) => (
              <button
                key={child.commodity_code}
                onClick={() => onPickChild(child)}
                disabled={loading}
                className="block w-full text-left border border-gray-700 bg-gray-950 p-2 hover:border-blue-500 disabled:opacity-50"
              >
                <span className="font-mono text-blue-400">{child.commodity_code}</span>{" "}
                <span className="text-sm text-gray-300">{child.description}</span>
              </button>
            ))}
          </div>
          {loading && (
            <p className="text-xs italic text-gray-400 mt-2">Recalculating with the more specific code...</p>
          )}
        </div>
      )}

      {result.warnings && result.warnings.length > 0 && (
        <DutyWarningsBlock warnings={result.warnings} />
      )}

      {result.low_value_regime && (
        <div className="border-l-4 border-blue-500 bg-gray-900 p-3 text-sm text-gray-300 mb-4">
          This consignment is valued at £135 or less, so the low-value import
          rules apply: no customs duty is charged at the border, and VAT is
          normally collected when the goods are sold (supply VAT) rather than
          as import VAT.
        </div>
      )}

      {!result.needs_more_detail && (
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4">
        <Stat
          label={result.rate_kind === "specific" ? "Customs duty rate" : "Customs duty rate"}
          value={result.rate_expression || `${result.rate_applied}%`}
          accent
        />
        <Stat
          label="Rate basis"
          value={result.rate_source === "MFN" ? "Standard rate (MFN)" : `${result.rate_source.replace(/_/g, " ")} preference`}
        />
        <Stat label="Customs duty" value={<Money value={result.customs_duty_gbp} />} accent />
      </div>
      )}
      {result.rate_kind === "specific" && (
        <div className="text-xs text-gray-400 mb-3 italic">
          Specific duty: charged per unit (not as a % of customs value). Calculated from your declared quantity.
        </div>
      )}

      {result.excise && (
        <div className="bg-gray-900 p-4 mb-4">
          <div className="text-xs font-bold tracking-widest uppercase text-gray-400 mb-2">
            Excise breakdown
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-gray-400">Band:</span>{" "}
              <strong>{result.excise.band_label}</strong>
            </div>
            <div>
              <span className="text-gray-400">Base rate:</span>{" "}
              <strong>£{result.excise.base_rate_per_lpa_gbp}/LPA</strong>
            </div>
            <div>
              <span className="text-gray-400">Effective rate:</span>{" "}
              <strong>£{result.excise.effective_rate_per_lpa_gbp}/LPA</strong>
            </div>
            <div>
              <span className="text-gray-400">Pure alcohol litres:</span>{" "}
              <strong>{result.excise.pure_alcohol_litres} LPA</strong>{" "}
              <span className="text-gray-400">
                ({result.excise.volume_litres} L × {result.excise.abv}%)
              </span>
            </div>
            <div className="sm:col-span-2 font-bold">
              <span className="text-gray-400 font-normal">Excise duty:</span>{" "}
              <Money value={result.excise.duty_gbp} />
            </div>
            {result.excise.applied_reliefs.length > 0 && (
              <ul className="sm:col-span-2 text-xs text-gray-400 mt-1 space-y-1">
                {result.excise.applied_reliefs.map((r, i) => (
                  <li key={i}>
                    <strong>{r.name}</strong> ({r.discount_pct}% off): {r.note}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {result.excise_detail && (
        <div className="bg-gray-900 p-4 mb-4">
          <div className="text-xs font-bold tracking-widest uppercase text-gray-400 mb-2">
            Excise breakdown - {result.excise_detail.label}
          </div>
          <table className="w-full text-sm">
            <tbody>
              {result.excise_detail.components.map((c, i) => (
                <tr key={i} className="border-b border-gray-800">
                  <td className="py-1 text-gray-400">{c.label}</td>
                  <td className="py-1 text-right"><Money value={c.amount_gbp} /></td>
                </tr>
              ))}
              <tr>
                <td className="py-1 font-bold">Excise duty</td>
                <td className="py-1 text-right font-bold text-emerald-400">
                  <Money value={result.excise_detail.duty_gbp} />
                </td>
              </tr>
            </tbody>
          </table>
          {result.excise_detail.notes.length > 0 && (
            <ul className="text-xs text-gray-400 list-disc list-inside space-y-1 mt-2">
              {result.excise_detail.notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          )}
          <p className="text-xs text-gray-500 mt-2">Excise rates as of {result.excise_detail.rates_as_of}.</p>
        </div>
      )}

      {result.meursing && (
        <div className="bg-gray-900 p-4 mb-4 border border-gray-700">
          <div className="text-xs font-bold tracking-widest uppercase text-gray-400 mb-2">
            Meursing/additional-code check
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-gray-400">Additional code:</span>{" "}
              <strong className="font-mono">{result.meursing.additional_code || "Verify manually"}</strong>
            </div>
            {result.meursing.lookup_url && (
              <details className="mt-1">
                <summary className="cursor-pointer text-gray-500">Technical details</summary>
                <span className="text-gray-400">Lookup path:</span>{" "}
                <span className="font-mono text-xs">{result.meursing.lookup_path || result.meursing.lookup_url}</span>
              </details>
            )}
          </div>
          {result.meursing.bands && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs text-gray-300 mt-3">
              {Object.entries(result.meursing.bands).map(([k, b]) => (
                <div key={k}>
                  <span className="text-gray-400">{prettyMeursingKey(k)}:</span>{" "}
                  <span className="font-mono">{b.label}</span>
                </div>
              ))}
            </div>
          )}
          {result.meursing.note && <p className="text-xs text-gray-400 mt-3">{result.meursing.note}</p>}
        </div>
      )}

      {result.eligible_preferences.length > 0 && (
        <div className="mb-4">
          <span className="text-xs font-bold tracking-widest uppercase text-gray-400">
            Eligible preferences for {result.country_name}
          </span>
          <ul className="text-sm mt-1">
            {result.eligible_preferences.map((p, i) => (
              <li key={i} className="flex justify-between border-b border-gray-800 py-1">
                <span>
                  {p.group}
                  {p.measure_id && (
                    <span className="text-xs text-gray-400 ml-2">(tariff measure {p.measure_id})</span>
                  )}
                </span>
                <span className="font-semibold">
                  {p.rate_expression || `${p.rate}${p.rate_kind === "ad_valorem" ? "%" : ""}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.measures_inspected && result.measures_inspected.length > 0 && (
        <details className="mb-4 text-sm">
          <summary className="cursor-pointer text-blue-400 underline">
            Show all tariff measures we checked for {result.country_name} ({result.measures_inspected.length})
          </summary>
          <table className="w-full text-xs mt-2 border border-gray-700">
            <thead className="bg-gray-900">
              <tr>
                <th className="text-left p-2">Measure</th>
                <th className="text-left p-2">Type</th>
                <th className="text-left p-2">Geographical area</th>
                <th className="text-left p-2">Duty expression</th>
              </tr>
            </thead>
            <tbody>
              {result.measures_inspected.map((m, i) => (
                <tr key={`${m.measure_id}-${m.geographical_area}-${i}`} className="border-t border-gray-700">
                  <td className="p-2 font-mono">{m.measure_id}</td>
                  <td className="p-2">
                    <span className="font-mono">{m.measure_type_id}</span>{" "}
                    <span className="text-gray-400">{m.measure_type_description}</span>
                  </td>
                  <td className="p-2">{m.geographical_area}</td>
                  <td className="p-2 font-mono">{m.duty_expression || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {result.notes.length > 0 && (
        <ul className="text-sm text-gray-400 list-disc list-inside space-y-1 mb-4">
          {result.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}

      <div className="border-l-4 border-blue-500 bg-gray-900 p-4 mb-4">
        <div className="text-xs font-bold tracking-widest uppercase text-blue-400 mb-1">
          What this means
        </div>
        {explainer ? (
          <p className="text-sm text-gray-100">{explainer}</p>
        ) : (
          <p className="text-sm italic text-gray-400">
            Generating a plain-English summary of this result...
          </p>
        )}
      </div>

      <p className="text-xs text-gray-400 mb-4">
        Check rates yourself on the{" "}
        <a href="https://www.gov.uk/trade-tariff" target="_blank" rel="noreferrer" className="text-blue-400 underline">UK Trade Tariff (opens in new tab)</a>,
        or read about{" "}
        <a href="https://www.gov.uk/goods-sent-from-abroad/tax-and-duty" target="_blank" rel="noreferrer" className="text-blue-400 underline">tax and duty on goods sent from abroad (opens in new tab)</a>.
      </p>

      <div className="flex justify-between">
        <button className="tj-btn-secondary" onClick={onEdit}>
          &larr; Change answers
        </button>
        <button className="tj-btn" onClick={onNext}>
          Next: Import costs &rarr;
        </button>
      </div>
    </>
  );
}

// Annotate-and-warn layer (educational stance): trade remedies and
// prohibitions get a prominent alert card, quota/suspension are informational.
// Nothing here ever blocks the calculation or the journey.
function DutyWarningsBlock({ warnings }: { warnings: DutyWarning[] }) {
  const tradeRemedies = warnings.filter((w) => w.kind === "trade_remedy");
  const prohibitions = warnings.filter((w) => w.kind === "prohibition");
  const exciseMissing = warnings.filter((w) => w.kind === "excise_missing");
  const info = warnings.filter(
    (w) => w.kind === "quota" || w.kind === "suspension_applied" || w.kind === "other"
  );
  // Generic restriction notices can be chatty on food codes - collapse beyond 2.
  const visibleProhibitions = prohibitions.slice(0, 2);
  const hiddenProhibitions = prohibitions.slice(2);
  const hasRed = tradeRemedies.length > 0;
  return (
    <div className="space-y-3 mb-4">
      {(tradeRemedies.length > 0 || prohibitions.length > 0) && (
        <div className={`border-l-4 ${hasRed ? "border-red-500" : "border-amber-500"} bg-gray-900 p-4`}>
          <div className={`text-xs font-bold tracking-widest uppercase ${hasRed ? "text-red-400" : "text-amber-400"} mb-2`}>
            Important - check before you import
          </div>
          <ul className="text-sm space-y-2">
            {tradeRemedies.map((w, i) => <WarningItem key={`tr-${i}`} warning={w} />)}
            {visibleProhibitions.map((w, i) => <WarningItem key={`pr-${i}`} warning={w} />)}
          </ul>
          {hiddenProhibitions.length > 0 && (
            <details className="mt-2 text-sm">
              <summary className="cursor-pointer text-blue-400 underline">
                More notices ({hiddenProhibitions.length})
              </summary>
              <ul className="text-sm space-y-2 mt-2">
                {hiddenProhibitions.map((w, i) => <WarningItem key={`hp-${i}`} warning={w} />)}
              </ul>
            </details>
          )}
        </div>
      )}
      {exciseMissing.map((w, i) => (
        <div key={`em-${i}`} className="border-l-4 border-amber-500 bg-gray-900 p-3 text-sm text-gray-300">
          {w.message}
        </div>
      ))}
      {info.map((w, i) => (
        <div key={`in-${i}`} className="border-l-4 border-blue-500 bg-gray-900 p-3 text-sm text-gray-300">
          {w.message}
          {w.rate_range && <span className="text-gray-400"> ({w.rate_range})</span>}
        </div>
      ))}
    </div>
  );
}

function WarningItem({ warning }: { warning: DutyWarning }) {
  const isRemedy = warning.kind === "trade_remedy";
  return (
    <li>
      <strong className={isRemedy ? "text-red-400" : "text-amber-400"}>
        {isRemedy ? "Trade remedy:" : "Restriction:"}
      </strong>{" "}
      {warning.message}
      {warning.rate_range && (
        <span className="block text-xs text-gray-400 mt-0.5">Possible rate range: {warning.rate_range}</span>
      )}
    </li>
  );
}

function Stat({ label, value, accent }: { label: string; value: React.ReactNode; accent?: boolean }) {
  return (
    <div className="border border-gray-700 p-3">
      <div className="text-xs text-gray-400">{label}</div>
      <div className={`text-lg font-bold ${accent ? "text-emerald-400" : ""}`}>{value}</div>
    </div>
  );
}

function knownDutyInputs(state: JourneyState): Record<string, unknown> {
  return {
    country_of_origin: state.countryOfOrigin || null,
    quantity_units: state.quantityUnits ?? state.selectedExample?.seed?.quantity_units ?? null,
    quantity_unit_type: state.quantityUnitType ?? state.selectedExample?.seed?.quantity_unit_type ?? null,
    excise_volume_litres: state.exciseVolumeLitres ?? state.selectedExample?.seed?.excise_volume_litres ?? null,
    abv: state.abv ?? state.selectedExample?.seed?.abv ?? null,
    meursing_inputs: state.meursingInputs ?? state.selectedExample?.seed?.meursing_inputs ?? null,
  };
}

function meursingComplete(v: MeursingInputs): boolean {
  return (
    v.starch_glucose_pct !== null &&
    v.starch_glucose_pct !== undefined &&
    v.sucrose_invert_isoglucose_pct !== null &&
    v.sucrose_invert_isoglucose_pct !== undefined &&
    v.milk_fat_pct !== null &&
    v.milk_fat_pct !== undefined &&
    v.milk_protein_pct !== null &&
    v.milk_protein_pct !== undefined
  );
}

type MeursingBand = [string, number, number | null];

const STARCH_GLUCOSE_BANDS: MeursingBand[] = [
  ["0", 0, 4.99],
  ["5", 5, 24.99],
  ["25", 25, 49.99],
  ["50", 50, 74.99],
  ["75", 75, null],
];

const SUCROSE_BANDS: MeursingBand[] = [
  ["0", 0, 4.99],
  ["5", 5, 29.99],
  ["30", 30, 49.99],
  ["50", 50, 69.99],
  ["70", 70, null],
];

const MILK_FAT_BANDS: MeursingBand[] = [
  ["0", 0, 1.49],
  ["1", 1.5, 2.99],
  ["3", 3, 5.99],
  ["6", 6, 8.99],
  ["9", 9, 11.99],
  ["12", 12, 17.99],
  ["18", 18, 25.99],
  ["26", 26, 39.99],
  ["40", 40, 54.99],
  ["55", 55, 69.99],
  ["70", 70, 84.99],
  ["85", 85, null],
];

const MILK_PROTEIN_BANDS: MeursingBand[] = [
  ["0", 0, 2.49],
  ["2", 2.5, 5.99],
  ["6", 6, 17.99],
  ["18", 18, 29.99],
  ["30", 30, 59.99],
  ["60", 60, null],
];

function meursingLookupPath(v: MeursingInputs): string | null {
  const key = [
    meursingBandValue(v.starch_glucose_pct, STARCH_GLUCOSE_BANDS),
    meursingBandValue(v.sucrose_invert_isoglucose_pct, SUCROSE_BANDS),
    meursingBandValue(v.milk_fat_pct, MILK_FAT_BANDS),
    meursingBandValue(v.milk_protein_pct, MILK_PROTEIN_BANDS),
  ];
  if (key.some((part) => part === null)) return null;
  return `/additional-commodity-code/y/${key.join("/")}`;
}

function meursingBandValue(value: number | null | undefined, bands: MeursingBand[]): string | null {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return null;
  const numeric = Number(value);
  for (const [code, low, high] of bands) {
    if (numeric >= low && (high === null || numeric <= high)) return code;
  }
  return null;
}

function canUseQuantityAsExciseVolume(
  supplementaryUnitType: string | null | undefined,
  quantityUnits: string,
  exciseVolumeLitres: string
): boolean {
  if (exciseVolumeLitres !== "" && Number(exciseVolumeLitres) > 0) return true;
  return supplementaryUnitType === "LTR" && quantityUnits !== "" && Number(quantityUnits) > 0;
}

function formatMeursing(v: MeursingInputs | null | undefined): string {
  if (!v) return "";
  const parts = [
    `starch/glucose ${v.starch_glucose_pct ?? "-"}%`,
    `sugar ${v.sucrose_invert_isoglucose_pct ?? "-"}%`,
    `milk fat ${v.milk_fat_pct ?? "-"}%`,
    `milk protein ${v.milk_protein_pct ?? "-"}%`,
  ];
  if (v.additional_code) parts.push(`code ${v.additional_code}`);
  return parts.join("; ");
}

function prettyMeursingKey(k: string): string {
  return k.replace(/_/g, " ").replace(/^./, (m) => m.toUpperCase());
}

// Tiny helper: does the trader's chosen country have ANY non-MFN preference for the commodity?
// If not, skip the proof-of-origin step entirely - it's irrelevant.
function countryHasAnyPreference(
  countryCode: string,
  countries: Country[],
  _commodityCode: string | undefined,
  reqs: CommodityRequirements
): boolean {
  if (!countryCode || !reqs.has_any_preference) return false;
  const c = countries.find((x) => x.code === countryCode);
  if (!c) return false;
  return c.groups.some((g) => g !== "MFN");
}
