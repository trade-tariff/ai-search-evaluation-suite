"""Deterministic NOTE-CLAUSE GRAPH for UK tariff commodity-code retrieval.

Two things in one module:

  (A) note_routing_map(): parse every kg.kg_edges row of type='exclusion' and turn its
      free-text `body` into a ROUTING TARGET. Chapter/section notes say things like
      "This chapter does not cover ... (heading 9603)" or "(Section XI)". The phrase in
      parentheses (or inline) is the heading/chapter/section the goods actually belong to.
      We extract that target with regex (handling both markdown-link form
      "heading [9603](/headings/9603)" and plain "heading 9603" / "Chapter 30" /
      "Section XI"), then expand it to the set of declarable (suffix 80) 10-digit leaf
      codes underneath it. Result: {source scope -> set(target leaf codes)}.

  (B) main(): an OFFLINE rescue analysis, same shape as measure_residual_backfill.py.
      Reads stored top_codes from a finished eval run (NO retrieval re-run, free+fast).
      For each MISS, asks: does the note-graph make the gold reachable by EXPANDING a
      retrieved candidate (a top-N candidate sits in a source chapter whose exclusion
      note routes to a branch that contains the gold)? Counts rescues -> recall lift.
      Also estimates DEMOTION value: how many WRONG candidates sit inside a branch that
      a source-chapter exclusion would correctly push them out of.

Read-only. In-memory only. No writes to any table.

Run:
  .venv/bin/python -m journey.note_clause_graph                 (rescue analysis, run 64)
  .venv/bin/python -m journey.note_clause_graph --dump-routing  (print the routing map)

Env: RUN_LABEL (default ai_semantic_composite_triage), PERSONA (default naive_vague),
     TARIFF_DB_DSN.
"""
import os
import re
import sys
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
RUN_LABEL = os.environ.get("RUN_LABEL", "ai_semantic_composite_triage")
PERSONA = os.environ.get("PERSONA", "naive_vague")
K = 100  # cap on candidates considered; stored top lists may be shorter (we use what's there)

# Roman numeral -> int, for "Section XVII" style targets. Sections only run I..XXI.
_ROMAN = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
    "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17,
    "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
}

# --- target-extraction regexes -------------------------------------------------
# A 4-digit heading, either bare or as a markdown link target /headings/NNNN.
#   "heading 9603", "headings 8401 to 8479", "heading [9603](/headings/9603)"
_HEADING_RE = re.compile(r"\bheadings?\b[^.;]*?", re.IGNORECASE)
_FOURDIGIT_RE = re.compile(r"\b(\d{4})\b")
# A chapter: "Chapter 30", "Chapter [96](/chapters/96)".
_CHAPTER_RE = re.compile(r"\bChapters?\s+\[?(\d{1,2})\]?", re.IGNORECASE)
# A section: "Section XV", "Section XVII".
_SECTION_RE = re.compile(r"\bSection\s+([IVXLC]+)\b")


def norm(c: str) -> str:
    d = "".join(ch for ch in (c or "") if ch.isdigit())
    return d.ljust(10, "0")[:10] if d else (c or "")


def chapter2(s: str) -> str:
    """Normalise a chapter number ('5', '05', 5) to zero-padded 2-digit string."""
    d = "".join(ch for ch in str(s) if ch.isdigit())
    return d.zfill(2)[:2] if d else ""


def heading4(code: str) -> str:
    """First 4 digits (the heading) of a normalised 10-digit code."""
    return norm(code)[:4]


