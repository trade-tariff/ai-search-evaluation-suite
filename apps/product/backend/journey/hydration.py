"""Live evidence hydration for selected commodity codes.

Hydration attaches authoritative/contextual evidence to a code that retrieval
has already shortlisted or the trader has selected. It does not generate
candidate codes. That keeps scraped or stale evidence from creating a bad CC
list and then making it look well-grounded.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from . import local_db
from .evidence_labels import hydration_labels
from .provider_guard import openai_allowed


MAX_BODY = 1400
DEFAULT_LIVE_HYDRATION_MODEL = "gpt-5-nano"
ATAR_BASE = "https://www.tax.service.gov.uk/search-for-advance-tariff-rulings"
USER_AGENT = "ai-fan-out live-hydration/1.0"
DEFAULT_SOURCES = {
    "facets": True,
    "footnotes": True,
    "measures": True,
    "section_notes": True,
    "chapter_notes": True,
    "hsen": True,
    "atar": True,
    "girs": True,
}


def hydrate_commodity(
    code: str,
    *,
    summarize: bool | None = None,
    model: str | None = None,
    sources: dict | None = None,
) -> dict:
    flat = local_db._flat(code)
    source_flags = _normal_sources(sources)
    commodity = local_db.commodity(flat)
    if not commodity:
        return {
            "ok": False,
            "commodity_code": flat,
            "error": "Commodity not found in live tariff data.",
            "candidate_guardrail": "Hydration never creates candidate codes; run retrieval/classification first.",
        }

    evidence: list[dict] = []
    evidence.extend(_legal_notes(flat, source_flags))
    evidence.extend(_kg_edges(flat, source_flags))
    if source_flags["facets"]:
        evidence.extend(_facets(flat))
    if source_flags["measures"]:
        evidence.extend(_measures(flat))
    if source_flags["footnotes"]:
        evidence.extend(_footnotes(flat))
    if source_flags["atar"]:
        evidence.extend(_live_atar_rulings(flat, evidence))

    summary = _deterministic_summary(evidence)
    use_llm = (
        summarize
        if summarize is not None
        else os.environ.get("LIVE_HYDRATION_USE_LLM") == "1"
    )
    if use_llm:
        summary["llm"] = _llm_summary(commodity, evidence, model=model)

    return {
        "ok": True,
        "commodity_code": flat,
        "code_dotted": local_db._dotted(flat),
        "commodity": commodity,
        "model_requested": _model_name(model) if use_llm else None,
        "sources_requested": source_flags,
        "candidate_guardrail": (
            "Evidence is hydrated only for selected/shortlisted commodity codes; "
            "it does not create or broaden the candidate list."
        ),
        "coverage": _coverage(evidence),
        "summary": summary,
        "evidence": evidence,
    }


def _normal_sources(sources: dict | None) -> dict[str, bool]:
    out = dict(DEFAULT_SOURCES)
    for key, value in (sources or {}).items():
        if key in out:
            out[key] = bool(value)
    return out


def _legal_notes(flat: str, sources: dict[str, bool]) -> list[dict]:
    chapter = flat[:2]
    section_roman = local_db._chapter_to_section(chapter)
    out: list[dict] = []
    try:
        with local_db._conn() as c, c.cursor() as cur:
            if sources["chapter_notes"]:
                cur.execute(
                    f"""
                    SELECT chapter_id, content
                    FROM {local_db.SCHEMA}.chapter_notes
                    WHERE chapter_id = %s AND content IS NOT NULL
                    LIMIT 1
                    """,
                    (chapter,),
                )
                row = cur.fetchone()
                if row:
                    out.append(_evidence(
                        kind="chapter_note",
                        id=f"chapter:{chapter}:notes",
                        title=f"Chapter {chapter} notes",
                        body=row["content"],
                        source="uk.chapter_notes",
                        authority_tier=1,
                        scope=f"chapter:{chapter}",
                    ))
            if sources["section_notes"] and section_roman:
                cur.execute(
                    f"""
                    SELECT sn.section_id, s.numeral, s.title, sn.content
                    FROM {local_db.SCHEMA}.section_notes sn
                    JOIN {local_db.SCHEMA}.sections s ON s.id = sn.section_id
                    WHERE s.numeral = %s AND sn.content IS NOT NULL
                    LIMIT 1
                    """,
                    (section_roman,),
                )
                row = cur.fetchone()
                if row:
                    out.append(_evidence(
                        kind="section_note",
                        id=f"section:{section_roman}:notes",
                        title=f"Section {section_roman} notes - {row['title']}",
                        body=row["content"],
                        source="uk.section_notes",
                        authority_tier=1,
                        scope=f"section:{section_roman}",
                    ))
    except Exception as exc:
        out.append(_warning("legal_notes", exc))
    return out


def _kg_edges(flat: str, sources: dict[str, bool]) -> list[dict]:
    out: list[dict] = []
    try:
        edges = local_db.kg_edges_for_candidates([flat], include={
            "chapter_notes": sources["chapter_notes"],
            "section_notes": sources["section_notes"],
            "legacy_blob_notes": False,
            "girs": sources["girs"],
            "atar_rationales": sources["atar"],
            "heading_rules": True,
            "other_global": False,
            "hsen": sources["hsen"],
        })
        for edge in edges[:40]:
            kind = _edge_kind(edge)
            out.append(_evidence(
                kind=kind,
                id=edge.get("id") or "",
                title=edge.get("title") or "",
                body=edge.get("body") or "",
                source=edge.get("source") or "",
                authority_tier=edge.get("authority_tier"),
                scope=edge.get("scope") or "",
                url=_extract_url(edge.get("source") or ""),
                use_scopes=edge.get("use_scopes") or None,
                evidence_roles=edge.get("evidence_roles") or None,
                provenance=edge.get("provenance") or {},
            ))
    except Exception as exc:
        out.append(_warning("kg_edges", exc))
    return out


def _measures(flat: str) -> list[dict]:
    out: list[dict] = []
    chain = local_db._parent_code_chain(flat)
    try:
        with local_db._conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  m.measure_sid,
                  m.measure_type_id,
                  m.goods_nomenclature_item_id AS attached_at,
                  m.geographical_area_id,
                  COALESCE(ga.description, '') AS geographical_area_description,
                  COALESCE(mtd.description, '') AS measure_type_description,
                  (
                    SELECT jsonb_agg(jsonb_build_object(
                      'duty_expression_id', mc.duty_expression_id,
                      'duty_amount', mc.duty_amount,
                      'monetary_unit_code', mc.monetary_unit_code,
                      'measurement_unit_code', mc.measurement_unit_code,
                      'measurement_unit_qualifier_code', mc.measurement_unit_qualifier_code
                    ))
                    FROM {local_db.SCHEMA}.measure_components mc
                    WHERE mc.measure_sid = m.measure_sid
                  ) AS components
                FROM {local_db.SCHEMA}.measures m
                LEFT JOIN {local_db.SCHEMA}.measure_type_descriptions mtd
                  ON mtd.measure_type_id = m.measure_type_id
                LEFT JOIN {local_db.SCHEMA}.geographical_area_descriptions ga
                  ON ga.geographical_area_id = m.geographical_area_id
                 AND ga.language_id = 'EN'
                WHERE m.goods_nomenclature_item_id = ANY(%s)
                  AND (m.validity_end_date IS NULL OR m.validity_end_date > now())
                ORDER BY m.goods_nomenclature_item_id DESC, m.measure_type_id, m.geographical_area_id
                LIMIT 40
                """,
                (chain,),
            )
            for row in cur.fetchall():
                components = row.get("components") or []
                body_parts = [
                    f"Type {row['measure_type_id']}: {row['measure_type_description']}".strip(),
                    f"Geography: {row['geographical_area_description'] or row['geographical_area_id']}",
                    f"Attached at: {row['attached_at']}" + (" (inherited)" if row["attached_at"] != flat else ""),
                ]
                if components:
                    body_parts.append("Components: " + "; ".join(_format_measure_component(c) for c in components[:4]))
                out.append(_evidence(
                    kind="measure",
                    id=f"measure:{row['measure_sid']}",
                    title=f"Measure {row['measure_sid']} - {row['measure_type_description'] or row['measure_type_id']}",
                    body="\n".join(body_parts),
                    source="uk.measures",
                    authority_tier=7,
                    scope=f"commodity:{row['attached_at']}",
                    provenance={
                        "measure_sid": row["measure_sid"],
                        "attached_at": row["attached_at"],
                        "inherited": row["attached_at"] != flat,
                        "geographical_area_id": row["geographical_area_id"],
                        "measure_type_id": row["measure_type_id"],
                    },
                ))
    except Exception as exc:
        out.append(_warning("measures", exc))
    return out


