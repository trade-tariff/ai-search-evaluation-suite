import os, psycopg
FACTOR = 12  # est_cost_usd ~$0.02/classify-call (~12x under real gpt-5.5); scale to a real-$ proxy
with psycopg.connect(os.environ["TARIFF_DB_DSN"]) as c, c.cursor() as cur:
    cur.execute("SELECT coalesce(round((sum(est_cost_usd)*%s)::numeric, 2), 0) FROM kg.classify_runs WHERE run_label LIKE %s", (FACTOR, "exp_%"))
    print(cur.fetchone()[0])
