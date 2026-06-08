"""Knowledge-base browser for the kg schema in tariff_db.

Powers the KnowledgePanel tab. Read mostly + light curation: delete a bad
fact / edge, edit a facet value, promote an LLM-extracted fact to 'verified'.

This module is the viewing/curating surface for the copied knowledge layer in
the local tariff database.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import asyncpg

_DSN = os.environ.get(
    "TARIFF_DB_DSN",
    "postgresql:///tariff_db",
)
KG_SCHEMA = os.environ.get("TARIFF_DB_KG_SCHEMA", "kg")
TARIFF_SCHEMA = os.environ.get("TARIFF_DB_SCHEMA", "uk")

_pool: asyncpg.Pool | None = None
_column_cache: dict[tuple[str, str], bool] = {}


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=_DSN, min_size=1, max_size=4, command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _kg_has_column(conn: asyncpg.Connection, table: str, column: str) -> bool:
    key = (table, column)
    if key not in _column_cache:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.columns
              WHERE table_schema = $1
                AND table_name = $2
                AND column_name = $3
            )
            """,
            KG_SCHEMA,
            table,
            column,
        )
        _column_cache[key] = bool(exists)
    return _column_cache[key]


# --- Coverage stats ----------------------------------------------------

async def coverage_stats() -> dict[str, Any]:
    """High-level health of the KG: total counts + per-chapter coverage."""
    pool = await _get_pool()
    async with pool.acquire() as c:
        totals = await c.fetchrow(
            f"""
            SELECT
              (SELECT count(*) FROM {KG_SCHEMA}.facet_definitions) AS facet_defs,
              (SELECT count(*) FROM {KG_SCHEMA}.commodity_facets) AS facets,
              (SELECT count(DISTINCT commodity_code) FROM {KG_SCHEMA}.commodity_facets) AS codes_with_facets,
              (SELECT count(*) FROM {KG_SCHEMA}.kg_edges) AS edges,
              (SELECT count(*) FROM {KG_SCHEMA}.kg_edge_commodities) AS edge_links
            """
        )
        per_chapter = await c.fetch(
            f"""
            SELECT LEFT(commodity_code, 2) AS chapter,
                   count(DISTINCT commodity_code) AS codes_with_facets,
                   count(*) AS total_facets,
                   count(DISTINCT source) AS distinct_sources
            FROM {KG_SCHEMA}.commodity_facets
            GROUP BY LEFT(commodity_code, 2)
            ORDER BY 1
            """
        )
        per_source = await c.fetch(
            f"""
            SELECT
              CASE
                WHEN source = 'hand' THEN 'hand'
                WHEN source = 'description_llm' THEN 'description_llm'
                WHEN source LIKE 'atar:%' THEN 'atar'
                ELSE source
              END AS source_bucket,
              count(*) AS n
            FROM {KG_SCHEMA}.commodity_facets
            GROUP BY 1 ORDER BY n DESC
            """
        )
        per_scope = await c.fetch(
            f"""
            SELECT scope, count(*) AS n
            FROM {KG_SCHEMA}.kg_edges
            GROUP BY scope ORDER BY scope
            """
        )
        per_use_scope = []
        scope_sources = []
        if await _kg_has_column(c, "commodity_facets", "use_scopes"):
            scope_sources.append(
                f"SELECT 'fact' AS kind, unnest(use_scopes) AS use_scope FROM {KG_SCHEMA}.commodity_facets"
            )
        if await _kg_has_column(c, "kg_edges", "use_scopes"):
            scope_sources.append(
                f"SELECT 'edge' AS kind, unnest(use_scopes) AS use_scope FROM {KG_SCHEMA}.kg_edges"
            )
        if scope_sources:
            per_use_scope = await c.fetch(
                f"""
                SELECT kind, use_scope, count(*) AS n
                FROM (
                  {" UNION ALL ".join(scope_sources)}
                ) scoped
                GROUP BY kind, use_scope
                ORDER BY kind, n DESC
                """
            )
        per_evidence_role = []
        role_sources = []
        if await _kg_has_column(c, "commodity_facets", "evidence_roles"):
            role_sources.append(
                f"SELECT 'fact' AS kind, unnest(evidence_roles) AS evidence_role FROM {KG_SCHEMA}.commodity_facets"
            )
        if await _kg_has_column(c, "kg_edges", "evidence_roles"):
            role_sources.append(
                f"SELECT 'edge' AS kind, unnest(evidence_roles) AS evidence_role FROM {KG_SCHEMA}.kg_edges"
            )
        if role_sources:
            per_evidence_role = await c.fetch(
                f"""
                SELECT kind, evidence_role, count(*) AS n
                FROM (
                  {" UNION ALL ".join(role_sources)}
                ) scoped
                GROUP BY kind, evidence_role
                ORDER BY kind, n DESC
                """
            )
    return {
        "totals": dict(totals) if totals else {},
        "per_chapter": [dict(r) for r in per_chapter],
        "per_source": [dict(r) for r in per_source],
        "per_scope": [dict(r) for r in per_scope],
        "per_use_scope": [dict(r) for r in per_use_scope],
        "per_evidence_role": [dict(r) for r in per_evidence_role],
    }