def _format_measure_component(component: dict) -> str:
    amount = component.get("duty_amount")
    monetary = component.get("monetary_unit_code")
    unit = component.get("measurement_unit_code")
    qualifier = component.get("measurement_unit_qualifier_code")
    if amount is None:
        return str(component.get("duty_expression_id") or "component")
    if monetary and unit:
        suffix = f" / {unit}{('/' + qualifier) if qualifier else ''}"
        return f"{amount} {monetary}{suffix}"
    return f"{amount}%"


def _facets(flat: str) -> list[dict]:
    out: list[dict] = []
    try:
        for f in local_db.facets_for(flat)[:50]:
            facet_key = str(f.get("facet_key") or "facet").replace("_", " ")
            facet_value = f.get("facet_value")
            out.append(_evidence(
                kind="facet",
                id=f"facet:{flat}:{f.get('facet_key')}:{facet_value}",
                title=f"{facet_key}: {facet_value}",
                body=f.get("evidence") or "",
                source=f.get("source") or "kg.commodity_facets",
                authority_tier=f.get("authority_tier"),
                scope=f"commodity:{flat}",
                use_scopes=f.get("use_scopes") or None,
                evidence_roles=f.get("evidence_roles") or None,
                provenance=f.get("provenance") or {},
            ))
    except Exception as exc:
        out.append(_warning("facets", exc))
    return out


