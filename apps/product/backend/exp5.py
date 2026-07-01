"""Experiment 5: DEFINITIVE rounds-to-commit at the real config.
gpt-5.5, N=100, eliminate, oracle trader simulator (gpt-5-mini), via the actual
_run_eliminate_session loop. Measures real rounds-per-classification across 19 chapters.
"""
import os, asyncio, statistics as st
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
os.environ["QA_SIMULATOR_MODEL"] = "gpt-5-mini"
import journey.classification as C
import journey.qa_loop as Q

QUERIES = [
    ("freeze dried fig slices in resealable snack bags", "0804209000"),
    ("raw frozen marinated chicken fillets in vacuum bag", "1602321190"),
    ("chocolate soy and whey protein powder 1kg tub", "1806907090"),
    ("frozen gluten free cheddar dough balls for baking", "1901200000"),
    ("fried sliced tomato chips in foil bags", "2002109000"),
    ("bell pepper and chilli table sauce bottle", "2103909089"),
    ("mineral solution for purified drinking water system", "2201900000"),
    ("turmeric and black pepper powder for horses", "2309909696"),
    ("50ml peptide moisturiser in glass bottle", "3304990000"),
    ("150g white sticky putty for hanging pictures on walls", "3506100000"),
    ("polyethylene strip for connecting geogrid sheets in road works", "3916100090"),
    ("epdm rubber bellows for water pump seal", "4016999790"),
    ("20 litre waterproof polyester backpack with bottle pockets", "4202921900"),
    ("10cm wooden skewers for canapes and bbq", "4421910000"),
    ("14 inch tall cake box with silver round board", "4819200000"),
    ("small polyester felt chicks for childrens craft projects", "5602101900"),
    ("spicy ramen set with ceramic bowl and chopsticks", "6912002310"),
    ("one kilo raw gold bullion bars from tanzania", "7108120000"),
    ("2mm zinc coated embossed steel sheet panels", "7210300090"),
]

cfg = dict(C.DEFAULT_CLASSIFY_CONFIG)
cfg.update({"use_llm_candidate_selection": True, "candidate_selection_model": "gpt-5.5",
            "qa_mode": "ask_first", "strategy": "eliminate"})
cfg["retrieval"] = {**(cfg.get("retrieval") or {}), "limit": 100}

async def one(query, code):
    oracle = f"Product: {query}. The correct UK commodity code is {code}."
    res = await Q._run_eliminate_session(query, max_rounds=7, oracle_text=oracle, config=cfg, human_answers=None)
    rounds = res.get("rounds") or []
    q_rounds = sum(1 for r in rounds if r.get("mode") == "questions")
    surv = res.get("survivors_final") or res.get("final_answers") or []
    surv_codes = [s.get("commodity_code") for s in surv] if (surv and isinstance(surv[0], dict)) else (surv or [])
    return {"query": query, "code": code,
            "classify_rounds": res.get("total_classify_calls"),  # gpt-5.5 calls = cost rounds
            "q_rounds": q_rounds, "final": res.get("final_mode"),
            "gold_in": code in surv_codes, "top1_ok": (surv_codes[0] == code) if surv_codes else False,
            "surv_n": len(surv_codes), "sim_calls": res.get("total_simulator_calls")}

async def main():
    print("provider_calls_allowed=", C.provider_calls_allowed(), "N=100 model=gpt-5.5 sim=gpt-5-mini")
    print(f"queries={len(QUERIES)}")
    sem = asyncio.Semaphore(4)
    async def guarded(q, c):
        async with sem:
            try:
                r = await one(q, c); print(".", end="", flush=True); return r
            except Exception as e:
                print("x", end="", flush=True); return {"query": q, "code": c, "err": repr(e)}
    results = await asyncio.gather(*[guarded(q, c) for q, c in QUERIES])
    print("\n")
    ok = [r for r in results if r.get("classify_rounds") is not None]
    print("="*100)
    print(f"{'chap':4} {'query':44} {'gpt5.5_rounds':>13} {'q_asked':>8} {'final':>9} {'gold_in':>8} {'top1':>5}")
    print("-"*100)
    for r in sorted(ok, key=lambda x: x["code"]):
        print(f"{r['code'][:2]:4} {r['query'][:44]:44} {r['classify_rounds']:>13} {r['q_rounds']:>8} "
              f"{str(r['final'])[:9]:>9} {str(r['gold_in']):>8} {str(r['top1_ok']):>5}")
    print("-"*100)
    rd = [r["classify_rounds"] for r in ok]
    qd = [r["q_rounds"] for r in ok]
    if rd:
        print(f"n={len(rd)}  MEAN gpt-5.5 rounds/classification = {st.mean(rd):.2f}  (median {st.median(rd)}, min {min(rd)}, max {max(rd)})")
        print(f"           mean questions actually asked        = {st.mean(qd):.2f}")
        from collections import Counter
        print("  rounds distribution:", dict(sorted(Counter(rd).items())))
        print(f"  gold retained in survivors: {sum(1 for r in ok if r['gold_in'])}/{len(ok)}   top-1 correct: {sum(1 for r in ok if r['top1_ok'])}/{len(ok)}")
    errs = [r for r in results if r.get("err")]
    if errs:
        print(f"  errors: {len(errs)} e.g. {errs[0]['err'][:120]}")
    print("="*100)

asyncio.run(main())