# --- Facets ------------------------------------------------------------

async def list_facets(
    chapter: Optional[str] = None,
    source: Optional[str] = None,
    facet_key: Optional[str] = None,
    use_scope: Optional[str] = None,
    evidence_role: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Filtered list of commodity_facets rows."""
    pool = await _get_pool()
    async with pool.acquire() as c:
        has_use_scopes = await _kg_has_column(c, "commodity_facets", "use_scopes")
        has_evidence_roles = await _kg_has_column(c, "commodity_facets", "evidence_roles")
    where = ["1=1"]
    args: list[Any] = []
    if chapter:
        where.append(f"LEFT(cf.commodity_code, 2) = ${len(args)+1}")
        args.append(chapter)
    if source:
        # 'atar' matches all atar:<ref>
        if source == "atar":
            where.append(f"cf.source LIKE ${len(args)+1}")
            args.append("atar:%")
        else:
            where.append(f"cf.source = ${len(args)+1}")
            args.append(source)
    if facet_key:
        where.append(f"cf.facet_key = ${len(args)+1}")
        args.append(facet_key)
    if use_scope and has_use_scopes:
        where.append(f"${len(args)+1} = ANY(cf.use_scopes)")
        args.append(use_scope)
    elif use_scope:
        return {"total": 0, "rows": []}
    if evidence_role and has_evidence_roles:
        where.append(f"${len(args)+1} = ANY(cf.evidence_roles)")
        args.append(evidence_role)
    elif evidence_role:
        return {"total": 0, "rows": []}
    if q:
        where.append(
            f"(cf.commodity_code ILIKE ${len(args)+1} "
            f"OR cf.facet_key ILIKE ${len(args)+1} "
            f"OR cf.facet_value ILIKE ${len(args)+1})"
        )
        args.append(f"%{q}%")

    where_clause = " AND ".join(where)
    args_for_data = args + [limit, offset]
    use_scopes_expr = "cf.use_scopes" if has_use_scopes else "ARRAY[]::text[] AS use_scopes"
    evidence_roles_expr = "cf.evidence_roles" if has_evidence_roles else "ARRAY[]::text[] AS evidence_roles"
    async with pool.acquire() as c:
        total = await c.fetchval(
            f"SELECT count(*) FROM {KG_SCHEMA}.commodity_facets cf WHERE {where_clause}",
            *args,
        )
        rows = await c.fetch(
            f"""
            SELECT cf.id, cf.commodity_code, cf.facet_key, cf.facet_value,
                   cf.source, cf.confidence::float AS confidence, cf.evidence, cf.created_at,
                   cf.authority_tier, {use_scopes_expr}, {evidence_roles_expr}, cf.provenance,
                   fd.label AS facet_label
            FROM {KG_SCHEMA}.commodity_facets cf
            LEFT JOIN {KG_SCHEMA}.facet_definitions fd ON fd.key = cf.facet_key
            WHERE {where_clause}
            ORDER BY cf.commodity_code, cf.facet_key
            LIMIT ${len(args)+1} OFFSET ${len(args)+2}
            """,
            *args_for_data,
        )
    return {"total": int(total or 0), "rows": [dict(r) for r in rows]}


async def list_facet_definitions() -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            f"""
            SELECT fd.key, fd.label, fd.short_label, fd.value_set, fd.applies_to_chapters, fd.rank,
                   (SELECT count(*) FROM {KG_SCHEMA}.commodity_facets cf WHERE cf.facet_key = fd.key) AS uses
            FROM {KG_SCHEMA}.facet_definitions fd
            ORDER BY uses DESC, fd.rank, fd.key
            """
        )
        return [dict(r) for r in rows]


async def update_facet(
    facet_id: int,
    value: Optional[str] = None,
    confidence: Optional[float] = None,
    source: Optional[str] = None,
    use_scopes: Optional[list[str]] = None,
    evidence_roles: Optional[list[str]] = None,
) -> Optional[dict]:
    """Edit a single fact (e.g. fix LLM mistake, promote to 'verified')."""
    pool = await _get_pool()
    async with pool.acquire() as c:
        has_use_scopes = await _kg_has_column(c, "commodity_facets", "use_scopes")
        has_evidence_roles = await _kg_has_column(c, "commodity_facets", "evidence_roles")
    sets = []
    args: list[Any] = []
    if value is not None:
        sets.append(f"facet_value = ${len(args)+1}")
        args.append(value)
    if confidence is not None:
        sets.append(f"confidence = ${len(args)+1}")
        args.append(confidence)
    if source is not None:
        sets.append(f"source = ${len(args)+1}")
        args.append(source)
    if use_scopes is not None and has_use_scopes:
        sets.append(f"use_scopes = ${len(args)+1}::text[]")
        args.append(use_scopes)
    if evidence_roles is not None and has_evidence_roles:
        sets.append(f"evidence_roles = ${len(args)+1}::text[]")
        args.append(evidence_roles)
    if not sets:
        return None
    args.append(facet_id)
    async with pool.acquire() as c:
        row = await c.fetchrow(
            f"UPDATE {KG_SCHEMA}.commodity_facets SET {', '.join(sets)} WHERE id = ${len(args)} RETURNING *",
            *args,
        )
        return dict(row) if row else None


async def delete_facet(facet_id: int) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as c:
        r = await c.execute(f"DELETE FROM {KG_SCHEMA}.commodity_facets WHERE id = $1", facet_id)
        return r.startswith("DELETE 1")


# --- KG edges ----------------------------------------------------------

async def list_edges(
    scope: Optional[str] = None,
    type_: Optional[str] = None,
    use_scope: Optional[str] = None,
    evidence_role: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    pool = await _get_pool()
    async with pool.acquire() as c:
        has_use_scopes = await _kg_has_column(c, "kg_edges", "use_scopes")
        has_evidence_roles = await _kg_has_column(c, "kg_edges", "evidence_roles")
    where = ["1=1"]
    args: list[Any] = []
    if scope:
        where.append(f"scope = ${len(args)+1}")
        args.append(scope)
    if type_:
        where.append(f"type = ${len(args)+1}")
        args.append(type_)
    if use_scope and has_use_scopes:
        where.append(f"${len(args)+1} = ANY(use_scopes)")
        args.append(use_scope)
    elif use_scope:
        return {"total": 0, "rows": []}
    if evidence_role and has_evidence_roles:
        where.append(f"${len(args)+1} = ANY(evidence_roles)")
        args.append(evidence_role)
    elif evidence_role:
        return {"total": 0, "rows": []}
    if q:
        where.append(
            f"(id ILIKE ${len(args)+1} OR title ILIKE ${len(args)+1} OR body ILIKE ${len(args)+1})"
        )
        args.append(f"%{q}%")
    where_clause = " AND ".join(where)
    use_scopes_expr = "use_scopes" if has_use_scopes else "ARRAY[]::text[] AS use_scopes"
    evidence_roles_expr = "evidence_roles" if has_evidence_roles else "ARRAY[]::text[] AS evidence_roles"
    async with pool.acquire() as c:
        total = await c.fetchval(
            f"SELECT count(*) FROM {KG_SCHEMA}.kg_edges WHERE {where_clause}",
            *args,
        )
        rows = await c.fetch(
            f"""
            SELECT id, type, scope, title, body, source, created_at,
                   authority_tier, {use_scopes_expr}, {evidence_roles_expr}, provenance,
                   (SELECT count(*) FROM {KG_SCHEMA}.kg_edge_commodities kec WHERE kec.edge_id = kg_edges.id) AS n_linked_codes
            FROM {KG_SCHEMA}.kg_edges
            WHERE {where_clause}
            ORDER BY scope, id
            LIMIT ${len(args)+1} OFFSET ${len(args)+2}
            """,
            *(args + [limit, offset]),
        )
        return {"total": int(total or 0), "rows": [dict(r) for r in rows]}


async def get_edge(edge_id: str) -> Optional[dict]:
    pool = await _get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            f"SELECT * FROM {KG_SCHEMA}.kg_edges WHERE id = $1", edge_id,
        )
        if not row:
            return None
        linked = await c.fetch(
            f"SELECT commodity_code FROM {KG_SCHEMA}.kg_edge_commodities WHERE edge_id = $1 ORDER BY commodity_code",
            edge_id,
        )
        return {**dict(row), "linked_codes": [r["commodity_code"] for r in linked]}


async def update_edge(
    edge_id: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    scope: Optional[str] = None,
    type_: Optional[str] = None,
    use_scopes: Optional[list[str]] = None,
    evidence_roles: Optional[list[str]] = None,
) -> Optional[dict]:
    pool = await _get_pool()
    async with pool.acquire() as c:
        has_use_scopes = await _kg_has_column(c, "kg_edges", "use_scopes")
        has_evidence_roles = await _kg_has_column(c, "kg_edges", "evidence_roles")
    sets = []
    args: list[Any] = []
    for col, v in (("title", title), ("body", body), ("scope", scope), ("type", type_)):
        if v is not None:
            sets.append(f"{col} = ${len(args)+1}")
            args.append(v)
    if use_scopes is not None and has_use_scopes:
        sets.append(f"use_scopes = ${len(args)+1}::text[]")
        args.append(use_scopes)
    if evidence_roles is not None and has_evidence_roles:
        sets.append(f"evidence_roles = ${len(args)+1}::text[]")
        args.append(evidence_roles)
    if not sets:
        return await get_edge(edge_id)
    args.append(edge_id)
    async with pool.acquire() as c:
        await c.execute(
            f"UPDATE {KG_SCHEMA}.kg_edges SET {', '.join(sets)} WHERE id = ${len(args)}",
            *args,
        )
    return await get_edge(edge_id)


async def audit_log_for(table_name: str, row_id: str, limit: int = 50) -> list[dict]:
    """Recent audit entries for one row (facet or edge)."""
    pool = await _get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            f"""
            SELECT id, action, old_value, new_value, actor, reason, created_at
            FROM {KG_SCHEMA}.audit_log
            WHERE table_name = $1 AND row_id = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            table_name, row_id, limit,
        )
        return [dict(r) for r in rows]