def _live_atar_rulings(flat: str, existing_evidence: list[dict]) -> list[dict]:
    """Fetch exact-code ATAR ruling pages for audit hydration.

    Prefer refs already linked in KG; if none are linked, do a bounded public
    listing search by commodity code and keep only rulings whose fetched page
    confirms the exact code. This is deliberately evidence-only: it never adds
    candidate codes to retrieval.
    """
    refs = _atar_refs_from_evidence(existing_evidence)
    out: list[dict] = []
    try:
        with httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            if not refs:
                refs = _search_atar_refs_for_code(client, flat)[:5]
            for ref in refs[:5]:
                ruling = _fetch_atar_ruling(client, ref)
                if not ruling or local_db._flat(ruling.get("commodity_code") or "") != flat:
                    continue
                body_parts = []
                if ruling.get("description"):
                    body_parts.append(f"Product: {ruling['description']}")
                if ruling.get("justification"):
                    body_parts.append(f"HMRC justification: {ruling['justification']}")
                if ruling.get("keywords"):
                    body_parts.append("Keywords: " + ", ".join(ruling["keywords"]))
                out.append(_evidence(
                    kind="atar",
                    id=f"atar_live:{ref}",
                    title=f"Live ATAR {ref}",
                    body="\n\n".join(body_parts),
                    source=f"{ATAR_BASE}/ruling/{ref}",
                    authority_tier=2,
                    scope=f"commodity:{flat}",
                    url=f"{ATAR_BASE}/ruling/{ref}",
                    provenance={
                        "source_type": "atar",
                        "source_id": ref,
                        "live_scraped": True,
                        "start_date": ruling.get("start_date"),
                        "expiry_date": ruling.get("expiry_date"),
                    },
                ))
    except Exception as exc:
        out.append(_warning("atar_live_scrape", exc))
    return out


def _atar_refs_from_evidence(evidence: list[dict]) -> list[str]:
    refs: list[str] = []
    for item in evidence:
        if item.get("kind") != "atar":
            continue
        candidates = [
            (item.get("provenance") or {}).get("source_id"),
            item.get("id", "").removeprefix("atar_"),
            item.get("source"),
            item.get("url"),
        ]
        for value in candidates:
            match = re.search(r"(\d{6,})", str(value or ""))
            if match and match.group(1) not in refs:
                refs.append(match.group(1))
    return refs


def _search_atar_refs_for_code(client: httpx.Client, flat: str) -> list[str]:
    refs: list[str] = []
    url = f"{ATAR_BASE}/search?{urlencode({'keyword': flat})}"
    resp = client.get(url)
    resp.raise_for_status()
    for ref in re.findall(r"/ruling/(\d+)", resp.text):
        if ref not in refs:
            refs.append(ref)
    return refs


