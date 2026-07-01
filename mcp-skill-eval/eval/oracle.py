#!/usr/bin/env python3
"""Oracle = simulated importer. Answers a classifier's question using ONLY the true
facts about the product (the source ATAR text), never revealing the code.

Usage: oracle.py <sample.json> <row_id> "<question>"
Env: OPENAI_API_KEY (sourced by caller). Model: gpt-5-mini (cheap).
"""
import json, os, sys, urllib.request

MODEL = os.environ.get("ORACLE_MODEL", "gpt-5-mini")
SYS = (
    "You are simulating an importer answering a customs classifier's questions about YOUR product. "
    "The true facts about your product are below. Answer ONLY the question asked, in one or two short "
    "sentences, using ONLY these facts. If the facts do not cover it, reply exactly 'Not specified.' "
    "NEVER state, guess, or hint at a commodity code, HS code, tariff heading, or chapter number. "
    "Do not volunteer information beyond the question.\n\nTRUE PRODUCT FACTS:\n{facts}"
)


def main():
    sample_path, row_id, question = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    rows = {r["id"]: r for r in json.load(open(sample_path))}
    facts = rows[row_id]["oracle_text"]
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYS.format(facts=facts)},
            {"role": "user", "content": question},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    print(d["choices"][0]["message"]["content"].strip())


if __name__ == "__main__":
    main()
