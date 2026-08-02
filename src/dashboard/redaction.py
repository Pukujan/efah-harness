"""Protected-content boundary for read projections.

``plane.yaml -> protected_content_rule`` and contract Sections 11.2 and 17.2:

* holdout assertions -- never exposed;
* private fixtures -- never exposed;
* mutant implementations -- never exposed;
* real model identity -- never exposed, aliases only.

The projection layer is the last place these could leak, because it is the only
layer whose whole job is to publish. So the check runs *here*, on the rendered
view, not on the inputs -- a guard that trusts its caller is decoration.

Design note: this raises rather than silently scrubbing. A scrubbed projection
looks correct and hides that something upstream tried to publish a holdout,
which is precisely the signal GATE-D1-06 and GATE-D2-11 A3 exist to catch.
"""

from __future__ import annotations

import re
from typing import Any, Final

from observability.identity import ProtectedIdentityLeak, scan_for_leaks

#: Field names that carry sealed-side internals. Their *presence* is the
#: violation; their value is never inspected, printed, or logged.
PROTECTED_FIELD_NAMES: Final = frozenset(
    {
        "holdout_assertion",
        "holdout_assertions",
        "hidden_assertion",
        "hidden_assertions",
        "hidden_assertion_text",
        "private_fixture",
        "private_fixtures",
        "fixture_content",
        "mutant_source",
        "mutant_implementation",
        "mutant_diff",
        "mutant_patch",
        "oracle_internals",
        "holdout_dataset",
        "sealed_repo",
        "verifier_token",
    }
)

#: Sealed-side repositories. A projection that even names a route to them fails.
SEALED_REPOSITORIES: Final = ("efah-lab-verifier", "eval-lab-verifier")

_ASSERTION_BODY = re.compile(r"\bassert\s+|\bdef\s+test_|<<<<<<<|-----BEGIN [A-Z ]*PRIVATE KEY")


class ProtectedContentLeak(RuntimeError):
    """A read projection tried to publish sealed-side content."""

    def __init__(self, where: str, reason: str) -> None:
        self.where = where
        self.reason = reason
        super().__init__(f"PROTECTED_ASSET_ACCESS: projection {where} would expose {reason}")


def assert_no_protected_content(payload: Any, *, where: str) -> None:
    """Raise unless *payload* is safe to publish to Plane or the dashboard.

    Three families of finding:

    1. a field named after a sealed-side artifact (holdout, fixture, mutant);
    2. a value that reads like test-assertion or key material;
    3. a real vendor/model identity, or a prestige/cost ranking field.
    """
    for path, name in _walk_field_names(payload):
        if name.lower() in PROTECTED_FIELD_NAMES:
            raise ProtectedContentLeak(where, f"protected field {path}")

    for path, value in _walk_values(payload):
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        for repo in SEALED_REPOSITORIES:
            if repo in lowered:
                raise ProtectedContentLeak(where, f"a route to the sealed repository at {path}")
        if _ASSERTION_BODY.search(value):
            raise ProtectedContentLeak(where, f"assertion or key material at {path}")

    leaks = scan_for_leaks(payload, path=where)
    if leaks:
        path, matched = leaks[0]
        raise ProtectedIdentityLeak(path, matched)


def _walk_field_names(payload: Any, path: str = "$") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.append((f"{path}.{key}", str(key)))
            found.extend(_walk_field_names(value, f"{path}.{key}"))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found.extend(_walk_field_names(value, f"{path}[{index}]"))
    return found


def _walk_values(payload: Any, path: str = "$") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.extend(_walk_values(value, f"{path}.{key}"))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found.extend(_walk_values(value, f"{path}[{index}]"))
    else:
        found.append((path, payload))
    return found
