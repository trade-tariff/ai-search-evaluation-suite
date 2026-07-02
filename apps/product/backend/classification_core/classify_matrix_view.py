from __future__ import annotations

import math
import os

from . import local_db


def _wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion, as (lo, hi) fractions."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))

_PERSONA_ORDER = [
    "naive_vague",
    "naive_branded",
    "naive_specific",
    "emu_generic",
    "emu_ordinary",
    "emu_specific",
    "original",
]
_PERSONA_SHORT = {
    "naive_vague": "naive<br>vague",
    "naive_branded": "naive<br>branded",
    "naive_specific": "naive<br>specific",
    "emu_generic": "expert<br>generic",
    "emu_ordinary": "expert<br>ordinary",
    "emu_specific": "expert<br>specific",
    "original": "ATAR<br>original",
}
_PERSONA_TIPS = {
    "naive_vague": "Novice trader, vague wording - e.g. 'metal machine part'",
    "naive_branded": "Novice trader, brand/everyday wording",
    "naive_specific": "Novice trader, specific wording",
    "emu_generic": "Expert wording but generic - e.g. 'mechanical seal'",
    "emu_ordinary": "Expert wording, ordinary level of detail",
    "emu_specific": "Expert wording, highly specific",
    "original": "The full ATAR product description - the most complete possible input",
}


