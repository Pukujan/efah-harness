"""The canonical declaration of what is protected.

Contract §11.2 (protected model-identity store) and §17.2 (sealed verifier).

Several modules legitimately need to *name* a protected asset in order to
**refuse** it: the owner surface denies commands that reach for one, the failure
classifier maps a 401 against the protected instance to ``PROTECTED_ACCESS``,
and the isolation tests assert the boundary holds.

Declaring the markers in one place separates the two things that look identical
in a text search but are opposites:

* a module that **routes** to a protected asset — forbidden everywhere except
  :mod:`integrations.protected_identity`;
* a module that **denies** one — which must name it, and whose naming is the
  boundary working rather than leaking.

Anything that needs a denylist imports it from here rather than writing the
literal, so a text scan finds one authorised definition instead of a scatter of
indistinguishable string constants.
"""

from __future__ import annotations

#: What actually constitutes a *route* to the protected instance: its port and
#: its credential. Holding either is holding reach.
#:
#: Deliberately narrower than the names below. ``terminusdb_protected`` is an
#: ``environments.yaml`` key and ``efah_protected`` a database name; the
#: contract compiler legitimately references the key when mapping a plan item to
#: the services it covers, and that is a citation, not a connection. Conflating
#: the two made the isolation test flag the compiler for reading configuration.
PROTECTED_ROUTE_MARKERS: tuple[str, ...] = (
    "6364",
    "TERMINUSDB_PROTECTED_PASS",
)

#: Names of the protected instance. A command naming any of these is asking for
#: it, so the owner surface refuses them -- but a module merely citing one is
#: not thereby routing to it.
PROTECTED_INSTANCE_NAMES: tuple[str, ...] = (
    "efah_protected",
    "terminusdb_protected",
)

#: Backwards-compatible alias for the full set.
PROTECTED_INSTANCE_MARKERS: tuple[str, ...] = PROTECTED_ROUTE_MARKERS + PROTECTED_INSTANCE_NAMES

#: Sealed-side repositories and assets (``repositories.yaml → sealed_repos``).
#: The builder's correct result for every one of these is 401, 403, or 404.
SEALED_SIDE_MARKERS: tuple[str, ...] = (
    "efah-lab-verifier",
    "eval-lab-verifier",
    "sealed_holdout",
    "sealed holdout",
    "holdout content",
    "holdout_db",
    "verifier_token",
    "oracle internals",
    "private mutant",
    "hard gold case",
)

#: Everything a task participant must never reach.
ALL_PROTECTED_MARKERS: tuple[str, ...] = SEALED_SIDE_MARKERS + PROTECTED_INSTANCE_MARKERS

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
