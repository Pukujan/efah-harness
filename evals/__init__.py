"""EFAH Eval Lab — Inspect AI tasks over deterministic gold lanes.

Contract EFAH-CONTRACT-001 Section 14.5 requires the Eval Lab to exist BEFORE broad
feature expansion; it may not be deferred. `project-pack/dependency-policy.yaml` selects
`inspect_ai` as `evaluation_runtime` and prohibits a `custom_eval_runner`
("duplicates_inspect_ai"), so everything in this package is an Inspect AI `Task` /
`Solver` / `Scorer` — there is no hand-rolled run loop anywhere under `evals/`.

THE ONE RULE THIS PACKAGE EXISTS TO ENFORCE
-------------------------------------------
Inspect runs and logs; it does NOT judge. The verdict of record for every sample comes
from a deterministic lane checker that executes code. Any framework-native score
(inspect's own `includes()`, `match()`, a future rubric metric) is ADVISORY and lands in
`Score.metadata["diagnostics"]`, never in `passed`. This mirrors the rule already written
at `/home/yoav/ssc-github/evals/oracle_adapter.py` (TRUST RULES 2 and 3).

Modules:
  * `gold_lanes.py`          — framework-free lane/checker/record loading + integrity scans.
                               Imports NOTHING from inspect_ai, so the verdict path is
                               provably independent of the runner.
  * `mbpp_execution_lane.py` — the Inspect `@task` wiring for the MBPP execution-oracle lane.
"""
