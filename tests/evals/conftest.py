"""Put the repo root on `sys.path` for the Eval Lab tests.

`pyproject.toml` sets `pythonpath = ["src"]`, which makes the harness packages importable
but not the top-level `evals/` package (which deliberately lives outside `src/` — it is a
lab, not shipped runtime). This adds the repo root for these tests only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
