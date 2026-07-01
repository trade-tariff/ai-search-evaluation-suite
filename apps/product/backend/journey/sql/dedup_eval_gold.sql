-- Soft-dedup of kg.eval_gold (idempotent). Adds an `active` flag and marks
-- duplicate gold rows inactive WITHOUT deleting them (eval_run_results has an FK
-- to eval_gold.id, so physical deletes would orphan historical runs).
--
-- Duplicate classes:
--   (a) exact (source_type, source_id, persona, expected_code)  -> keep lowest id
--   (b) (persona, query, expected_code)                         -> keep lowest id
--
-- run_eval loads `WHERE active`. lock_timeout keeps this from wedging behind the
-- live eval runs' open transactions - just retry until it catches a commit gap.
SET lock_timeout = '3s';

BEGIN;

ALTER TABLE kg.eval_gold ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;

-- Reset so re-running is idempotent.
UPDATE kg.eval_gold SET active = true WHERE active = false;

-- Class (a): exact quad dup -> keep lowest id.
WITH ranked AS (
  SELECT id,
         row_number() OVER (
           PARTITION BY source_type, source_id, persona, expected_code
           ORDER BY id
         ) AS rn
  FROM kg.eval_gold
)
UPDATE kg.eval_gold g
SET active = false
FROM ranked r
WHERE g.id = r.id AND r.rn > 1;

-- Class (b): (persona, query, expected_code) dup -> keep lowest id among still-active.
WITH ranked AS (
  SELECT id,
         row_number() OVER (
           PARTITION BY persona, query, expected_code
           ORDER BY id
         ) AS rn
  FROM kg.eval_gold
  WHERE active
)
UPDATE kg.eval_gold g
SET active = false
FROM ranked r
WHERE g.id = r.id AND r.rn > 1;

COMMIT;

SELECT active, count(*) FROM kg.eval_gold GROUP BY active ORDER BY active;
