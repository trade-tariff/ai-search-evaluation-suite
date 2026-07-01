#!/usr/bin/env python3
"""Benchmark the Trade Tariff MCP classification_search against kg.eval_gold.

Read-only: reads a gold dump (JSON), calls the live MCP classification_search per
query, scores recall@k vs the expected commodity code. Writes results to a JSON
file. Does NOT touch the database or kg.eval_runs.

Env: OTT_HUB_CLIENT_ID / OTT_HUB_CLIENT_SECRET (source ~/.claude/.env first).
Usage: python bench_mcp.py <gold.json> <out.json> [per_persona_sample] [limit]
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error

TOKEN_URL = "https://auth.id.trade-tariff.service.gov.uk/oauth2/token"
MCP_URL = "https://mcp.trade-tariff.service.gov.uk/"
KS = [1, 5, 10, 20]


def get_token():
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["OTT_HUB_CLIENT_ID"],
        "client_secret": os.environ["OTT_HUB_CLIENT_SECRET"],
        "scope": "tariff/read",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def mcp_search(query, token, limit):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "classification_search",
                   "arguments": {"query": query, "limit": limit, "service": "uk"}},
    }).encode()
    req = urllib.request.Request(MCP_URL, data=payload, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    content = d.get("result", {}).get("content", [])
    if not content:
        return []
    rows = json.loads(content[0]["text"])
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("results") or []
    out = []
    for row in rows:
        attrs = row.get("attributes", row) if isinstance(row, dict) else {}
        code = attrs.get("goods_nomenclature_item_id") or attrs.get("commodity_code")
        if code:
            out.append(str(code))
    return out


def stratified(gold, n):
    by = {}
    for g in gold:
        by.setdefault(g.get("persona") or "?", []).append(g)
    sample = []
    for persona, rows in by.items():
        sample.extend(rows[:n])  # deterministic: first n (gold already shuffled at gen time)
    return sample


def rank_of(expected, got, digits=10):
    e = expected[:digits]
    for i, c in enumerate(got):
        if c[:digits] == e:
            return i + 1
    return None


def main():
    gold_path, out_path = sys.argv[1], sys.argv[2]
    per_persona = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 20
    gold = json.load(open(gold_path))
    sample = stratified(gold, per_persona)
    token = get_token()
    results = []
    t0 = time.time()
    for i, g in enumerate(sample):
        q, exp = g["query"], str(g["expected_code"])
        try:
            got = mcp_search(q, token, limit)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                token = get_token(); got = mcp_search(q, token, limit)
            else:
                got = []
                print(f"  [{i}] HTTP {e.code} on persona={g.get('persona')}", file=sys.stderr)
        except Exception as e:
            got = []
            print(f"  [{i}] {type(e).__name__}: {e}", file=sys.stderr)
        results.append({
            "persona": g.get("persona"), "expected": exp,
            "rank_d10": rank_of(exp, got, 10),
            "rank_d8": rank_of(exp, got, 8),
            "rank_d6": rank_of(exp, got, 6),
            "rank_d4": rank_of(exp, got, 4),
            "n_returned": len(got),
        })
        if (i + 1) % 20 == 0:
            print(f"  ...{i+1}/{len(sample)} ({time.time()-t0:.0f}s)", file=sys.stderr)
        time.sleep(0.05)

    def agg(rows):
        n = len(rows)
        out = {"n": n}
        for d in (10, 8, 6, 4):
            for k in KS:
                out[f"r@{k}_d{d}"] = round(sum(1 for r in rows if r[f"rank_d{d}"] and r[f"rank_d{d}"] <= k) / n, 3)
        out["mrr_d10"] = round(sum(1.0 / r["rank_d10"] for r in rows if r["rank_d10"]) / n, 3)
        out["mrr_d8"] = round(sum(1.0 / r["rank_d8"] for r in rows if r["rank_d8"]) / n, 3)
        return out

    summary = {"overall": agg(results), "by_persona": {}}
    personas = sorted(set(r["persona"] for r in results))
    for p in personas:
        summary["by_persona"][p] = agg([r for r in results if r["persona"] == p])
    summary["meta"] = {"sample": len(sample), "per_persona": per_persona, "limit": limit,
                       "elapsed_s": round(time.time() - t0, 1), "endpoint": MCP_URL}
    json.dump({"summary": summary, "results": results}, open(out_path, "w"), indent=2)

    o = summary["overall"]
    print("\n=== MCP classification_search vs kg.eval_gold (sample) ===")
    print(f"sample={o['n']}  limit={limit}  elapsed={summary['meta']['elapsed_s']}s")
    print("Recall by code-precision level (d10=exact 10-digit, d8=subheading, d6, d4=heading):")
    print(f"OVERALL  R@1(d10)={o['r@1_d10']}  R@10: d10={o['r@10_d10']} d8={o['r@10_d8']} "
          f"d6={o['r@10_d6']} d4={o['r@10_d4']}  R@20(d8)={o['r@20_d8']}  MRR(d8)={o['mrr_d8']}")
    print(f"{'persona':16}{'n':>4}{'R1d10':>7}{'R10d10':>7}{'R10d8':>7}{'R10d6':>7}{'R10d4':>7}{'R20d8':>7}")
    for p in personas:
        s = summary["by_persona"][p]
        print(f"{p:16}{s['n']:>4}{s['r@1_d10']:>7}{s['r@10_d10']:>7}{s['r@10_d8']:>7}"
              f"{s['r@10_d6']:>7}{s['r@10_d4']:>7}{s['r@20_d8']:>7}")


if __name__ == "__main__":
    main()
