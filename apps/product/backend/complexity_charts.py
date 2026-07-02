"""Server-rendered complexity charts as PNGs.

Browser-side recharts can't handle 14k+ DOM nodes — it gets slow and crashy.
This module renders the chapter scatter (and the per-template density chart)
to PNG with matplotlib so the frontend can just <img src="..."> them.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
from typing import Any

_RUNTIME_CACHE_ROOT = Path(
    os.environ.get("AI_FAN_OUT_RUNTIME_CACHE_DIR")
    or Path(os.environ.get("TMPDIR", "/tmp")) / "ai-fan-out"
)
os.environ.setdefault(
    "MPLCONFIGDIR",
    str(_RUNTIME_CACHE_ROOT / "matplotlib"),
)
os.environ.setdefault(
    "XDG_CACHE_HOME",
    str(_RUNTIME_CACHE_ROOT / "xdg"),
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

DATA_DIR = Path(__file__).parent.parent / "data" / "intercept_runs"

# Single source of truth for composite weights - do not redefine here.
from intercept_kpis import DEFAULT_WEIGHTS

TEMPLATE_COLORS = {
    "Generic": "#3b82f6",
    "Hard-to-classify": "#f59e0b",
    "Escalate": "#ef4444",
}
CONTEXT_COLOR = "#a855f7"


def _stable_jitter(key: str, width: float = 0.6) -> float:
    """Match the frontend stableJitter — deterministic [-width/2, width/2)."""
    h = 0
    for c in key:
        h = ((h << 5) - h + ord(c)) & 0xFFFFFFFF
    return ((h % 0x10000) / 0x10000 - 0.5) * width


def _composite(row: dict[str, Any], weights: dict[str, float] | None = None) -> float:
    w = weights or DEFAULT_WEIGHTS
    n = row.get("n_results") or 0
    n_sec = row.get("n_section") or 0
    n_chap = row.get("n_chapter") or 0
    s = (n_sec - 1) / max(min(n, 21) - 1, 1) if n_sec > 0 else 0
    c = (n_chap - 1) / max(min(n, 99) - 1, 1) if n_chap > 0 else 0
    q = (row.get("questions_expected") or 0) / math.log2(n) if n > 1 else 0
    u = (row.get("unresolved_digits") or 0) / 10
    v = row.get("vagueness") or 0
    is_trader = row.get("query_strategy") != "self_text"
    rf = max(0, (5 - min(n, 5)) / 5) if is_trader else 0
    wsum = (
        w["section_spread"] * s
        + w["chapter_spread"] * c
        + w["questions_expected"] * q
        + w["unresolved_digits"] * u
        + w["score_flatness"] * (row.get("score_flatness") or 0)
        + w["other_leaf_share"] * (row.get("other_leaf_share") or 0)
        + w["vagueness"] * v
    )
    return (1 - rf) * wsum + rf * 1.0


# Full-parsing 788MB sweep files OOM-kills small hosts; above this size only
# a details-stripped run_<id>.rows.json sidecar is acceptable.
_FULL_PARSE_MAX_BYTES = int(os.environ.get("INTERCEPT_RUN_OPEN_MAX_MB", "200")) * 1024 * 1024


def _load_run(run_id: str) -> dict[str, Any]:
    path = DATA_DIR / f"run_{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Run not found: {run_id}")
    if path.stat().st_size > _FULL_PARSE_MAX_BYTES:
        sidecar = DATA_DIR / f"run_{run_id}.rows.json"
        if sidecar.exists():
            return json.loads(sidecar.read_text())
        raise FileNotFoundError(
            f"Run {run_id} is too large to open on this host and has no .rows.json sidecar"
        )
    return json.loads(path.read_text())


def _load_scatter_companion(run_id: str) -> list[dict[str, Any]] | None:
    """Companion has saved composite + bucket fields, no raw KPIs."""
    path = DATA_DIR / f"run_{run_id}.scatter.json"
    if not path.exists():
        return None
    return json.loads(path.read_text()).get("points") or []


def _find_term_analysis_run() -> str | None:
    """Find the most recent term-analysis run for the 728 overlay."""
    candidates = []
    for f in DATA_DIR.glob("run_*.json"):
        if ".scatter" in f.name or ".meta" in f.name or ".rows" in f.name:
            continue
        try:
            # Metadata sidecar first; never full-parse an oversized run just
            # to read its kind (the 788MB sweeps OOM-killed this scan).
            meta = f.parent / (f.name[: -len(".json")] + ".meta.json")
            if meta.exists():
                d = json.loads(meta.read_text())
            elif f.stat().st_size > _FULL_PARSE_MAX_BYTES:
                continue
            else:
                d = json.loads(f.read_text())
        except Exception:
            continue
        kind = d.get("kind")
        if kind in (None, "term_analysis"):
            n_terms = d.get("n_terms") or len(d.get("rows", []))
            if 500 < n_terms < 1500:  # the 728 intercept-term sweep
                candidates.append((d.get("saved_at", ""), f.stem.replace("run_", "")))
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else None


def render_chapter_scatter(sweep_id: str, png_only: bool = True) -> bytes:
    """Render the big chapter scatter (commodity sweep + 728 overlay + MISS).

    Returns the PNG bytes. Designed to be served directly from a FastAPI
    endpoint as image/png.
    """
    sweep_data = _load_scatter_companion(sweep_id)
    if sweep_data is None:
        raise FileNotFoundError(f"Scatter companion missing for {sweep_id}")

    # Grey baseline + Context dependant
    grey_x, grey_y = [], []
    purple_x, purple_y = [], []
    for p in sweep_data:
        comp = p.get("composite")
        ch = p.get("chapter")
        if comp is None or not ch:
            continue
        try:
            ch_int = int(ch)
        except (TypeError, ValueError):
            continue
        x = ch_int + _stable_jitter(p.get("code") or "")
        grey_x.append(x)
        grey_y.append(comp)
        if p.get("intercept_type") == "description.guidance":
            purple_x.append(x)
            purple_y.append(comp)

    # 728 overlay
    term_id = _find_term_analysis_run()
    overlays: dict[str, tuple[list[float], list[float]]] = {
        "Generic": ([], []),
        "Hard-to-classify": ([], []),
        "Escalate": ([], []),
    }
    if term_id:
        td = _load_run(term_id)
        for r in td.get("rows", []):
            tmpl = r.get("template")
            if tmpl not in overlays:
                continue
            comp = _composite(r)
            if not math.isfinite(comp):
                continue
            top_chap = (r.get("top_chapter") or "")[:2]
            try:
                ch_int = int(top_chap)
                inMiss = False
            except (TypeError, ValueError):
                ch_int = None
                inMiss = True
            j = _stable_jitter(r.get("term") or "")
            if inMiss:
                x = 103 + j * 5
                # tiny y-fan so 23 stacked n=0 rows don't pile pixel-perfect
                y = max(0.86, comp - abs(j) * 0.12)
            else:
                x = ch_int + j
                y = comp
            overlays[tmpl][0].append(x)
            overlays[tmpl][1].append(y)

    # ---- Plot ----
    fig, ax = plt.subplots(figsize=(16, 6), facecolor="#0b1220")
    ax.set_facecolor("#0b1220")

    # MISS gutter shading
    ax.axvspan(100, 105, color="#dc2626", alpha=0.06)
    ax.axvline(100, color="#7f1d1d", linestyle="--", linewidth=0.8, alpha=0.7)

    # Grey commodity baseline — every dot, no subsampling. Matplotlib eats this.
    ax.scatter(grey_x, grey_y, s=6, color="#9ca3af", alpha=0.35, linewidths=0, zorder=1)

    # Context dependant rings (purple outline, no fill so grey shows through)
    ax.scatter(
        purple_x,
        purple_y,
        s=18,
        facecolors="none",
        edgecolors=CONTEXT_COLOR,
        linewidths=0.7,
        alpha=0.55,
        zorder=2,
    )

    # 728 template circles
    for tmpl, (xs, ys) in overlays.items():
        if not xs:
            continue
        ax.scatter(
            xs,
            ys,
            s=22,
            color=TEMPLATE_COLORS[tmpl],
            alpha=0.9,
            edgecolors="#0b1220",
            linewidths=0.4,
            zorder=3,
            label=f"Intercept: {tmpl} (n={len(xs)})",
        )

    # Legend bits for the baseline + rings
    ax.scatter([], [], s=18, color="#9ca3af", alpha=0.45, label=f"Commodity codes (n={len(grey_x):,})")
    ax.scatter(
        [],
        [],
        s=22,
        facecolors="none",
        edgecolors=CONTEXT_COLOR,
        linewidths=0.7,
        label=f"Context dependant (n={len(purple_x):,})",
    )

    ax.set_xlim(0, 106)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Chapter", color="#d1d5db")
    ax.set_ylabel("Composite complexity", color="#d1d5db")
    ticks = [1, 10, 20, 30, 40, 50, 60, 70, 80, 90, 99, 103]
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(t) if t != 103 else "MISS" for t in ticks])
    ax.tick_params(colors="#9ca3af")
    for spine in ax.spines.values():
        spine.set_color("#374151")
    ax.grid(True, color="#1f2937", linewidth=0.5)
    leg = ax.legend(
        facecolor="#111827",
        edgecolor="#374151",
        labelcolor="#d1d5db",
        loc="upper center",
        bbox_to_anchor=(0.5, 1.13),
        ncol=5,
        fontsize=9,
        frameon=True,
    )
    for txt in leg.get_texts():
        txt.set_color("#d1d5db")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def render_template_density(sweep_id: str) -> bytes:
    """Per-template composite-density histogram overlaid on commodity baseline."""
    sweep_data = _load_scatter_companion(sweep_id)
    if sweep_data is None:
        raise FileNotFoundError(f"Scatter companion missing for {sweep_id}")
    term_id = _find_term_analysis_run()
    if not term_id:
        raise FileNotFoundError("No term-analysis (728) run available")

    commodity_scores = [
        p["composite"] for p in sweep_data
        if isinstance(p.get("composite"), (int, float))
    ]
    td = _load_run(term_id)
    by_tmpl: dict[str, list[float]] = {"Generic": [], "Hard-to-classify": [], "Escalate": []}
    for r in td.get("rows", []):
        tmpl = r.get("template")
        if tmpl in by_tmpl:
            by_tmpl[tmpl].append(_composite(r))

    fig, ax = plt.subplots(figsize=(14, 5), facecolor="#0b1220")
    ax.set_facecolor("#0b1220")
    bins = np.arange(0, 1.02, 0.02)
    ax.hist(
        commodity_scores,
        bins=bins,
        density=True,
        alpha=0.4,
        color="#9ca3af",
        label=f"Commodities (n={len(commodity_scores):,})",
    )
    for tmpl, vals in by_tmpl.items():
        if not vals:
            continue
        ax.hist(
            vals,
            bins=bins,
            density=True,
            alpha=0.55,
            color=TEMPLATE_COLORS[tmpl],
            label=f"{tmpl} (n={len(vals)})",
        )
    ax.set_xlabel("Composite complexity", color="#d1d5db")
    ax.set_ylabel("Within-series density", color="#d1d5db")
    ax.tick_params(colors="#9ca3af")
    for spine in ax.spines.values():
        spine.set_color("#374151")
    ax.grid(True, color="#1f2937", linewidth=0.5)
    leg = ax.legend(facecolor="#111827", edgecolor="#374151", labelcolor="#d1d5db")
    for txt in leg.get_texts():
        txt.set_color("#d1d5db")
    ax.set_title("Composite complexity — per template vs commodity baseline", color="#f3f4f6")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# Tiny on-disk cache keyed by run id so the second view is instant.
_CACHE_DIR = Path(
    os.environ.get("AI_FAN_OUT_CHART_CACHE_DIR")
    or _RUNTIME_CACHE_ROOT / "complexity_charts"
)
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_chart(kind: str, sweep_id: str) -> bytes:
    """Return cached PNG bytes for (kind, sweep_id), rendering if missing."""
    key = hashlib.md5(f"{kind}|{sweep_id}".encode()).hexdigest()
    cache_file = _CACHE_DIR / f"{key}.png"
    if cache_file.exists():
        return cache_file.read_bytes()
    if kind == "scatter":
        png = render_chapter_scatter(sweep_id)
    elif kind == "density":
        png = render_template_density(sweep_id)
    else:
        raise ValueError(f"Unknown chart kind: {kind}")
    cache_file.write_bytes(png)
    return png


def invalidate_cache() -> int:
    """Wipe the chart PNG cache (call when underlying data changes)."""
    n = 0
    for f in _CACHE_DIR.glob("*.png"):
        f.unlink()
        n += 1
    return n
