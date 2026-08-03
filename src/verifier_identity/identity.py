"""Where the boundary is, and whether it actually holds.

DEC-006 option B: the protected verifier runs under a **separate service
identity on this host**, with its own credential and its own store the builder
process cannot read. Contract §17.2 permits this — "a separate repository
**and/or** service identity" — and the same pattern is already proven live for
the protected model-identity store, where the main admin credential receives a
measured HTTP 401 against ``terminusdb_protected``.

Every property below is **measured**, never asserted. GATE-D1-08's own preamble
is the reason: "This is the gate that must NOT be made to pass by granting
access. It passes by proving access is impossible." A module that declared the
store unreadable would be doing the opposite of what the gate asks.

The measurement that matters is :func:`builder_can_read_store`, and it is
performed the only way that means anything — by attempting the read as the
current process and recording what the kernel said. A ``PermissionError`` from
``os.listdir`` is evidence. A configuration file claiming mode ``0700`` is not.

Honest limit, stated here rather than in a footnote
---------------------------------------------------
The builder runs as a user holding **passwordless sudo** and membership of the
**docker** group. A separate OS user with a ``0700`` store therefore:

* prevents *accidental* exposure — no glob, no ``rglob``, no stray ``cat``
  reaches the content, and no library that walks the tree can stumble into it;
* makes any real access **auditable** — it must go through ``sudo``, which logs,
  or through the docker socket, which logs;
* and is **not sound against a deliberate builder**, who can simply escalate.

Option A — the sealed side on a separate host under an owner-held identity —
remains the durable path. DEC-006 records this and so does
:data:`BUILDER_CANNOT_ESCALATE`, which is ``False`` and is *reported in the
evidence*, not hidden. An isolation claim that omits the escalation path would
be the "recorded run is not the run that happened" failure one layer up.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The service account the sealed side runs as. Not a sealed *repository* name —
#: GATE-D1-08 A2 forbids those on the build side, and this is a local OS user,
#: which is a boundary the builder must be able to name in order to respect it.
VERIFIER_USER = "efah-verifier"

#: Everything the verifier identity owns lives under here, mode 0700.
VERIFIER_HOME = Path("/var/lib/efah-verifier")

#: Generated holdouts, private mutants and oracle internals. The builder must
#: never read this, and :func:`builder_can_read_store` proves it cannot.
SEALED_STORE = VERIFIER_HOME / "store"

#: The verifier identity's own eval-gateway credential. Separate from the
#: builder's copy so the two can diverge later without touching this code.
VERIFIER_CREDENTIAL = VERIFIER_HOME / "etc" / "eval.env"

#: Root-owned, mode 0755. The verifier identity cannot rewrite its own
#: generator, so a compromise of that account cannot quietly change what gets
#: generated or where it goes.
GENERATOR_ROOT = Path("/opt/efah-verifier/bin")
GENERATOR = GENERATOR_ROOT / "generate-holdouts"

#: The sudoers drop-in that lets the builder invoke exactly the entrypoints
#: below as the verifier identity, and nothing else.
SUDOERS_DROPIN = Path("/etc/sudoers.d/efah-verifier")

#: DEC-006, recorded as data so the evidence package cannot omit it. The builder
#: holds passwordless sudo; OS separation stops accidents and creates an audit
#: trail, and stops nothing else.
BUILDER_CANNOT_ESCALATE = False


@dataclass(frozen=True)
class VerifierIdentity:
    """The declared boundary. Paths and a user name — no credential, no content."""

    user: str = VERIFIER_USER
    home: Path = VERIFIER_HOME
    store: Path = SEALED_STORE
    credential: Path = VERIFIER_CREDENTIAL
    generator: Path = GENERATOR
    sudoers: Path = SUDOERS_DROPIN

    def as_body(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "home": str(self.home),
            "store": str(self.store),
            "generator": str(self.generator),
            # The credential *path* is not the credential, and naming it is how
            # the provisioning check verifies its mode. The value never appears.
            "credential_path": str(self.credential),
        }


def default_identity() -> VerifierIdentity:
    return VerifierIdentity()


# -- primitive measurements -------------------------------------------------


def _passwd(user: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(user)
    except KeyError:
        return None


@dataclass
class PathFacts:
    """What the kernel says about a path — including that it refused to say.

    ``stat_denied`` is not an error condition to be smoothed over. For the sealed
    store it is the *strongest* possible result: the builder cannot even learn
    whether the path exists, because the parent directory denies traversal. A
    measurement that swallowed the ``PermissionError`` and reported
    ``exists: false`` would be recording a denial as an absence — the FINDING-004
    mistake of counting a missing signal as success.
    """

    exists: bool | None = False
    mode: str | None = None
    owner_uid: int | None = None
    owner_name: str | None = None
    stat_denied: bool = False
    denial: str | None = None

    def as_body(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "mode": self.mode,
            "owner_uid": self.owner_uid,
            "owner_name": self.owner_name,
            "stat_denied": self.stat_denied,
            "denial": self.denial,
        }


def path_facts(path: Path) -> PathFacts:
    try:
        st = path.stat()
    except PermissionError as exc:
        return PathFacts(
            exists=None,
            stat_denied=True,
            denial=f"PermissionError: {exc.strerror}",
        )
    except OSError as exc:
        if getattr(exc, "errno", None) == 2:  # ENOENT — genuinely absent
            return PathFacts(exists=False)
        return PathFacts(exists=None, stat_denied=True, denial=f"{type(exc).__name__}: {exc}")
    owner = _passwd_by_uid(st.st_uid)
    return PathFacts(
        exists=True,
        mode=oct(st.st_mode & 0o7777),
        owner_uid=st.st_uid,
        owner_name=owner.pw_name if owner else None,
    )


def _passwd_by_uid(uid: int) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwuid(uid)
    except KeyError:
        return None


def builder_can_read_store(store: Path = SEALED_STORE) -> tuple[bool, str]:
    """Attempt the read. Record what the kernel said.

    This is the whole gate in one function. It returns ``(can_read, detail)``
    where ``can_read`` is the *measured* outcome of a real ``os.listdir`` under
    the current effective identity, not a deduction from the mode bits.

    Running as root would succeed and the result would be meaningless, so the
    caller's euid is reported alongside and
    :func:`measure` refuses to treat a root-run measurement as evidence.
    """
    try:
        entries = os.listdir(store)
    except PermissionError as exc:
        return False, f"PermissionError: {exc.strerror}"
    except FileNotFoundError:
        # Genuinely absent is *not* isolation. An unprovisioned host must not
        # read as a proven boundary, so this is reported as its own case and
        # :meth:`IdentityMeasurement.provisioned` refuses it.
        return False, "store does not exist; nothing has been provisioned"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"listdir succeeded and returned {len(entries)} entries"


def sudoers_scope(dropin: Path = SUDOERS_DROPIN) -> dict[str, Any]:
    """What the builder is permitted to run as the verifier identity.

    Read through ``sudo`` because the drop-in is root-only by design; if it
    cannot be read, that is reported rather than guessed at.
    """
    if shutil.which("sudo") is None:
        return {"readable": False, "reason": "sudo is not installed"}
    try:
        proc = subprocess.run(
            ["sudo", "-n", "cat", str(dropin)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"readable": False, "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"readable": False, "reason": f"sudo exited {proc.returncode}"}

    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    return {
        "readable": True,
        "rules": lines,
        "grants_unrestricted_command": any(line.rstrip().endswith("ALL") for line in lines),
    }


# -- the composed measurement ----------------------------------------------


@dataclass
class IdentityMeasurement:
    """Everything measured about the boundary, in one evidence-shaped record."""

    identity: VerifierIdentity
    builder_user: str
    builder_uid: int
    verifier_uid: int | None
    identities_distinct: bool
    home: PathFacts
    store: PathFacts
    generator: PathFacts
    credential: PathFacts
    builder_read_attempt: tuple[bool, str]
    sudoers: dict[str, Any] = field(default_factory=dict)
    measured_as_root: bool = False

    @property
    def provisioned(self) -> bool:
        """Provisioned means the account and the store exist and are separated.

        The store's ``exists`` is ``None`` here, not ``True``: from the builder
        identity it is *unknowable*, because the parent denies traversal. That is
        the intended state, so ``stat_denied`` counts as provisioned while a
        clean ``exists is False`` does not — an absent store is an unprovisioned
        host, not a proven boundary.
        """
        store_present = self.store.exists is True or self.store.stat_denied
        return (
            self.verifier_uid is not None
            and self.identities_distinct
            and store_present
            and self.generator.exists is True
        )

    @property
    def isolation_holds(self) -> bool:
        """Measured isolation, with the root caveat applied honestly.

        A measurement taken as root proves nothing about the builder identity,
        so it is not allowed to report success. This is the FINDING-004 lesson:
        a check whose remedy contained the same error it was fixing — absence of
        a signal counted as success — must not be repeated here.
        """
        if self.measured_as_root:
            return False
        can_read, _ = self.builder_read_attempt
        return self.provisioned and not can_read

    def as_body(self) -> dict[str, Any]:
        can_read, detail = self.builder_read_attempt
        return {
            "check": "verifier_service_identity_isolation",
            "decision_ref": "DEC-006",
            "contract_ref": "contract_17.2_protected_verifier_architecture",
            "gate_ref": "GATE-D1-08 A4",
            "oracle_type": "deterministic_execution_or_state",
            "identity": self.identity.as_body(),
            "builder_identity": {"user": self.builder_user, "uid": self.builder_uid},
            "verifier_identity": {"user": self.identity.user, "uid": self.verifier_uid},
            "identities_distinct": self.identities_distinct,
            "paths": {
                "home": self.home.as_body(),
                "store": self.store.as_body(),
                "generator": self.generator.as_body(),
                "credential": self.credential.as_body(),
            },
            "builder_read_attempt": {
                "method": "os.listdir under the current effective identity",
                "succeeded": can_read,
                "detail": detail,
            },
            "sudoers": self.sudoers,
            "provisioned": self.provisioned,
            "isolation_holds": self.isolation_holds,
            "measured_as_root": self.measured_as_root,
            # DEC-006 accepted consequences, carried in the evidence rather than
            # left to the prose. An isolation claim that omits its own defeat
            # condition is not an honest one.
            "honest_debt": {
                "builder_cannot_escalate": BUILDER_CANNOT_ESCALATE,
                "why": (
                    "the builder identity holds passwordless sudo and docker group "
                    "membership; OS separation prevents accidental exposure and makes "
                    "deliberate access auditable, and does not prevent it"
                ),
                "durable_path": "DEC-006 option A - sealed side on a separate host under an owner-held identity",
                "reversible": (
                    "the submission interface does not change, so regenerating under an "
                    "owner-held identity later costs nothing already built"
                ),
            },
        }


def measure(identity: VerifierIdentity | None = None) -> IdentityMeasurement:
    """Take every measurement. Never raises: an unprovisioned host is a result."""
    identity = identity or default_identity()
    builder_uid = os.geteuid()
    builder = _passwd_by_uid(builder_uid)
    verifier = _passwd(identity.user)

    return IdentityMeasurement(
        identity=identity,
        builder_user=builder.pw_name if builder else str(builder_uid),
        builder_uid=builder_uid,
        verifier_uid=verifier.pw_uid if verifier else None,
        identities_distinct=verifier is not None and verifier.pw_uid != builder_uid,
        home=path_facts(identity.home),
        store=path_facts(identity.store),
        generator=path_facts(identity.generator),
        credential=path_facts(identity.credential),
        builder_read_attempt=builder_can_read_store(identity.store),
        sudoers=sudoers_scope(identity.sudoers),
        measured_as_root=builder_uid == 0,
    )
