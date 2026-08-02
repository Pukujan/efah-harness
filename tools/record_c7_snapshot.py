#!/usr/bin/env python3
"""Record a Context7 retrieval as a hashed snapshot (§16.1).

Contract §16.1 requires dependency documentation to be snapshotted and hashed at
retrieval, and ``dependency-policy.yaml`` sets
``context7_snapshot_required: true``. A dependency decision argued from a
model's memory of a library is exactly the unsourced claim §7.3 forbids, so the
retrieved text is stored verbatim and hashed, and the decision cites the
snapshot rather than the library.

Two hashes, as the existing snapshots carry: the raw response, and a normalised
form (trailing whitespace stripped, blank lines dropped, joined with LF) so a
cosmetic reflow upstream does not read as a content change while a real edit
still does.

Usage: the retrieved text arrives on stdin as JSON:

    {"library_id": "...", "query": "...", "raw_response": "...", ...}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from integrations.context7 import normalised_hash, raw_hash  # noqa: E402

SNAPSHOT_DIR = REPO_ROOT / "project-pack" / "evidence" / "context7-snapshots"


def record(payload: dict) -> Path:
    raw = payload["raw_response"]
    rhash = raw_hash(raw)
    slug = payload["slug"]
    snapshot_id = f"C7-{slug}-{rhash.removeprefix('sha256:')[:8]}"

    snapshot = {
        "schema_id": "efah.context7_snapshot",
        "schema_version": "1.0",
        "contract_id": "EFAH-CONTRACT-001",
        "contract_version": "1.1",
        "snapshot_id": snapshot_id,
        "library_id": payload["library_id"],
        "library_version_or_branch": payload.get("library_version_or_branch", "unpinned_upstream_docs"),
        "query": payload["query"],
        "retrieved_at": payload["retrieved_at"],
        "credential_alias": payload.get("credential_alias", "primary"),
        "raw_response": raw,
        "raw_response_hash": rhash,
        "normalized_response_hash": normalised_hash(raw),
        "normalization_rule": "strip-trailing-whitespace; drop-blank-lines; join-with-lf",
        "source_locations": payload.get("source_locations", []),
        "affected_decisions": payload.get("affected_decisions", []),
        "affected_dependencies": payload.get("affected_dependencies", []),
        # §16 / environments.yaml: the two Context7 credentials are capacity and
        # failover, not independent sources. One retrieval is one source.
        "independent_corroboration": payload.get("independent_corroboration", "none"),
        "installed_versions_probed": payload.get("installed_versions_probed", {}),
    }

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SNAPSHOT_DIR / f"{snapshot_id}.json"
    out.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    return out


def main() -> int:
    payloads = json.load(sys.stdin)
    if isinstance(payloads, dict):
        payloads = [payloads]
    for payload in payloads:
        path = record(payload)
        print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