def eval_classify_matrix() -> str:
    import psycopg
    from psycopg.rows import dict_row

    porder = _PERSONA_ORDER
    rows_by_label: dict[str, dict] = {}
    have_table = True
    try:
        with psycopg.connect(local_db.DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_label, persona,
                       count(*) AS n,
                       avg(gold_in_final_set::int) AS in_set,
                       avg(gold_in_top1::int)      AS top1,
                       avg(gold_in_top5::int)      AS top5,
                       sum(gold_in_final_set::int) AS in_set_n,
                       sum(gold_in_top1::int)      AS top1_n,
                       sum(est_cost_usd)           AS total_cost,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY gold_rank)
                         FILTER (WHERE gold_rank IS NOT NULL) AS med_rank,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY survivor_set_size) AS med_size,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY rounds) AS med_rounds,
                       avg(est_cost_usd) AS avg_cost,
                       max(strategy) AS strategy, max(prompt_mode) AS prompt_mode,
                       max(augmentation) AS augmentation, max(model) AS model
                FROM kg.classify_runs
                GROUP BY run_label, persona
                """
            )
            agg = [dict(r) for r in cur.fetchall()]
    except Exception:
        have_table = False
        agg = []

    meta: dict[str, dict] = {}
    for r in agg:
        lbl = r["run_label"]
        d = rows_by_label.setdefault(lbl, {})
        d[r["persona"]] = r
        meta.setdefault(
            lbl,
            {
                "strategy": r["strategy"],
                "prompt_mode": r["prompt_mode"],
                "augmentation": r["augmentation"],
                "model": r["model"],
            },
        )
    baseline_label = (os.environ.get("CLASSIFY_BASELINE_RUN_LABEL") or "").strip()
    have_baseline = baseline_label in rows_by_label
    labels = sorted(
        rows_by_label,
        key=lambda l: (l != baseline_label if have_baseline else False,
                       meta[l]["strategy"] != "eliminate", l),
    )

    def _cell(r, baseline_row=None, is_baseline_label=False):
        if not r:
            return '<td style="background:#1f2937;color:#6b7280;text-align:center">-</td>'
        v = float(r["in_set"] or 0)
        if v >= 0.9:
            bg, fg = "#064e3b", "#6ee7b7"
        elif v >= 0.8:
            bg, fg = "#065f46", "#a7f3d0"
        elif v >= 0.7:
            bg, fg = "#3f6212", "#d9f99d"
        elif v >= 0.6:
            bg, fg = "#854d0e", "#fde68a"
        elif v >= 0.4:
            bg, fg = "#7c2d12", "#fed7aa"
        else:
            bg, fg = "#7f1d1d", "#fecaca"
        rank = r["med_rank"]
        n = int(r["n"] or 0)
        lo, hi = _wilson(int(r.get("in_set_n") or 0), n)
        top1_n = int(r.get("top1_n") or 0)
        total_cost = float(r.get("total_cost") or 0)
        cost_per_correct = (total_cost / top1_n) if top1_n else None
        tip = (
            f"n={n} | gold-in-set {v * 100:.0f}% (95% CI {lo * 100:.0f}-{hi * 100:.0f}%) "
            f"| top1 {float(r['top1'] or 0) * 100:.0f}% "
            f"| top5 {float(r['top5'] or 0) * 100:.0f}% | med rank "
            f"{('%.0f' % rank) if rank is not None else '-'} "
            f"| med survivors {float(r['med_size'] or 0):.0f} | med rounds "
            f"{float(r['med_rounds'] or 0):.1f} | est ${float(r['avg_cost'] or 0):.4f}/session"
            + (f" | ${cost_per_correct:.4f}/correct-top1" if cost_per_correct is not None else "")
        )
        sub = f"t1 {float(r['top1'] or 0) * 100:.0f}%"
        if baseline_row and not is_baseline_label:
            delta = (v - float(baseline_row["in_set"] or 0)) * 100
            colour = "#6ee7b7" if delta >= 0 else "#fca5a5"
            sub += f' &middot; <span style="color:{colour}">{delta:+.0f}pp vs live</span>'
        return (
            f'<td title="{tip}" style="background:{bg};color:{fg};text-align:center;'
            f'font-variant-numeric:tabular-nums"><b>{v * 100:.0f}%</b>'
            f'<div style="font-size:9px;opacity:.85">{sub}</div></td>'
        )

    header_cells = "".join(
        f'<th title="{_PERSONA_TIPS.get(p, "")}" style="text-align:center;font-size:11px;'
        f'line-height:1.2">{_PERSONA_SHORT.get(p, p)}</th>'
        for p in porder
    )
    body_rows = []
    baseline_rows = rows_by_label.get(baseline_label, {}) if have_baseline else {}
    for lbl in labels:
        m = meta[lbl]
        prow = rows_by_label[lbl]
        is_base = have_baseline and lbl == baseline_label
        cells = "".join(
            _cell(prow.get(p), baseline_rows.get(p), is_baseline_label=is_base)
            for p in porder
        )
        strat_badge = "#6ee7b7" if m["strategy"] == "eliminate" else "#93c5fd"
        base_badge = (
            '<span style="background:#1d4ed8;color:#dbeafe;font-size:9px;padding:1px 6px;'
            'border-radius:8px;margin-right:6px;vertical-align:middle">LIVE BASELINE</span>'
            if is_base else ""
        )
        body_rows.append(
            f'<tr><td class="rl">{base_badge}<span class="lbl" style="color:{strat_badge}">'
            f'{m["strategy"]}</span> &middot; {m["prompt_mode"]} &middot; {m["augmentation"]}'
            f'<span class="key">{lbl} ({m["model"]})</span></td>{cells}</tr>'
        )

    css = (
        "body{margin:0;background:#0b0f19;color:#e5e7eb;font-family:Inter,system-ui,sans-serif;padding:28px 36px}"
        "h1{font-size:24px;margin:0 0 4px}.sub{color:#9ca3af;font-size:13px;margin-bottom:22px;max-width:920px;line-height:1.5}"
        "table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:8px}"
        "th,td{border:1px solid #1f2937;padding:7px 10px}thead th{background:#111827;color:#cbd5e1;font-weight:600}"
        ".axis{font-size:11px;color:#6b7280;letter-spacing:1px;text-transform:uppercase;margin:18px 0 6px}"
        "td.rl{text-align:left;white-space:nowrap}.lbl{font-weight:600}"
        ".key{display:block;font-family:monospace;font-size:9.5px;color:#5b6677;margin-top:1px}"
        "thead th{cursor:help}td[title]{cursor:help}"
        ".empty{background:#0f1629;border:1px solid #1f2937;border-radius:10px;padding:24px;color:#9ca3af;font-size:14px;line-height:1.6}"
        "code{background:#111827;padding:2px 6px;border-radius:4px;font-size:12px;color:#93c5fd}"
    )

    if not have_table or not labels:
        msg = (
            "<div class='empty'>No classification-matrix runs yet. Populate "
            "<code>kg.classify_runs</code> with the harness:<br><br>"
            "<code>cd apps/product/backend &amp;&amp; .venv/bin/python -m classification_core.run_classify_matrix "
            "--run-label baseline_converge --strategy converge --prompt-mode baseline "
            "--augmentation facts+kg --model gpt-5-mini --personas naive_vague --limit 5</code>"
            "</div>"
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>Classification matrix</title>"
            "<style>"
            + css
            + "</style></head><body><h1>Classification matrix</h1>"
            "<div class='sub'>Disambiguation analogue of the retrieval matrix: each cell is "
            "<b>gold-in-final-set %</b> (presence) for one config x persona.</div>"
            + msg
            + "</body></html>"
        )

    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Classification matrix</title>"
        "<style>"
        + css
        + "</style></head><body>"
        "<h1>Classification matrix</h1>"
        "<div class='sub'>The disambiguation analogue of the retrieval matrix. Rows = a Q&amp;A "
        "<b>config</b> (strategy &middot; prompt_mode &middot; augmentation); columns = personas "
        "(same item, 7 phrasings: novice/vague &rarr; ATAR-original). Each cell = <b>gold-in-final-set %</b> "
        "&mdash; how often the correct code is present anywhere in the final committed/surviving set "
        "(presence, the primary metric). Hover a cell for median rank, survivor-set size, rounds and "
        "estimated cost/session. <b>eliminate</b> rows (green badge) fix the candidate set at round 1 "
        "and only rule out; <b>converge</b> rows re-retrieve each round. Greener = better; blank = that "
        "config x persona not run yet.</div>"
        "<div class='axis'>&larr; same item, 7 phrasings: novice / vague &nbsp;...&nbsp; expert / ATAR-original &rarr;</div>"
        "<table><thead><tr><th style='text-align:left'>config (strategy &middot; prompt_mode &middot; augmentation)</th>"
        + header_cells
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></body></html>"
    )