def _fetch_atar_ruling(client: httpx.Client, ref: str) -> dict | None:
    resp = client.get(f"{ATAR_BASE}/ruling/{ref}")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    fields: dict[str, str] = {}
    keywords: list[str] = []
    dl = soup.find("dl", id="ruling-details") or soup.find("dl", class_="govuk-summary-list")
    if dl:
        rows = dl.find_all("div", class_="govuk-summary-list__row")
        for row in rows:
            key_el = row.find("dt", class_="govuk-summary-list__key")
            val_el = row.find("dd", class_="govuk-summary-list__value")
            if key_el is None or val_el is None:
                continue
            key = key_el.get_text(" ", strip=True).lower()
            if key == "keywords":
                keywords = [t.get_text(strip=True) for t in val_el.find_all("span", class_="govuk-tag")]
                if not keywords:
                    keywords = [v.strip() for v in val_el.get_text(",", strip=True).split(",") if v.strip()]
            else:
                fields[key] = val_el.get_text(" ", strip=True)
    else:
        for dt in soup.find_all("dt"):
            key = dt.get_text(" ", strip=True).rstrip(":").strip().lower()
            dd = dt.find_next_sibling("dd")
            if dd:
                fields[key] = dd.get_text(" ", strip=True)
    code = fields.get("commodity code", "")
    code_match = re.search(r"\b(\d{8,10})\b", code)
    return {
        "ref": ref,
        "commodity_code": code_match.group(1) if code_match else code,
        "description": fields.get("description of the goods", "") or fields.get("description", ""),
        "justification": fields.get("justification", "") or fields.get("rationale", ""),
        "keywords": keywords,
        "start_date": fields.get("start date", ""),
        "expiry_date": fields.get("expiry date", ""),
    }