async def audit_log_recent(limit: int = 100, action: Optional[str] = None) -> list[dict]:
    pool = await _get_pool()
    where = []
    args: list = []
    if action:
        where.append(f"action = ${len(args)+1}")
        args.append(action)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    async with pool.acquire() as c:
        rows = await c.fetch(
            f"""
            SELECT id, table_name, row_id, action, actor, reason, created_at
            FROM {KG_SCHEMA}.audit_log
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ${len(args)+1}
            """,
            *args, limit,
        )
        return [dict(r) for r in rows]


async def delete_edge(edge_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as c:
        r = await c.execute(f"DELETE FROM {KG_SCHEMA}.kg_edges WHERE id = $1", edge_id)
        return r.startswith("DELETE 1")


# --- Per-commodity drill-down -----------------------------------------

# --- Graph (cytoscape.js elements) -------------------------------------

async def graph_elements(
    focus_code: Optional[str] = None,
    chapter: Optional[str] = None,
    all_mode: bool = False,
    rule_types: Optional[list[str]] = None,
    sources: Optional[list[str]] = None,
    scopes: Optional[list[str]] = None,
    include_gaps: bool = False,
    gap_chapters: Optional[list[str]] = None,
    max_nodes: int = 200,
) -> dict[str, Any]:
    """Build a cytoscape.js elements payload.

    Three modes:
      - focus_code: neighbourhood around one CC (code + facets + applicable rules)
      - chapter: all facetted CCs in chapter + rules scoped to them
      - all_mode=True: every rule node + every CC linked to a rule. Use filters.
    """
    import re
    pool = await _get_pool()
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_nodes: set[str] = set()

    def add_node(node_id: str, label: str, type_: str, **data):
        if node_id in seen_nodes: return
        seen_nodes.add(node_id)
        nodes.append({
            "data": {"id": node_id, "label": label, "type": type_, **data}
        })

    def add_edge(source: str, target: str, label: str = "", type_: str = "applies_to"):
        edges.append({
            "data": {
                "id": f"{source}__{target}__{type_}",
                "source": source, "target": target,
                "label": label, "type": type_,
            }
        })

    async with pool.acquire() as c:
        if focus_code:
            flat = re.sub(r"\D", "", focus_code).ljust(10, "0")[:10]
            row = await c.fetchrow(
                f"""
                SELECT gn.goods_nomenclature_item_id AS code, gnd.description
                FROM {TARIFF_SCHEMA}.goods_nomenclatures gn
                JOIN {TARIFF_SCHEMA}.goods_nomenclature_descriptions gnd
                  ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
                WHERE gn.goods_nomenclature_item_id = $1 AND gn.validity_end_date IS NULL
                LIMIT 1
                """,
                flat,
            )
            if not row:
                return {"nodes": [], "edges": [], "stats": {"reason": f"code {flat} not active"}}

            add_node(flat, flat, "code", chapter=flat[:2], description=row["description"])

            # Facets as one bag node per code (avoid clutter)
            facets = await c.fetch(
                f"""
                SELECT facet_key, facet_value, source FROM {KG_SCHEMA}.commodity_facets
                WHERE commodity_code = $1
                ORDER BY facet_key
                """,
                flat,
            )
            if facets:
                bag_id = f"{flat}__facets"
                pairs = ", ".join(f"{f['facet_key']}={f['facet_value']}" for f in facets[:8])
                add_node(bag_id, f"{len(facets)} facts", "facet_bag",
                         chapter=flat[:2], summary=pairs)
                add_edge(flat, bag_id, "has_facts", "has_facts")

            # KG edges that apply
            chapter_id = flat[:2]
            edges_rs = await c.fetch(
                f"""
                SELECT DISTINCT e.id, e.type, e.scope, e.title, e.body, e.source
                FROM {KG_SCHEMA}.kg_edges e
                LEFT JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = e.id
                WHERE kec.commodity_code = $1
                   OR e.scope = $2
                   OR e.scope = 'global'
                ORDER BY e.scope, e.id
                LIMIT $3
                """,
                flat, f"chapter:{chapter_id}", max_nodes,
            )
            for e in edges_rs:
                rid = f"rule:{e['id']}"
                add_node(rid, (e["title"] or e["id"])[:70], "rule",
                         scope=e["scope"], rule_type=e["type"], source=e["source"], body=e["body"][:400])
                add_edge(rid, flat, e["type"], "rule_applies")
        elif chapter:
            # All facetted CCs in chapter + their rules + cross-links
            cc_rows = await c.fetch(
                f"""
                SELECT DISTINCT cf.commodity_code,
                       (SELECT description FROM {TARIFF_SCHEMA}.goods_nomenclature_descriptions gnd
                        JOIN {TARIFF_SCHEMA}.goods_nomenclatures gn
                          ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                        WHERE gn.goods_nomenclature_item_id = cf.commodity_code
                          AND gn.validity_end_date IS NULL LIMIT 1) AS description,
                       count(*) AS n_facets
                FROM {KG_SCHEMA}.commodity_facets cf
                WHERE LEFT(cf.commodity_code, 2) = $1
                GROUP BY cf.commodity_code
                ORDER BY n_facets DESC, cf.commodity_code
                LIMIT $2
                """,
                chapter, max_nodes,
            )
            for r in cc_rows:
                code = r["commodity_code"]
                add_node(code, code, "code", chapter=code[:2],
                         description=r["description"], n_facets=r["n_facets"])

            # Rules scoped to the chapter, or explicitly linked to any of these codes
            chapter_codes = [r["commodity_code"] for r in cc_rows]
            edges_rs = await c.fetch(
                f"""
                SELECT DISTINCT e.id, e.type, e.scope, e.title, e.body, e.source,
                       array_agg(DISTINCT kec.commodity_code) FILTER (WHERE kec.commodity_code = ANY($2)) AS linked_codes
                FROM {KG_SCHEMA}.kg_edges e
                LEFT JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = e.id
                WHERE e.scope = $1 OR kec.commodity_code = ANY($2)
                GROUP BY e.id, e.type, e.scope, e.title, e.body, e.source
                """,
                f"chapter:{chapter}", chapter_codes,
            )
            for e in edges_rs:
                rid = f"rule:{e['id']}"
                add_node(rid, (e["title"] or e["id"])[:60], "rule",
                         scope=e["scope"], rule_type=e["type"], source=e["source"], body=e["body"][:400])
                if e["linked_codes"]:
                    for code in e["linked_codes"]:
                        if code in seen_nodes:
                            add_edge(rid, code, e["type"], "rule_link_explicit")
                else:
                    # Chapter-wide rule - link to every code shown
                    for code in chapter_codes:
                        add_edge(rid, code, e["type"], "rule_link_chapter")
        elif all_mode:
            # All KG: every rule node, every CC with a fact, every link.
            # Optional filters.
            where_rule = []
            args: list[Any] = []
            if rule_types:
                where_rule.append(f"e.type = ANY(${len(args)+1})")
                args.append(rule_types)
            if scopes:
                where_rule.append(f"e.scope = ANY(${len(args)+1})")
                args.append(scopes)
            rule_where_sql = (" WHERE " + " AND ".join(where_rule)) if where_rule else ""

            rule_rows = await c.fetch(
                f"""
                SELECT e.id, e.type, e.scope, e.title, e.body, e.source,
                       (SELECT count(*) FROM {KG_SCHEMA}.kg_edge_commodities kec WHERE kec.edge_id = e.id) AS n_linked
                FROM {KG_SCHEMA}.kg_edges e
                {rule_where_sql}
                ORDER BY e.scope, e.id
                LIMIT $%d
                """ % (len(args) + 1),
                *args, max_nodes,
            )
            for e in rule_rows:
                rid = f"rule:{e['id']}"
                add_node(rid, (e["title"] or e["id"])[:50], "rule",
                         scope=e["scope"], rule_type=e["type"], source=e["source"],
                         body=(e["body"] or "")[:400], n_linked=e["n_linked"])

            # Pull explicit edge_commodities links AND show the code nodes they target
            rule_ids = [e["id"] for e in rule_rows]
            if rule_ids:
                link_rows = await c.fetch(
                    f"""
                    SELECT kec.edge_id, kec.commodity_code,
                           (SELECT description FROM {TARIFF_SCHEMA}.goods_nomenclature_descriptions gnd
                            JOIN {TARIFF_SCHEMA}.goods_nomenclatures gn
                              ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                            WHERE gn.goods_nomenclature_item_id = kec.commodity_code
                              AND gn.validity_end_date IS NULL LIMIT 1) AS description,
                           (SELECT count(*) FROM {KG_SCHEMA}.commodity_facets cf
                            WHERE cf.commodity_code = kec.commodity_code) AS n_facets
                    FROM {KG_SCHEMA}.kg_edge_commodities kec
                    WHERE kec.edge_id = ANY($1)
                    """,
                    rule_ids,
                )
                for r in link_rows:
                    code = r["commodity_code"]
                    add_node(code, code, "code", chapter=code[:2],
                             description=r["description"] or "", n_facets=r["n_facets"])
                    add_edge(f"rule:{r['edge_id']}", code, "applies_to", "rule_link")

            # Also pull code nodes with facets even if no explicit link (so we see the breadth)
            if not rule_types and not scopes:
                fact_codes = await c.fetch(
                    f"""
                    SELECT cf.commodity_code,
                           (SELECT description FROM {TARIFF_SCHEMA}.goods_nomenclature_descriptions gnd
                            JOIN {TARIFF_SCHEMA}.goods_nomenclatures gn
                              ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                            WHERE gn.goods_nomenclature_item_id = cf.commodity_code
                              AND gn.validity_end_date IS NULL LIMIT 1) AS description,
                           count(*) AS n_facets
                    FROM {KG_SCHEMA}.commodity_facets cf
                    GROUP BY cf.commodity_code
                    ORDER BY n_facets DESC
                    LIMIT $1
                    """,
                    max_nodes,
                )
                for r in fact_codes:
                    add_node(r["commodity_code"], r["commodity_code"], "code",
                             chapter=r["commodity_code"][:2],
                             description=r["description"] or "", n_facets=r["n_facets"])

            # Coverage gaps: include active codes from the requested chapter(s) that have
            # neither facets nor explicit edge links - rendered as grey to flag uncovered slice.
            if include_gaps and gap_chapters:
                gap_rows = await c.fetch(
                    f"""
                    SELECT gn.goods_nomenclature_item_id AS code,
                           gnd.description AS description
                    FROM {TARIFF_SCHEMA}.goods_nomenclatures gn
                    JOIN {TARIFF_SCHEMA}.goods_nomenclature_descriptions gnd
                      ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
                    WHERE gn.validity_end_date IS NULL
                      AND LEFT(gn.goods_nomenclature_item_id, 2) = ANY($1)
                      AND NOT EXISTS (
                        SELECT 1 FROM {KG_SCHEMA}.commodity_facets cf
                        WHERE cf.commodity_code = gn.goods_nomenclature_item_id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM {KG_SCHEMA}.kg_edge_commodities kec
                        WHERE kec.commodity_code = gn.goods_nomenclature_item_id
                      )
                    LIMIT $2
                    """,
                    gap_chapters, max(max_nodes // 2, 100),
                )
                for r in gap_rows:
                    add_node(r["code"], r["code"], "code_gap",
                             chapter=r["code"][:2],
                             description=r["description"] or "",
                             n_facets=0)
        else:
            return {"nodes": [], "edges": [], "stats": {"reason": "pass focus_code, chapter, or all_mode=true"}}

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {"n_nodes": len(nodes), "n_edges": len(edges)},
    }


async def commodity_view(code: str) -> dict[str, Any]:
    """Everything we know about a single commodity code."""
    pool = await _get_pool()
    import re
    flat = re.sub(r"\D", "", code).ljust(10, "0")[:10]
    async with pool.acquire() as c:
        has_facet_use_scopes = await _kg_has_column(c, "commodity_facets", "use_scopes")
        has_edge_use_scopes = await _kg_has_column(c, "kg_edges", "use_scopes")
        has_facet_evidence_roles = await _kg_has_column(c, "commodity_facets", "evidence_roles")
        has_edge_evidence_roles = await _kg_has_column(c, "kg_edges", "evidence_roles")
        facet_use_scopes_expr = "cf.use_scopes" if has_facet_use_scopes else "ARRAY[]::text[] AS use_scopes"
        edge_use_scopes_expr = "e.use_scopes" if has_edge_use_scopes else "ARRAY[]::text[] AS use_scopes"
        facet_evidence_roles_expr = "cf.evidence_roles" if has_facet_evidence_roles else "ARRAY[]::text[] AS evidence_roles"
        edge_evidence_roles_expr = "e.evidence_roles" if has_edge_evidence_roles else "ARRAY[]::text[] AS evidence_roles"
        meta = await c.fetchrow(
            f"""
            SELECT gn.goods_nomenclature_item_id AS code,
                   gnd.description AS description
            FROM {TARIFF_SCHEMA}.goods_nomenclatures gn
            JOIN {TARIFF_SCHEMA}.goods_nomenclature_descriptions gnd
              ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
            WHERE gn.goods_nomenclature_item_id = $1
              AND gn.validity_end_date IS NULL
            LIMIT 1
            """,
            flat,
        )
        facets = await c.fetch(
            f"""
            SELECT cf.id, cf.facet_key, cf.facet_value, cf.source, cf.confidence::float AS confidence,
                   cf.evidence, {facet_use_scopes_expr}, {facet_evidence_roles_expr}, fd.label AS facet_label
            FROM {KG_SCHEMA}.commodity_facets cf
            LEFT JOIN {KG_SCHEMA}.facet_definitions fd ON fd.key = cf.facet_key
            WHERE cf.commodity_code = $1
            ORDER BY fd.rank NULLS LAST, cf.facet_key
            """,
            flat,
        )
        # Edges: explicit links + chapter-scope edges
        chapter = flat[:2]
        edges = await c.fetch(
            f"""
            SELECT DISTINCT e.id, e.type, e.scope, e.title, e.body, e.source, {edge_use_scopes_expr}, {edge_evidence_roles_expr}
            FROM {KG_SCHEMA}.kg_edges e
            LEFT JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = e.id
            WHERE kec.commodity_code = $1
               OR e.scope = $2
               OR e.scope = 'global'
            ORDER BY e.scope, e.id
            """,
            flat, f"chapter:{chapter}",
        )
    return {
        "code": flat,
        "description": (meta["description"] if meta else None),
        "facets": [dict(r) for r in facets],
        "edges": [dict(r) for r in edges],
    }