def parse_targets(body: str):
    """Extract routing targets from one exclusion-note body.

    Returns a dict with three sets of *prefixes*:
      {'headings': {'9603', ...}, 'chapters': {'30', ...}, 'sections': {'XI', ...}}
    Headings win where present (most specific); we still also capture chapter/section
    refs because some notes route only to a chapter or section.
    """
    headings, chapters, sections = set(), set(), set()

    # Headings: find every "heading(s) ... <runs of 4-digit numbers>". We scan each
    # "heading" mention and grab the 4-digit groups that immediately follow it, up to
    # the next clause boundary (. ; ) so we don't swallow numbers from later clauses.
    for m in re.finditer(r"\bheadings?\b", body, re.IGNORECASE):
        tail = body[m.end(): m.end() + 80]
        # stop at a hard clause boundary
        tail = re.split(r"[.;]", tail, maxsplit=1)[0]
        for fd in _FOURDIGIT_RE.findall(tail):
            headings.add(fd)

    for m in _CHAPTER_RE.finditer(body):
        chapters.add(chapter2(m.group(1)))

    for m in _SECTION_RE.finditer(body):
        rom = m.group(1).upper()
        if rom in _ROMAN:
            sections.add(rom)

    return {"headings": headings, "chapters": chapters, "sections": sections}


def _load_leaf_index(cur):
    """code -> heading(4-digit) for every current declarable (suffix 80) leaf, plus a
    heading->leaves and chapter->leaves index for expansion."""
    cur.execute("""
      SELECT goods_nomenclature_item_id AS code
      FROM uk.goods_nomenclatures
      WHERE validity_end_date IS NULL AND producline_suffix = '80'
    """)
    leaf_heading = {}
    leaves_by_heading = defaultdict(set)
    leaves_by_chapter = defaultdict(set)
    for r in cur.fetchall():
        code = norm(r["code"])
        h = code[:4]
        ch = code[:2]
        leaf_heading[code] = h
        leaves_by_heading[h].add(code)
        leaves_by_chapter[ch].add(code)
    return leaf_heading, leaves_by_heading, leaves_by_chapter


def _load_section_chapters(cur):
    """section roman numeral -> set of 2-digit chapter prefixes."""
    cur.execute("""
      SELECT s.numeral AS numeral, left(g.goods_nomenclature_item_id, 2) AS chapter
      FROM uk.sections s
      JOIN uk.chapters_sections cs ON cs.section_id = s.id
      JOIN uk.goods_nomenclatures g ON g.goods_nomenclature_sid = cs.goods_nomenclature_sid
    """)
    out = defaultdict(set)
    for r in cur.fetchall():
        if r["chapter"]:
            out[r["numeral"].upper()].add(r["chapter"])
    return out


def note_routing_map(cur, headings_only=False):
    """Build {source_scope -> set(target leaf codes)} from all exclusion edges.

    source_scope is normalised: 'chapter:NN' (2-digit) or 'section:<roman-upper>'.
    If headings_only=True, only HEADING targets (4-digit, ~6 leaves each) are routed;
    whole-chapter and whole-section targets are dropped. This is the high-precision
    variant: a heading exclusion is a sharp pointer; "goods of Chapter 39" is not.
    Returns (routing, leaf_heading, leaves_by_heading, leaves_by_chapter, section_chapters, stats).
    """
    leaf_heading, leaves_by_heading, leaves_by_chapter = _load_leaf_index(cur)
    section_chapters = _load_section_chapters(cur)

    cur.execute("SELECT id, scope, body FROM kg.kg_edges WHERE type='exclusion'")
    edges = cur.fetchall()

    routing = defaultdict(set)
    stats = {
        "edges": len(edges), "edges_with_target": 0,
        "target_headings": 0, "target_chapters": 0, "target_sections": 0,
        "leaves_routed": 0, "edges_no_target": [],
    }

    def norm_scope(scope: str) -> str:
        scope = (scope or "").strip()
        if scope.lower().startswith("chapter:"):
            return "chapter:" + chapter2(scope.split(":", 1)[1])
        if scope.lower().startswith("section:"):
            roman = scope.split(":", 1)[1].strip().upper()
            return "section:" + roman
        return scope

    for e in edges:
        src = norm_scope(e["scope"])
        tg = parse_targets(e["body"] or "")
        had = False

        for h in tg["headings"]:
            if h in leaves_by_heading:
                routing[src] |= leaves_by_heading[h]
                stats["target_headings"] += 1
                had = True
        if not headings_only:
            for ch in tg["chapters"]:
                if ch in leaves_by_chapter:
                    routing[src] |= leaves_by_chapter[ch]
                    stats["target_chapters"] += 1
                    had = True
            for sec in tg["sections"]:
                for ch in section_chapters.get(sec, set()):
                    routing[src] |= leaves_by_chapter.get(ch, set())
                if section_chapters.get(sec):
                    stats["target_sections"] += 1
                    had = True

        if had:
            stats["edges_with_target"] += 1
        else:
            stats["edges_no_target"].append((e["id"], (e["body"] or "")[:70]))

    stats["leaves_routed"] = sum(len(v) for v in routing.values())
    return routing, leaf_heading, leaves_by_heading, leaves_by_chapter, section_chapters, stats


