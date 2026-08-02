"""Context7 snapshot normalisation and hashing.

Contract §16.1 requires every retrieval to record both a ``raw_response_hash``
and a ``normalized_response_hash``. It does not say what "normalized" means —
and two lanes independently chose two different rules, which is worse than
either rule, because §16.2's version-diff loop compares normalised documents
across dependency versions. Two conventions make those diffs incomparable and
the whole loop silently useless.

So the rule is declared once, here.

**The raw hash is the evidence.** It is the hash of exactly the bytes the
credential returned, and it is never recomputed — doing so would destroy the
only tamper-evident record of what was actually retrieved. The normalised hash
is a *derived* convenience for diffing, so it may be recomputed under a stated
rule, and this module is that rule.
"""

from __future__ import annotations

from typing import Any

from governance.envelope import content_hash

#: Fields contract §16.1 and ``dependency-policy.yaml`` require on every snapshot.
REQUIRED_SNAPSHOT_FIELDS = (
    "snapshot_id",
    "credential_alias",
    "library_id",
    "library_version_or_branch",
    "query",
    "retrieved_at",
    "raw_response_hash",
    "normalized_response_hash",
    "source_locations",
    "affected_dependencies",
    "affected_decisions",
)

#: The canonical key holding the retrieved body. Not named by the contract, so
#: it is fixed here rather than left to each lane.
BODY_FIELD = "raw_response"

#: The normalisation rule, in one place.
NORMALISATION_RULE = "strip-trailing-whitespace; drop-blank-lines; join-with-lf"


def normalise(body: str) -> str:
    """Apply the canonical normalisation.

    Trailing whitespace and blank lines are formatting noise: a documentation
    page that gains a blank line has not changed its API, and a diff loop that
    reports it as a change trains everyone to ignore the diff loop.
    """
    return "\n".join(line for line in (raw.rstrip() for raw in body.splitlines()) if line)


def raw_hash(body: str) -> str:
    """Hash of exactly the bytes retrieved. Evidence — never recomputed."""
    return content_hash(body.encode("utf-8"))


def normalised_hash(body: str) -> str:
    return content_hash(normalise(body))


def verify_snapshot(snapshot: dict[str, Any]) -> list[str]:
    """Return the problems with a snapshot. Empty means it verifies."""
    problems = [f"missing field {f!r}" for f in REQUIRED_SNAPSHOT_FIELDS if f not in snapshot]
    if snapshot.get("credential_alias") not in {"primary", "secondary"}:
        problems.append(f"credential_alias {snapshot.get('credential_alias')!r} is not primary or secondary")

    body = snapshot.get(BODY_FIELD)
    if not isinstance(body, str) or not body:
        problems.append(f"no retrieved body under {BODY_FIELD!r}")
        return problems

    if snapshot["raw_response_hash"] != raw_hash(body):
        problems.append("raw_response_hash does not recompute from the stored body")
    if snapshot["normalized_response_hash"] != normalised_hash(body):
        problems.append("normalized_response_hash does not match the canonical rule")
    if snapshot["raw_response_hash"] == snapshot["normalized_response_hash"]:
        problems.append("raw and normalized hashes are equal, so normalisation did nothing")
    return problems