def _footnotes(flat: str) -> list[dict]:
    out: list[dict] = []
    chain = local_db._parent_code_chain(flat)
    try:
        with local_db._conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                WITH code_chain AS (SELECT unnest(%s::text[]) AS code),
                goods_footnotes AS (
                  SELECT 'goods_nomenclature'::text AS linked_to,
                         fagn.goods_nomenclature_item_id AS attached_at,
                         NULL::integer AS measure_sid,
                         fagn.footnote_type AS footnote_type_id,
                         fagn.footnote_id
                  FROM {local_db.SCHEMA}.footnote_association_goods_nomenclatures fagn
                  JOIN code_chain cc ON cc.code = fagn.goods_nomenclature_item_id
                  WHERE fagn.validity_end_date IS NULL OR fagn.validity_end_date > now()
                ),
                measure_footnotes AS (
                  SELECT 'measure'::text AS linked_to,
                         m.goods_nomenclature_item_id AS attached_at,
                         m.measure_sid,
                         fam.footnote_type_id,
                         fam.footnote_id
                  FROM {local_db.SCHEMA}.measures m
                  JOIN code_chain cc ON cc.code = m.goods_nomenclature_item_id
                  JOIN {local_db.SCHEMA}.footnote_association_measures fam
                    ON fam.measure_sid = m.measure_sid
                  WHERE m.validity_end_date IS NULL OR m.validity_end_date > now()
                ),
                all_footnotes AS (
                  SELECT * FROM goods_footnotes
                  UNION ALL
                  SELECT * FROM measure_footnotes
                ),
                latest_desc AS (
                  SELECT DISTINCT ON (footnote_type_id, footnote_id)
                         footnote_type_id, footnote_id, description
                  FROM {local_db.SCHEMA}.footnote_descriptions_oplog
                  WHERE language_id = 'EN'
                  ORDER BY footnote_type_id, footnote_id, footnote_description_period_sid DESC
                )
                SELECT DISTINCT af.linked_to, af.attached_at, af.measure_sid,
                       af.footnote_type_id, af.footnote_id, ld.description
                FROM all_footnotes af
                LEFT JOIN latest_desc ld
                  ON ld.footnote_type_id = af.footnote_type_id
                 AND ld.footnote_id = af.footnote_id
                ORDER BY af.linked_to, af.attached_at, af.footnote_type_id, af.footnote_id
                LIMIT 40
                """,
                (chain,),
            )
            for row in cur.fetchall():
                fid = f"{row['footnote_type_id']}{row['footnote_id']}"
                attached = row.get("attached_at") or flat
                out.append(_evidence(
                    kind="footnote",
                    id=f"footnote:{fid}:{attached}:{row.get('measure_sid') or ''}",
                    title=f"Footnote {fid}",
                    body=row.get("description") or "",
                    source=f"uk.footnotes ({row['linked_to']})",
                    authority_tier=7,
                    scope=f"commodity:{attached}",
                    provenance={
                        "attached_at": attached,
                        "inherited": attached != flat,
                        "measure_sid": row.get("measure_sid"),
                    },
                ))
    except Exception as exc:
        out.append(_warning("footnotes", exc))
    return out


def _edge_kind(edge: dict) -> str:
    edge_id = edge.get("id") or ""
    etype = edge.get("type") or ""
    source = edge.get("source") or ""
    if edge_id.startswith("atar_"):
        return "atar"
    if edge_id.startswith("hsen:") or source.lower().startswith("hsen"):
        return "hsen"
    if edge_id.startswith("gir_"):
        return "gir"
    if "note" in edge_id or "note" in etype:
        return "kg_note"
    return etype or "kg_edge"


def _evidence(
    *,
    kind: str,
    id: str,
    title: str,
    body: str,
    source: str,
    authority_tier: Optional[int],
    scope: str,
    url: str | None = None,
    use_scopes: list[str] | None = None,
    evidence_roles: list[str] | None = None,
    provenance: dict | None = None,
) -> dict:
    clean_body = _clean(body)
    default_scopes, default_roles = hydration_labels(kind)
    return {
        "kind": kind,
        "id": id,
        "title": _clean(title)[:220],
        "body": clean_body[:MAX_BODY],
        "body_truncated": len(clean_body) > MAX_BODY,
        "source": source,
        "authority_tier": authority_tier,
        "use_scopes": use_scopes or default_scopes,
        "evidence_roles": evidence_roles or default_roles,
        "scope": scope,
        "url": url,
        "provenance": provenance or {},
    }


def _warning(kind: str, exc: Exception) -> dict:
    return {
        "kind": "warning",
        "id": f"warning:{kind}",
        "title": f"{kind} hydration failed",
        "body": str(exc)[:300],
        "body_truncated": False,
        "source": "hydration",
        "authority_tier": None,
        "use_scopes": ["audit"],
        "evidence_roles": ["unknown"],
        "scope": "",
        "url": None,
        "provenance": {},
    }


def _clean(text: Any) -> str:
    text = str(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0).rstrip(").,") if m else None


def _coverage(evidence: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for e in evidence:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {
        "counts_by_kind": counts,
        "has_atar": counts.get("atar", 0) > 0,
        "has_hsen": counts.get("hsen", 0) > 0,
        "has_legal_notes": counts.get("chapter_note", 0) > 0 or counts.get("section_note", 0) > 0,
        "has_footnotes": counts.get("footnote", 0) > 0,
        "has_facets": counts.get("facet", 0) > 0,
    }


def _deterministic_summary(evidence: list[dict]) -> dict:
    counts = _coverage(evidence)["counts_by_kind"]
    priority = ["chapter_note", "section_note", "hsen", "atar", "footnote", "facet", "gir", "kg_note"]
    bullets = []
    for kind in priority:
        rows = [e for e in evidence if e["kind"] == kind]
        if rows:
            bullets.append(f"{len(rows)} {kind.replace('_', ' ')} item(s): {rows[0]['title']}")
    return {
        "mode": "deterministic",
        "counts_by_kind": counts,
        "bullets": bullets[:8],
    }


def _model_name(model: str | None = None) -> str:
    selected = (model or os.environ.get("LIVE_HYDRATION_MODEL") or DEFAULT_LIVE_HYDRATION_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{2,80}", selected):
        return DEFAULT_LIVE_HYDRATION_MODEL
    return selected


def _llm_summary(commodity: dict, evidence: list[dict], *, model: str | None = None) -> dict:
    selected_model = _model_name(model)
    if not openai_allowed():
        return {
            "enabled": False,
            "model": selected_model,
            "reason": "Provider-backed hydration is disabled. Set JOURNEY_ALLOW_PROVIDER_CALLS=1 to opt in.",
        }
    if not os.environ.get("OPENAI_API_KEY"):
        return {"enabled": False, "model": selected_model, "reason": "OPENAI_API_KEY is not set."}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        evidence_text = "\n".join(
            f"- [{e['kind']}] {e['title']}: {e['body'][:500]}"
            for e in evidence
            if e["kind"] != "facet"
        )[:12000]
        kwargs: dict[str, Any] = {
            "model": selected_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Summarize tariff classification evidence for a customs demo. "
                        "Do not invent codes, facts, rates, documents, or legal conclusions. "
                        "Use only the provided evidence. Return concise bullets covering: "
                        "classification signals, risk/uncertainty, and data still needed."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Commodity: {commodity.get('code')} - {commodity.get('description')}\n\n"
                        f"Evidence:\n{evidence_text}"
                    ),
                },
            ],
        }
        if selected_model.startswith("gpt-5") or selected_model.startswith("o"):
            kwargs["reasoning_effort"] = os.environ.get("HYDRATION_REASONING_EFFORT", "low")
        else:
            kwargs["temperature"] = 0.2
        resp = client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        return {
            "enabled": True,
            "model": selected_model,
            "text": resp.choices[0].message.content,
            "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
        }
    except Exception as exc:
        return {"enabled": False, "model": selected_model, "reason": str(exc)[:300]}
