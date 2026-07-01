#!/usr/bin/env python3
"""Score + compare the two eval arms on the same 210 gold sample.

Usage: score.py <sample210.json> <armA.json> <armB_results_dir> [armA_label] [armB_label]
- armA.json : list of {id, expected, final, persona, calls, q, secs}
- armB_dir  : files <id>.json = {commodity_code, gir, alternatives, questions_asked}
Outputs a comparison table (top-1 at 10/8/6/4-digit, per persona) + agreement.
"""
import json, os, sys, glob

LEVELS = [10, 8, 6, 4]


def load_arm_a(path):
    out = {}
    for r in json.load(open(path)):
        out[r["id"]] = (str(r.get("final") or "") or None, r.get("calls"), r.get("q"))
    return out


def load_arm_b(d):
    out = {}
    for f in glob.glob(os.path.join(d, "*.json")):
        rid = int(os.path.basename(f)[:-5])
        try:
            j = json.load(open(f))
        except Exception:
            j = {}
        code = (str(j.get("commodity_code") or "").replace(" ", "")) or None
        out[rid] = (code, None, j.get("questions_asked"))
    return out


def match(pred, exp, digits):
    return bool(pred) and pred[:digits] == exp[:digits]


def score(arm, sample):
    n = len(sample)
    have = sum(1 for r in sample if arm.get(r["id"], (None,))[0])
    res = {"n": n, "with_code": have}
    for d in LEVELS:
        res["top1_d%d" % d] = round(sum(1 for r in sample if match(arm.get(r["id"], (None,))[0], r["expected_code"], d)) / n, 3)
    calls = [arm[r["id"]][1] for r in sample if arm.get(r["id"]) and arm[r["id"]][1] is not None]
    qs = [arm[r["id"]][2] for r in sample if arm.get(r["id"]) and arm[r["id"]][2] is not None]
    res["mean_calls"] = round(sum(calls) / len(calls), 1) if calls else None
    res["mean_q"] = round(sum(qs) / len(qs), 1) if qs else None
    return res


def by_persona(arm, sample):
    out = {}
    for p in sorted(set(r["persona"] for r in sample)):
        rows = [r for r in sample if r["persona"] == p]
        out[p] = {"n": len(rows),
                  "d10": round(sum(1 for r in rows if match(arm.get(r["id"], (None,))[0], r["expected_code"], 10)) / len(rows), 2),
                  "d4": round(sum(1 for r in rows if match(arm.get(r["id"], (None,))[0], r["expected_code"], 4)) / len(rows), 2)}
    return out


def main():
    sample = json.load(open(sys.argv[1]))
    A = load_arm_a(sys.argv[2])
    B = load_arm_b(sys.argv[3])
    la = sys.argv[4] if len(sys.argv) > 4 else "gpt-5.5 (EC2)"
    lb = sys.argv[5] if len(sys.argv) > 5 else "Claude (local)"
    sa, sb = score(A, sample), score(B, sample)

    def row(lbl, s):
        return ("%-18s n=%d code=%d | top1: d10=%.3f d8=%.3f d6=%.3f d4=%.3f | calls=%s q=%s"
                % (lbl, s["n"], s["with_code"], s["top1_d10"], s["top1_d8"], s["top1_d6"], s["top1_d4"], s["mean_calls"], s["mean_q"]))
    print("=== END-TO-END CLASSIFICATION (210 gold, same sample) ===")
    print(row(la, sa)); print(row(lb, sb))

    print("\n--- top1 exact (d10) by persona ---")
    pa, pb = by_persona(A, sample), by_persona(B, sample)
    print("%-16s %4s %8s %8s" % ("persona", "n", la.split()[0], lb.split()[0]))
    for p in sorted(pa):
        print("%-16s %4d %8.2f %8.2f" % (p, pa[p]["n"], pa[p]["d10"], pb[p]["d10"]))

    # agreement
    both = [r for r in sample if A.get(r["id"], (None,))[0] and B.get(r["id"], (None,))[0]]
    same10 = sum(1 for r in both if A[r["id"]][0] == B[r["id"]][0])
    same4 = sum(1 for r in both if A[r["id"]][0][:4] == B[r["id"]][0][:4])
    print("\n--- inter-model agreement (rows where both gave a code, n=%d) ---" % len(both))
    print("identical 10-digit: %.2f | same heading (4): %.2f" % (same10 / len(both), same4 / len(both)))

    # disagreements with gold but right heading (likely defensible)
    for lbl, arm in ((la, A), (lb, B)):
        rh = sum(1 for r in sample if match(arm.get(r["id"], (None,))[0], r["expected_code"], 4) and not match(arm.get(r["id"], (None,))[0], r["expected_code"], 10))
        print("%s: right-heading-wrong-leaf (review for defensible): %d" % (lbl, rh))

    json.dump({"arm_a": sa, "arm_b": sb, "by_persona_a": pa, "by_persona_b": pb}, open(sys.argv[3].rstrip("/") + "/../score_summary.json", "w"), indent=1)


if __name__ == "__main__":
    main()
