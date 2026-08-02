# Aborted / superseded runs

Outputs kept for audit but **not** evidence of the current state. Nothing here should be
cited as a result.

- `bronze_load_run1.json` — first invocation of the Phase 2A loader. It reported
  `skipped` because the tables had already been populated by manual INSERTs, so it never
  exercised the insert path. Superseded by `bronze_load_runA.json` (insert) and
  `bronze_load_runB.json` (skip), which were run from a truncated state on purpose.
