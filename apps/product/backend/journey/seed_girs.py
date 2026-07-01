"""Add General Interpretive Rules 1-6 to the KG as global-scope edges."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
for p in (
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent.parent/'.env',
):
    if p is not None and p.exists(): load_dotenv(p)

import psycopg
from psycopg.rows import dict_row
try:
    from .evidence_labels import edge_labels
except ImportError:
    from evidence_labels import edge_labels

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
DATA_DIR = Path(__file__).parent / "data"


def main():
    doc = json.loads((DATA_DIR / "girs.json").read_text())
    girs = doc["girs"]
    with psycopg.connect(DSN, row_factory=dict_row) as c, c.cursor() as cur:
        for g in girs:
            body = g["body"]
            if g.get("applies_when"):
                body += f"\n\nApplies when: {g['applies_when']}"
            use_scopes, evidence_roles = edge_labels(
                "classification_order",
                1,
                source="Combined Nomenclature / Tariff of the United Kingdom Part Two - General Interpretive Rules",
                edge_id=g["id"],
                scope="global",
            )
            cur.execute(
                """
                INSERT INTO kg.kg_edges
                  (id, type, scope, title, body, source, authority_tier, use_scopes, evidence_roles, provenance)
                VALUES (%s, %s, %s, %s, %s, %s, 1, %s::text[], %s::text[], %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                  type=EXCLUDED.type, scope=EXCLUDED.scope, title=EXCLUDED.title,
                  body=EXCLUDED.body, source=EXCLUDED.source,
                  authority_tier=EXCLUDED.authority_tier,
                  use_scopes=EXCLUDED.use_scopes,
                  evidence_roles=EXCLUDED.evidence_roles,
                  provenance=EXCLUDED.provenance,
                  updated_at=now()
                """,
                (
                    g["id"],
                    "classification_order",
                    "global",
                    g["title"],
                    body,
                    "Combined Nomenclature / Tariff of the United Kingdom Part Two - General Interpretive Rules",
                    use_scopes,
                    evidence_roles,
                    json.dumps({"source_type": "gir", "source_id": g["id"], "scope_ref": "global"}),
                ),
            )
        c.commit()
        cur.execute("SELECT count(*) AS n FROM kg.kg_edges WHERE scope='global'")
        print(f"GIRs seeded. Total global-scope edges: {cur.fetchone()['n']}")


if __name__ == "__main__":
    main()