# --- offline rescue analysis ---------------------------------------------------

def _resolve_run_id(cur) -> int:
    rid = os.environ.get("RUN_ID")
    if rid:
        return int(rid)
    cur.execute("""
      SELECT r.id, (SELECT count(*) FROM kg.eval_run_results rr WHERE rr.run_id=r.id) AS rows
      FROM kg.eval_runs r
      WHERE r.run_label = %s AND r.finished_at IS NOT NULL
      ORDER BY rows DESC, r.finished_at DESC
      LIMIT 1
    """, (RUN_LABEL,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"No finished run with label {RUN_LABEL!r}")
    return int(row["id"])


def _candidate_scopes(code, leaf_heading, section_chapters):
    """The source scopes whose exclusion notes apply to this candidate code:
    its own chapter, plus any section that chapter belongs to."""
    ch = norm(code)[:2]
    scopes = {f"chapter:{ch}"}
    for sec, chs in section_chapters.items():
        if ch in chs:
            scopes.add(f"section:{sec}")
    return scopes


def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)
    cur = conn.cursor()

    headings_only = "--headings-only" in sys.argv
    routing, leaf_heading, leaves_by_heading, leaves_by_chapter, section_chapters, stats = \
        note_routing_map(cur, headings_only=headings_only)

    if "--dump-routing" in sys.argv:
        print("=== NOTE-CLAUSE ROUTING MAP ===")
        print(f"exclusion edges:            {stats['edges']}")
        print(f"edges yielding a target:    {stats['edges_with_target']}")
        print(f"  heading targets:          {stats['target_headings']}")
        print(f"  chapter targets:          {stats['target_chapters']}")
        print(f"  section targets:          {stats['target_sections']}")
        print(f"source scopes routed:       {len(routing)}")
        print(f"total target leaves routed: {stats['leaves_routed']}")
        print(f"\nsample (scope -> #target leaves):")
        for scope in sorted(routing)[:25]:
            print(f"  {scope:14s} -> {len(routing[scope])} leaves")
        print(f"\nedges with NO extractable target ({len(stats['edges_no_target'])}):")
        for eid, body in stats["edges_no_target"][:15]:
            print(f"  {eid:24s} {body}")
        return

    run_id = _resolve_run_id(cur)

    cur.execute("""
      SELECT r.expected_code, r.top_codes, r.rank_of_expected
      FROM kg.eval_run_results r JOIN kg.eval_gold g ON g.id = r.gold_id
      WHERE r.run_id = %s AND g.persona = %s
    """, (run_id, PERSONA))
    rows = cur.fetchall()
    n = len(rows)
    if n == 0:
        print(f"run {run_id} persona {PERSONA}: NO stored per-query results.")
        return

    # candidate-list depth actually stored (the residual-backfill comparison uses the same)
    depths = [len(r["top_codes"] or []) for r in rows]
    stored_depth = max(depths) if depths else 0
    eff_k = min(K, stored_depth)

    base_hits = 0
    misses = []
    for r in rows:
        rank = r["rank_of_expected"]
        if rank is not None and rank <= K:
            base_hits += 1
        else:
            misses.append(r)

    rescued = []
    expansion_sizes = []
    demotions = 0          # wrong candidates an exclusion would correctly push out
    demotion_queries = 0   # queries where >=1 demotion fires
    for r in rows:
        gold = norm(r["expected_code"])
        gold_h = gold[:4]
        top = [norm(c) for c in (r["top_codes"] or [])[:K]]
        topset = set(top)

        # union of routing targets reachable by expanding the retrieved candidates
        expansion = set()
        for c in top:
            for scope in _candidate_scopes(c, leaf_heading, section_chapters):
                expansion |= routing.get(scope, set())
        expansion -= topset

        # --- demotion signal: a wrong candidate c sits in chapter X, and chapter X's
        # exclusion note routes goods OUT to some other branch. If the gold is NOT in
        # chapter X but c is, then c is in a chapter the gold's class is excluded from
        # -> c is a structurally wrong neighbour the exclusion edge can demote.
        q_demotes = 0
        for c in top:
            if c == gold:
                continue
            c_ch = c[:2]
            if c_ch == gold[:2]:
                continue  # same chapter as gold; not a cross-chapter confusion
            scopes = _candidate_scopes(c, leaf_heading, section_chapters)
            targets = set()
            for s in scopes:
                targets |= routing.get(s, set())
            # the exclusion from c's chapter points to where gold actually lives
            if gold in targets:
                q_demotes += 1
        demotions += q_demotes
        if q_demotes:
            demotion_queries += 1

        if r in misses:
            expansion_sizes.append(len(expansion))
            if gold in expansion:
                # cost = how many candidates had to be injected to reach this gold
                rescued.append((gold, len(expansion)))

    avg_exp = sum(expansion_sizes) / len(expansion_sizes) if expansion_sizes else 0
    rescue_costs = sorted(c for _, c in rescued)
    median_cost = rescue_costs[len(rescue_costs) // 2] if rescue_costs else 0

    print(f"NOTE-CLAUSE GRAPH rescue analysis  {'[HEADINGS-ONLY]' if headings_only else '[full]'}")
    print(f"run_id={run_id} label={RUN_LABEL!r} persona={PERSONA!r}")
    print(f"stored candidate depth:       {stored_depth} (eval used top-{eff_k})")
    print("-" * 60)
    print(f"routing map: {stats['edges_with_target']}/{stats['edges']} exclusion edges -> "
          f"{len(routing)} source scopes -> {stats['leaves_routed']} target leaves")
    print("-" * 60)
    print(f"base recall@{K}:              {base_hits}/{n} = {base_hits/n:.3f}")
    print(f"misses:                       {len(misses)}")
    print(f"note-graph expansion added:   avg {avg_exp:.1f} target leaves per miss")
    print(f"RESCUED by note-graph:        {len(rescued)}")
    new_recall = (base_hits + len(rescued)) / n
    print(f"new recall (base+rescue):     {base_hits + len(rescued)}/{n} = {new_recall:.3f}  "
          f"(+{len(rescued)/n*100:.1f}pp)")
    print(f"rescue expansion cost:        median {median_cost} candidates injected to land the gold")
    print(f"  cheap rescues (<=30 added): {sum(1 for c in rescue_costs if c <= 30)}")
    print(f"  costly rescues (>30 added): {sum(1 for c in rescue_costs if c > 30)}")
    print(f"rescued (gold, #injected): {[ (g, c) for g, c in rescued[:25] ]}")
    print("-" * 60)
    print(f"DEMOTION estimate (all {n} queries):")
    print(f"  wrong cross-chapter candidates an exclusion routes past gold: {demotions}")
    print(f"  queries with >=1 such demotion:                              {demotion_queries}/{n}")


if __name__ == "__main__":
    main()
