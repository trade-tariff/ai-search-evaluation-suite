"""ATaR (Advance Tariff Ruling) ingestion pipeline.

End-to-end flow:

  1. scrape_listing(N) -> [{ref, commodity_code, description_snippet, ...}]
       Pulls N ruling references from the public GOV.UK search (25 per page).

  2. fetch_ruling(ref) -> {ref, commodity_code, description, justification, ...}
       Hits /ruling/{ref} and parses the dl#ruling-details summary list.

  3. extract_facts(client, ruling) -> {gold_facts: [...], oracle_text: str}
       LLM call (gpt-5.4 low) that turns the ruling description + justification
       into a list of {slot, answer} pairs - the seeded fact sheet a candidate
       model would discover via Q&A. The full description+justification is
       returned as oracle_text so the simulator can answer anything the fact
       sheet doesn't cover.

  4. retrieve_context(client, raw_query, limit=80) -> [...]
       Reuses search.retrieve_candidates to mint the same OpenSearch+vector
       context that production AI Search would have, so an ATaR-sourced
       prompt drops into the existing benchmark unchanged.

  5. save_draft / load_drafts / approve_draft
       Drafts live in data/atar_drafts.json until a human approves them in
       the AtarPanel UI; on approval they're appended to search_contexts.json
       with gold_code + gold_facts + oracle_text + source="atar:<ref>".

This module is the single source of truth for ATaR data shapes used by
main.py's API endpoints and the AtarPanel UI.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from openai import AsyncOpenAI

from _retry import with_retry_and_limit
from search import retrieve_candidates

# Public, no-auth search service - same data as the GOV.UK customer-facing UI.
ATAR_BASE = "https://www.tax.service.gov.uk/search-for-advance-tariff-rulings"
USER_AGENT = "ai-fan-out atar-research-scraper/1.0"

DATA_DIR = Path(__file__).parent.parent / "data"
DRAFTS_PATH = DATA_DIR / "atar_drafts.json"
SEARCH_CONTEXTS_PATH = DATA_DIR / "search_contexts.json"

# gpt-5.4 low: same simulator-class model used elsewhere; cheap and good
# enough at slot canonicalisation.
EXTRACTOR_MODEL = "gpt-5.4"
EXTRACTOR_REASONING_EFFORT = "low"


# ─── Data shapes ──────────────────────────────────────────────────────────


@dataclass
class AtarListing:
    """One row from the search-results page."""
    ref: str
    commodity_code: str
    description_snippet: str
    expiry_date: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class AtarRuling:
    """Full ruling pulled from /ruling/{ref}."""
    ref: str
    commodity_code: str
    description: str
    justification: str
    keywords: list[str] = field(default_factory=list)
    start_date: str = ""
    expiry_date: str = ""

    @property
    def url(self) -> str:
        return f"{ATAR_BASE}/ruling/{self.ref}"


@dataclass
class AtarDraft:
    """A scraped+ingested ruling waiting for human approval. Once approved
    via the AtarPanel UI it's promoted to a real prompt in search_contexts.json.
    """
    ref: str
    ruling: dict  # AtarRuling fields
    raw_query: str           # the description, used as the trader's search query
    gold_code: str           # the ruling's commodity code
    oracle_text: str         # description + justification combined
    gold_facts: list[dict]   # [{slot, answer, source_question?}, ...]
    formatted_results: list[dict]  # OpenSearch top-N for raw_query
    status: str = "pending"  # "pending" | "approved" | "discarded"
    approved_prompt_index: int | None = None  # set when status="approved"


# ─── Scraping ─────────────────────────────────────────────────────────────


async def scrape_listing(limit: int = 20, page: int = 1) -> list[AtarListing]:
    """Pull `limit` rulings from the GOV.UK ATaR search results.

    The search page returns 25 per page, so a single fetch is enough for a
    20-ruling pilot. We iterate pages if the caller asks for more than 25.
    """
    listings: list[AtarListing] = []
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        page_num = page
        while len(listings) < limit:
            url = f"{ATAR_BASE}/search?{urlencode({'page': page_num})}"
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Each result row has: View ruling <ref> link, plus surrounding
            # description, expiry, commodity code, keyword tags.
            view_links = [
                a for a in soup.find_all("a") if "View ruling" in a.get_text()
            ]
            if not view_links:
                break

            for a in view_links:
                if len(listings) >= limit:
                    break
                ref = (a.get("href") or "").rsplit("/", 1)[-1]
                if not ref or not ref.isdigit():
                    continue
                # Walk up to the parent block for the row metadata.
                row = a
                for _ in range(6):
                    row = row.parent
                    if row is None:
                        break
                    text = row.get_text(" ", strip=True)
                    if "Commodity code" in text or "Expiry date" in text:
                        break
                code_match = re.search(r"\b(\d{8,10})\b", row.get_text(" ", strip=True) if row else "")
                listings.append(
                    AtarListing(
                        ref=ref,
                        commodity_code=code_match.group(1) if code_match else "",
                        description_snippet="",
                        expiry_date="",
                    )
                )
            page_num += 1
            if page_num > 50:  # safety
                break

    return listings[:limit]


async def fetch_ruling(ref: str) -> AtarRuling:
    """Hit /ruling/{ref} and pull the structured fields out of the
    govuk-summary-list."""
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        resp = await client.get(f"{ATAR_BASE}/ruling/{ref}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

    fields: dict[str, str] = {}
    keywords: list[str] = []
    dl = soup.find("dl", id="ruling-details") or soup.find("dl", class_="govuk-summary-list")
    if dl is None:
        raise ValueError(f"ruling {ref}: ruling-details list not found")

    for row in dl.find_all("div", class_="govuk-summary-list__row"):
        key_el = row.find("dt", class_="govuk-summary-list__key")
        val_el = row.find("dd", class_="govuk-summary-list__value")
        if key_el is None or val_el is None:
            continue
        key = key_el.get_text(" ", strip=True).lower()
        if key == "keywords":
            keywords = [
                t.get_text(strip=True) for t in val_el.find_all("span", class_="govuk-tag")
            ]
        else:
            fields[key] = val_el.get_text(" ", strip=True)

    code = fields.get("commodity code", "")
    code_match = re.search(r"\b(\d{8,10})\b", code)
    if code_match:
        code = code_match.group(1)

    return AtarRuling(
        ref=ref,
        commodity_code=code,
        description=fields.get("description", ""),
        justification=fields.get("justification", ""),
        keywords=keywords,
        start_date=fields.get("start date", ""),
        expiry_date=fields.get("expiry date", ""),
    )


# ─── Fact extraction ───────────────────────────────────────────────────────


_EXTRACTOR_SYSTEM = """\
You read UK tariff rulings and extract a structured "fact sheet" describing
the actual product, suitable for pre-seeding a trader simulator's per-prompt
fact store.

