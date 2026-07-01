#!/usr/bin/env python3
"""Create run_classify_matrix_ids.py from run_classify_matrix.py by injecting a
GOLD_IDS allowlist filter into the gold SELECT in run_config().

GOLD_IDS env = comma-separated gold ids. When set, both branches of the SELECT
(limit / no-limit) get an extra `AND id = ANY(%s)` and the id-list is appended
to params. Idempotent: rewrites the variant each run.
"""
import re, pathlib

SRC = pathlib.Path("/app/backend/journey/run_classify_matrix.py")
DST = pathlib.Path("/app/backend/journey/run_classify_matrix_ids.py")

src = SRC.read_text()

# 1. Add a module-level GOLD_IDS parse right after DSN definition.
anchor = 'DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")'
inject = anchor + '''

# --- gold-id allowlist (experiment variant) -------------------------------
# GOLD_IDS = comma-separated kg.eval_gold ids. When set, run_config restricts
# its gold SELECT to exactly these ids (and ignores --personas/--limit framing
# for membership, though they still apply as additional WHERE clauses).
_GOLD_IDS_ENV = os.environ.get("GOLD_IDS", "").strip()
GOLD_IDS = [int(x) for x in _GOLD_IDS_ENV.split(",") if x.strip()] if _GOLD_IDS_ENV else None'''
assert anchor in src, "DSN anchor not found"
src = src.replace(anchor, inject, 1)

# 2. Replace the SELECT-building block in run_config with an allowlist-aware one.
old_block = '''    with conn.cursor() as cur:
        sql = (
            "SELECT id, source_id, source_type, persona, query, expected_code FROM kg.eval_gold "
            "WHERE source_type='atar' AND persona = ANY(%s) ORDER BY id"
        )
        params: list = [personas]
        if limit:'''

new_block = '''    with conn.cursor() as cur:
        if GOLD_IDS:
            # Experiment mode: exactly the allowlisted gold ids, ignore persona/limit framing.
            cur.execute(
                "SELECT id, source_id, source_type, persona, query, expected_code "
                "FROM kg.eval_gold WHERE source_type='atar' AND id = ANY(%s) ORDER BY id",
                (GOLD_IDS,),
            )
            gold_rows = [dict(r) for r in cur.fetchall()]
            print(f"[{run_label}] GOLD_IDS allowlist -> {len(gold_rows)} rows")
            return await _run_rows(conn, gold_rows, run_label=run_label, strategy=strategy,
                                   prompt_mode=prompt_mode, augmentation=augmentation,
                                   model=model, candidate_limit=candidate_limit,
                                   concurrency=concurrency, max_rounds=max_rounds)
        sql = (
            "SELECT id, source_id, source_type, persona, query, expected_code FROM kg.eval_gold "
            "WHERE source_type='atar' AND persona = ANY(%s) ORDER BY id"
        )
        params: list = [personas]
        if limit:'''
assert old_block in src, "run_config SELECT block not found"
src = src.replace(old_block, new_block, 1)

# 3. Refactor the post-SELECT body of run_config into a helper _run_rows so the
#    allowlist branch can reuse it. Find the line after gold_rows fetch and split.
marker = "        cur.execute(sql, tuple(params))\n        gold_rows = [dict(r) for r in cur.fetchall()]\n"
assert marker in src, "post-select fetch marker not found"
# Everything from `    loo_map = build_loo_map(...)` to the end of run_config's return
# is the body we want in _run_rows. We locate it and wrap.
body_start = src.index("    loo_map = build_loo_map(conn, gold_rows)")
# run_config ends right before `def _print_summary`
body_end = src.index("\ndef _print_summary")
body = src[body_start:body_end]

# Build _run_rows from the body (same code, just a named function).
helper = ("\nasync def _run_rows(conn, gold_rows, *, run_label, strategy, prompt_mode,\n"
          "                    augmentation, model, candidate_limit, concurrency, max_rounds):\n"
          "    os.environ['CLASSIFY_LLM_MODEL'] = model\n"
          + body + "\n")

# Replace original body with: set model env + delegate to _run_rows.
delegate = ("    return await _run_rows(conn, gold_rows, run_label=run_label, strategy=strategy,\n"
            "                           prompt_mode=prompt_mode, augmentation=augmentation,\n"
            "                           model=model, candidate_limit=candidate_limit,\n"
            "                           concurrency=concurrency, max_rounds=max_rounds)\n")
src = src[:body_start] + delegate + src[body_end:]

# Insert helper just before _print_summary.
src = src.replace("\ndef _print_summary", helper + "\ndef _print_summary", 1)

DST.write_text(src)
print("wrote", DST)
