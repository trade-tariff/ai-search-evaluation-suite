# Duty and VAT method (formulas, VAT base, measure types)

The exact calculation, mirroring HMRC's order of operations.

## Formulas

**Customs duty**
- *Ad valorem* (a percentage, e.g. "6.50 %"): `duty = customs_value_gbp * pct / 100`.
- *Specific* (a money amount per unit, e.g. "GBP 12.00 / 100 kg"): `duty = rate * quantity_in_that_unit`.
  You must have the quantity in the measure's unit (net mass kg, litres, hectolitres, number of
  items, etc.). If you don't, ask for it - don't silently skip the duty.
- *Compound*: both an ad valorem and a specific component - add them.

**Import VAT**
- `vat_base = customs_value_gbp + customs_duty_gbp + excise_duty_gbp + incidental_costs_to_uk_gbp`
- `import_vat = vat_base * vat_rate / 100`
- Incidental costs to the UK = port handling and onward freight **to the first UK destination**.
  Post-arrival charges are outside the VAT base. UK standard rate 20%; reduced 5%; zero 0%.

**Totals**
- `cash_at_border = customs_duty + excise_duty + import_vat` (import VAT only if not low-value/PVA).
- `total_landed_cost = customs_value + customs_duty + excise_duty + incidental_costs + import_vat + other_post_arrival_charges`.

## Special cases

- **Low value (<= GBP 135 customs value, no excise goods):** no customs duty; VAT is **supply VAT**
  collected by the seller at the point of sale, not import VAT at the border.
- **Postponed VAT Accounting (PVA):** VAT-registered importers can account for import VAT on their
  VAT return instead of paying it at the border - moves it off `cash_at_border`, not off the total.
- **Northern Ireland (`service="xi"`):** different measure set; goods "at risk" of onward movement
  to the EU can attract the EU duty. Treat NI as its own lane, not a copy of GB.

## Measure types you'll see in `import_measures`

| Measure type | Meaning | Effect |
|---|---|---|
| Third country duty | MFN rate for any origin (ERGA OMNES) | The fallback duty when no preference applies |
| Tariff preference | Lower rate for a specific country/FTA/GSP group | Needs valid proof of origin to use |
| Tariff quota / preferential quota | Reduced rate up to a volume limit | Check balance via `search_quotas` |
| Anti-dumping / countervailing / safeguard | Extra duty, often supplier-specific via an additional code | Added on top; confirm the supplier's additional code |
| Suspension | Duty temporarily suspended (often 0%) for certain goods/uses | Can remove the duty; check conditions |
| VAT | The import VAT rate | The `vat_rate` for the VAT base |
| Excise | Alcohol / tobacco / hydrocarbon oils | Separate excise duty (see below) |

When several duty measures could apply, prefer the most specific origin match, then the lowest
lawful rate the trader actually qualifies for (e.g. preference only if origin rules are met).

## Excise (only chapters 22 alcohol, 24 tobacco, 27 fuel)

Excise is separate from customs duty and sits **inside** the VAT base. It is usually specific
(per litre of pure alcohol, per 1000 cigarettes + an ad valorem on retail price, per litre of
fuel) and needs quantity. If the goods are excise goods, flag it and either apply the live excise
rate or tell the trader you need the specific rate - don't fold it into the customs duty number.
Known gaps to call out rather than guess: raw/unmanufactured tobacco and vaping/nicotine products.

## Worked sketch

Headphones (`8518 30 00`), origin China, into GB, customs value GBP 1,000, VAT 20%, no preference:
- `lookup_commodity` -> Third country duty "0.00 %" (ERGA OMNES) and a VAT measure 20%.
- Customs duty = 1000 * 0 / 100 = **GBP 0.00**.
- VAT base = 1000 + 0 + 0 + 0 = 1000; import VAT = 1000 * 20 / 100 = **GBP 200.00**.
- Cash at border = **GBP 200.00**; total landed (goods + tax) = **GBP 1,200.00**.
Always show which measure each number came from so the trader can verify.