For each meaningful product attribute the ruling commits to (composition,
form, intended use, dimensions, packaging, function, mode of operation,
material, processing state, age/weight/size etc.), emit one fact:
  - slot: a short snake_case label (e.g. "form", "intended_use",
          "battery_type", "connectivity", "weight_g")
  - answer: the specific value extracted from the ruling, in the same
            grammatical form a trader would use when answering a multiple-
            choice question (short noun phrase or value)

Skip facts that are obvious from the raw query alone or that don't
discriminate between commodity codes. Aim for 4-8 facts. Use the same slot
label for the same concept across rulings (e.g. always "form", not
"physical_form" sometimes and "shape" other times).

Respond ONLY with valid JSON in this exact format:
{
  "facts": [
    {"slot": "<snake_case>", "answer": "<value>"},
    ...
  ]
}"""


async def extract_facts(client: AsyncOpenAI, ruling: AtarRuling) -> list[dict]:
    """Turn a ruling into a [{slot, answer}, ...] fact sheet via gpt-5.4 low.

    The output is the proposed fact sheet; a human approves/edits it in the
    AtarPanel UI before it lands on a real prompt.
    """
    user = (
        f"## Ruling reference\n{ruling.ref}\n\n"
        f"## Commodity code (gold)\n{ruling.commodity_code}\n\n"
        f"## Description (applicant)\n{ruling.description}\n\n"
        f"## Justification (HMRC)\n{ruling.justification}\n\n"
        f"## Keywords\n{', '.join(ruling.keywords) if ruling.keywords else '(none)'}"
    )
    resp = await with_retry_and_limit(
        "openai",
        lambda: client.chat.completions.create(
            model=EXTRACTOR_MODEL,
            reasoning_effort=EXTRACTOR_REASONING_EFFORT,
            messages=[
                {"role": "system", "content": _EXTRACTOR_SYSTEM},
                {"role": "user", "content": user},
            ],
        ),
    )
    text = (resp.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    facts = data.get("facts", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        slot = str(f.get("slot", "")).strip()
        answer = str(f.get("answer", "")).strip()
        if slot and answer:
            out.append({"slot": slot, "answer": answer})
    return out


# ─── Draft persistence ─────────────────────────────────────────────────────


def _read_drafts_file() -> dict:
    if not DRAFTS_PATH.exists():
        return {"drafts": []}
    try:
        return json.loads(DRAFTS_PATH.read_text())
    except json.JSONDecodeError:
        return {"drafts": []}


def _write_drafts_file(data: dict) -> None:
    DRAFTS_PATH.write_text(json.dumps(data, indent=2))


def list_drafts() -> list[dict]:
    return _read_drafts_file().get("drafts", [])


def get_draft(ref: str) -> dict | None:
    for d in list_drafts():
        if d.get("ref") == ref:
            return d
    return None


def upsert_draft(draft: AtarDraft) -> None:
    data = _read_drafts_file()
    drafts = data.get("drafts", [])
    out = [d for d in drafts if d.get("ref") != draft.ref]
    out.append(asdict(draft))
    data["drafts"] = out
    _write_drafts_file(data)


def update_draft_fields(ref: str, **fields: Any) -> dict | None:
    """Patch fields on an existing draft (e.g. user-edited gold_facts after
    approval review). Returns the updated draft, or None if not found."""
    data = _read_drafts_file()
    for d in data.get("drafts", []):
        if d.get("ref") == ref:
            d.update(fields)
            _write_drafts_file(data)
            return d
    return None


def discard_draft(ref: str) -> bool:
    data = _read_drafts_file()
    before = len(data.get("drafts", []))
    data["drafts"] = [d for d in data.get("drafts", []) if d.get("ref") != ref]
    _write_drafts_file(data)
    return len(data["drafts"]) < before


# ─── Approval: promote a draft into search_contexts.json ───────────────────


def approve_draft(ref: str, override_facts: list[dict] | None = None) -> dict:
    """Promote a draft to a real prompt in search_contexts.json. The draft's
    status is flipped to "approved" so it doesn't show up in the pending UI
    again. Returns {prompt_index, total}.

    If override_facts is provided, it replaces draft.gold_facts before
    promotion (UI lets the user edit before clicking approve).
    """
    draft = get_draft(ref)
    if draft is None:
        raise ValueError(f"draft {ref} not found")
    if draft.get("status") == "approved":
        raise ValueError(f"draft {ref} already approved as prompt #{draft.get('approved_prompt_index')}")

    if override_facts is not None:
        # Validate shape
        cleaned = []
        for f in override_facts:
            if not isinstance(f, dict):
                continue
            slot = str(f.get("slot", "")).strip()
            answer = str(f.get("answer", "")).strip()
            if slot and answer:
                cleaned.append({"slot": slot, "answer": answer})
        draft["gold_facts"] = cleaned

    contexts = json.loads(SEARCH_CONTEXTS_PATH.read_text())
    max_idx = max((q["index"] for q in contexts["queries"]), default=0)
    new_idx = max_idx + 1
    contexts["queries"].append({
        "index": new_idx,
        "raw_query": draft["raw_query"],
        "processed_query": draft["raw_query"],
        "result_count": len(draft["formatted_results"]),
        "formatted_results": draft["formatted_results"],
        "gold_code": draft["gold_code"],
        "gold_facts": draft["gold_facts"],
        "oracle_text": draft["oracle_text"],
        "source": f"atar:{ref}",
    })
    SEARCH_CONTEXTS_PATH.write_text(json.dumps(contexts, indent=2))

    # Flip draft status; bust the prompts cache.
    update_draft_fields(ref, status="approved", approved_prompt_index=new_idx)
    import prompts as prompts_mod  # noqa: WPS433 - circular avoidance
    prompts_mod._cached_data = None

    return {"prompt_index": new_idx, "total": len(contexts["queries"]), "ref": ref}


# ─── End-to-end ingest ──────────────────────────────────────────────────────


async def ingest_one(
    client: AsyncOpenAI,
    ref: str,
    *,
    opensearch_limit: int = 80,
) -> AtarDraft:
    """Full pipeline for a single ATaR reference: fetch -> extract -> retrieve
    -> save draft. The draft lands in atar_drafts.json with status="pending"
    until a human reviews it in the AtarPanel UI.
    """
    ruling = await fetch_ruling(ref)
    if not ruling.commodity_code or not ruling.description:
        raise ValueError(f"ruling {ref}: missing commodity_code or description")

    facts = await extract_facts(client, ruling)
    formatted = await retrieve_candidates(client, ruling.description, limit=opensearch_limit)

    oracle_text = ruling.description.strip()
    if ruling.justification:
        oracle_text = (
            f"{oracle_text}\n\n[HMRC justification]\n{ruling.justification.strip()}"
        )

    draft = AtarDraft(
        ref=ref,
        ruling=asdict(ruling),
        raw_query=ruling.description.strip(),
        gold_code=ruling.commodity_code,
        oracle_text=oracle_text,
        gold_facts=facts,
        formatted_results=formatted,
        status="pending",
    )
    upsert_draft(draft)
    return draft


async def ingest_batch(
    client: AsyncOpenAI,
    refs: list[str] | None = None,
    *,
    count: int = 20,
    opensearch_limit: int = 80,
    on_progress=None,
) -> list[dict]:
    """Ingest `count` rulings (or a specific list of refs). Skips refs that
    already have a draft so re-runs are idempotent."""
    if refs is None:
        listings = await scrape_listing(limit=count)
        refs = [l.ref for l in listings]

    existing = {d["ref"] for d in list_drafts()}
    out: list[dict] = []
    for i, ref in enumerate(refs, 1):
        if ref in existing:
            if on_progress:
                on_progress(i, len(refs), ref, "skipped")
            continue
        try:
            draft = await ingest_one(client, ref, opensearch_limit=opensearch_limit)
            out.append(asdict(draft))
            if on_progress:
                on_progress(i, len(refs), ref, "ingested")
        except Exception as exc:
            if on_progress:
                on_progress(i, len(refs), ref, f"error: {exc}")
    return out
