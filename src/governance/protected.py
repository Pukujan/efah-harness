"""The canonical declaration of what is protected.

Contract §11.2 (protected model-identity store) and §17.2 (sealed verifier).

Several modules legitimately need to *name* a protected asset in order to
**refuse** it: the owner surface denies commands that reach for one, and the
failure classifier maps a 401 against the protected instance to
``PROTECTED_ACCESS``.

**Sealed-side names are never written here as literals.** GATE-D1-08 A2 requires
zero matches for the sealed repository names anywhere under ``src/``, ``tests/``,
``project-pack/``, ``.github/`` or ``docker/`` *outside the declared
``sealed_repos`` block*. A denylist that hardcoded them would violate the gate it
exists to serve — and the gate is right: a name in source is a name that gets
copied, logged, and eventually resolved.

So the sealed names are read at runtime from ``repositories.yaml →
sealed_repos``, which is the one place the contract permits them to appear. The
pack declares them once; this module reads that declaration; nothing duplicates
it.

The protected-*instance* markers below are a different matter: a loopback port
and an environment-variable name are not a route to anything an outsider can
reach, and they appear in ``environments.yaml`` and ``secrets.refs.yaml`` by
design.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

#: What actually constitutes a *route* to the protected identity instance: its
#: port and its credential. Holding either is holding reach.
#:
#: Deliberately narrower than the names below. ``terminusdb_protected`` is an
#: ``environments.yaml`` key and ``efah_protected`` a database name; the contract
#: compiler legitimately cites the key when mapping a plan item to the services
#: it covers, and a citation is not a connection.
PROTECTED_ROUTE_MARKERS: tuple[str, ...] = (
    "6364",
    "TERMINUSDB_PROTECTED_PASS",
)

#: Names of the protected instance. A command naming one is asking for it, so
#: the owner surface refuses it — but a module citing one is not thereby routing.
PROTECTED_INSTANCE_NAMES: tuple[str, ...] = (
    "efah_protected",
    "terminusdb_protected",
)

PROTECTED_INSTANCE_MARKERS: tuple[str, ...] = PROTECTED_ROUTE_MARKERS + PROTECTED_INSTANCE_NAMES

#: Sealed-side *asset kinds*. These are contract vocabulary from §17.1, not
#: repository names, so they carry no route.
SEALED_ASSET_TERMS: tuple[str, ...] = (
    "sealed holdout",
    "holdout content",
    "oracle internals",
    "private mutant",
    "hard gold case",
)

#: Modules permitted to hold a *route* to the protected identity instance.
#: Exactly one, by contract §11.2.
AUTHORISED_PROTECTED_ROUTE = "integrations/protected_identity.py"

#: Modules permitted to name protected assets because they exist to deny them.
#: Naming is not routing; these hold no credential and open no connection.
AUTHORISED_DENYLIST_MODULES = (
    "governance/protected.py",
    "owner_surface/policy.py",
    "workflows/failures.py",
)


def _pack_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "project-pack" / "repositories.yaml"
        if candidate.is_file():
            return candidate.parent
    raise FileNotFoundError("project-pack/repositories.yaml not found")


@lru_cache(maxsize=1)
def sealed_repository_names() -> tuple[str, ...]:
    """Sealed repository names, read from the pack's declared ``sealed_repos``.

    Read rather than hardcoded so GATE-D1-08 A2 holds: the names exist in the
    one block the contract declares them in, and nowhere else on the build side.
    """
    data: dict[str, Any] = yaml.safe_load((_pack_root() / "repositories.yaml").read_text())
    names: list[str] = []
    for entry in data.get("sealed_repos") or []:
        name = (entry or {}).get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def denied_terms() -> tuple[str, ...]:
    """Everything a task participant must never reach, lowercased for matching."""
    return tuple(
        term.lower()
        for term in (
            *sealed_repository_names(),
            *SEALED_ASSET_TERMS,
            *PROTECTED_INSTANCE_MARKERS,
        )
    )
