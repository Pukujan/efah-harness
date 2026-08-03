"""The opaque subprocess seam. Exit status and a count — never content.

DEC-006 states the constraint this module exists to satisfy:

    Generation runs inside the verifier identity's process, never the builder's.
    If holdout text is returned into the builder process — even transiently,
    even only to be written out again — the builder has read it, and role
    separation is broken regardless of which vendor authored the text. A
    different model writing into the implementer's memory is theatre, not
    separation.

"Even transiently" is the demanding part, and it rules out the obvious designs.
The builder cannot receive the holdouts and write them to a protected path; it
cannot receive them to count them; it cannot receive them and hash them. So the
generator runs under the verifier identity, writes into the sealed store itself,
and hands back a **receipt**.

How content is prevented from riding back
------------------------------------------
The receipt is treated with the same suspicion as the §17.2 verdict shape, for
the same reason: it is the only channel, so it is the only place a leak could
ride. :func:`validate_receipt` enforces, in order:

1. **A closed key set.** :data:`PERMITTED_RECEIPT_FIELDS` is an allowlist. An
   extra key is a rejection, not an ignored field — a seam that tolerates extra
   fields is one the other side can start sending hints through.
2. **Per-field type and shape.** Every field is a scalar with a validator: counts
   are bounded integers, the hash must match ``sha256:`` plus exactly 64 hex
   digits, versions match a short identifier pattern, and the failure class must
   be one of the typed :class:`~governance.states.FailureClass` values. A field
   that is merely "a string" is a free channel; none of these are.
3. **Cross-field consistency.** ``killed_count`` may not exceed ``mutant_count``,
   and a claimed kill rate must equal the counts it is derived from. An
   inconsistent receipt is rejected rather than repaired.

And the channel that is easiest to forget: **stderr is never read.** A traceback
from the generator can contain holdout text, so the subprocess's stderr goes to
the generator's own log inside the sealed store and the builder's ``Popen`` sends
it to ``DEVNULL``. Stdout is read with a hard byte cap and only the last
non-empty line is parsed; the rest is discarded inside this function and never
reaches a returned value.

The cost of that, and what pays it
-----------------------------------
Discarding stderr is correct and stays. Its consequence is that a generator
failure is otherwise undiagnosable from this side: exit 1 and
``ORACLE_INVALID`` cover an unanswered owner decision, a truncated mutant
author and an absent pytest alike, and on 2026-08-03 they did. So the generator
reports its own reason **inside the receipt**, as
:class:`GeneratorFailureReason` — a closed vocabulary validated by membership,
not a message validated by pattern. The distinction is the whole point: a
pattern admits every string that matches it, an enumeration admits twenty
strings that are written down in this file. Holdout text cannot be one of them.

Rejections are a channel too
-----------------------------
A finding that quotes the value it rejected hands the rejected value to the
builder — ``rejected_because`` is carried in :meth:`SeamOutcome.as_evidence`.
So findings name the field and its type and never interpolate a value that has
not already passed a bound. That is why the messages below read as they do; it
is not squeamishness about detail.

What this does not defend against
----------------------------------
The builder authored the generator. It cannot read the output, but it wrote the
code that produces it, so a deliberately malicious generator could encode a few
bits per run into the permitted scalars. DEC-006 records this as accepted debt
and mitigates it with the mutation gate rather than pretending it away. The
defence here is against accident, against a buggy generator, and against the
gradual widening of a seam — which is how these boundaries actually fail.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from governance.envelope import CompiledObject, utc_now
from governance.states import FailureClass, ProjectState, TaskState
from verifier_identity.identity import VerifierIdentity, default_identity

#: The interface version of the seam itself. Semver over the receipt shape; a
#: change to the permitted field set is a change to the isolation property.
#:
#: 1.1.0 adds ``failure_reason`` and requires it whenever ``failure_class`` is
#: set. **A generator older than this cannot satisfy it**: its failure receipts
#: omit the field and are rejected here. That is deliberate — the alternative is
#: an optional diagnosis, which is the same as no diagnosis once anything stops
#: sending it — but it means the sealed side must be re-provisioned in step, so
#: ``tools/gate_dec_006.py`` compares the installed copy against the source and
#: fails loudly rather than leaving a stale generator to produce receipts this
#: side refuses for reasons the receipt cannot explain.
SEAM_VERSION = "1.1.0"

#: The only keys a receipt may carry. Compare with
#: ``evaluation.verifier_client.PERMITTED_RESPONSE_FIELDS`` — same discipline,
#: different direction of travel.
PERMITTED_RECEIPT_FIELDS: tuple[str, ...] = (
    "generation_request_id",
    "exit_status",
    "holdout_count",
    "mutant_count",
    "killed_count",
    "kill_rate",
    "store_content_hash",
    "generator_version",
    "oracle_version",
    "generated_at",
    "failure_class",
    "failure_reason",
)


class GeneratorFailureReason(StrEnum):
    """Why the generator failed, at a granularity the §10.6 classes do not have.

    ``FailureClass`` answers "is this worth retrying"; it is coarse on purpose
    and four unrelated conditions share ``ORACLE_INVALID``. This answers "what
    broke", and it is the only diagnosis available on this side of the seam
    because stderr is discarded.

    **Closed by construction.** Validation is set membership against these
    members — not a regex, not a length bound. A regex over a "short
    identifier" would still admit an unbounded number of distinct strings and
    therefore an unbounded number of bits; twenty names admit fewer than five.
    That is what makes this a diagnosis and not a channel.

    The generator holds the same list as ``FAILURE_REASONS`` because it cannot
    import this module (DEC-006: a generator that imported the builder's tree
    would be a generator the builder controls). ``tools/gate_dec_006.py`` and
    ``tests/unit/test_verifier_identity_seam.py`` compare the two copies, so
    drift fails a gate instead of passing quietly.
    """

    # refusals — the generator declined before spending anything
    NOT_VERIFIER_IDENTITY = "NOT_VERIFIER_IDENTITY"
    TRANSPORT_DECISION_UNRECORDED = "TRANSPORT_DECISION_UNRECORDED"
    CREDENTIAL_ABSENT = "CREDENTIAL_ABSENT"
    TARGET_COUNT_NOT_POSITIVE = "TARGET_COUNT_NOT_POSITIVE"
    THROTTLE_STATE_ABSENT = "THROTTLE_STATE_ABSENT"
    # the holdout author
    HOLDOUT_AUTHOR_EMPTY_GENERATION = "HOLDOUT_AUTHOR_EMPTY_GENERATION"
    HOLDOUT_AUTHOR_TRUNCATED = "HOLDOUT_AUTHOR_TRUNCATED"
    HOLDOUT_AUTHOR_UNPARSEABLE = "HOLDOUT_AUTHOR_UNPARSEABLE"
    # the mutant author
    MUTANT_AUTHOR_EMPTY_GENERATION = "MUTANT_AUTHOR_EMPTY_GENERATION"
    MUTANT_AUTHOR_TRUNCATED = "MUTANT_AUTHOR_TRUNCATED"
    MUTANT_AUTHOR_UNPARSEABLE = "MUTANT_AUTHOR_UNPARSEABLE"
    # the transport to either author
    MODEL_ECHO_MISMATCH = "MODEL_ECHO_MISMATCH"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_HTTP_ERROR = "MODEL_HTTP_ERROR"
    MODEL_TRANSPORT_FAILURE = "MODEL_TRANSPORT_FAILURE"
    # the deterministic gate
    TEST_RUNNER_UNAVAILABLE = "TEST_RUNNER_UNAVAILABLE"
    BASELINE_HOLDOUTS_FAILED = "BASELINE_HOLDOUTS_FAILED"
    MUTANT_RUN_NOT_A_VERDICT = "MUTANT_RUN_NOT_A_VERDICT"
    KILL_RATE_BELOW_THRESHOLD = "KILL_RATE_BELOW_THRESHOLD"
    # the honest bucket, never a free string
    UNCLASSIFIED_EXCEPTION = "UNCLASSIFIED_EXCEPTION"


#: A content hash identifies a set without revealing it, and §18 requires one on
#: every artifact. Pinned to exactly 64 hex digits so the field cannot be widened
#: into a text channel: 256 bits of preimage are useless to a reader who would
#: have to already hold the plaintext to interpret them.
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

#: Bounded so a generator cannot claim an implausible set, and so the field
#: cannot carry a large number as a payload.
_MAX_COUNT = 100_000

#: Stdout is a receipt line. Anything larger is carrying something else.
MAX_STDOUT_BYTES = 8192


class SeamViolation(RuntimeError):
    """The generator returned something the seam does not permit."""


@dataclass(frozen=True)
class GenerationRequest:
    """What the builder is permitted to *send* inward.

    Deliberately thin. The builder says which candidate commit and which contract
    version the set is for, and nothing about what the holdouts should contain —
    a builder that could shape the holdouts would be writing its own exam.
    """

    generation_request_id: str
    candidate_commit: str
    contract_version: str
    #: How many cases to mint. A number, not a topic list.
    target_count: int = 0

    def as_argv(self) -> list[str]:
        return [
            "--request-id", self.generation_request_id,
            "--candidate-commit", self.candidate_commit,
            "--contract-version", self.contract_version,
            "--target-count", str(self.target_count),
        ]

    def as_body(self) -> dict[str, Any]:
        return {
            "generation_request_id": self.generation_request_id,
            "candidate_commit": self.candidate_commit,
            "contract_version": self.contract_version,
            "target_count": self.target_count,
        }


@dataclass(frozen=True)
class GenerationReceipt:
    """What the builder is permitted to *receive*. Scalars, all validated."""

    generation_request_id: str
    exit_status: int
    holdout_count: int
    mutant_count: int
    killed_count: int
    kill_rate: float
    store_content_hash: str
    generator_version: str
    oracle_version: str
    generated_at: str
    failure_class: FailureClass | None = None
    #: Present exactly when ``failure_class`` is. Never on a success — a run
    #: that minted a set has nothing to explain.
    failure_reason: GeneratorFailureReason | None = None

    @property
    def mint_accepted(self) -> bool:
        """DEC-006: the mint refuses a holdout set with a kill rate below 1.0.

        "A holdout that fails to kill any known-bad mutant tests nothing, and one
        that 'passes' a mutant is worse than absent because it manufactures
        confidence."
        """
        return (
            self.exit_status == 0
            and self.holdout_count > 0
            and self.mutant_count > 0
            and self.kill_rate >= 1.0
        )

    def as_body(self) -> dict[str, Any]:
        return {
            "generation_request_id": self.generation_request_id,
            "exit_status": self.exit_status,
            "holdout_count": self.holdout_count,
            "mutant_count": self.mutant_count,
            "killed_count": self.killed_count,
            "kill_rate": self.kill_rate,
            "store_content_hash": self.store_content_hash,
            "generator_version": self.generator_version,
            "oracle_version": self.oracle_version,
            "generated_at": self.generated_at,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "failure_reason": self.failure_reason.value if self.failure_reason else None,
            "mint_accepted": self.mint_accepted,
        }


def validate_receipt(payload: Any) -> tuple[GenerationReceipt | None, list[str]]:
    """Enforce the closed shape, then the field shapes, then consistency."""
    findings: list[str] = []
    if not isinstance(payload, dict):
        return None, [f"receipt is {type(payload).__name__}, not an object"]

    extra = sorted(set(payload) - set(PERMITTED_RECEIPT_FIELDS))
    if extra:
        findings.append(
            f"receipt carries fields outside the permitted set: {extra}; the seam "
            "carries a status and counts, not generator output"
        )
    missing = sorted(
        set(PERMITTED_RECEIPT_FIELDS)
        - set(payload)
        - {"failure_class", "failure_reason", "kill_rate"}
    )
    if missing:
        findings.append(f"receipt omits required fields: {missing}")
    if findings:
        return None, findings

    def _int(name: str) -> int | None:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            findings.append(f"{name}: expected an integer, got {type(value).__name__}")
            return None
        if not 0 <= value <= _MAX_COUNT:
            # The value is not quoted back. An out-of-range integer is
            # unbounded in width, and a finding is carried into the builder's
            # evidence — quoting it would make the rejection itself the channel.
            findings.append(f"{name}: outside the permitted range 0..{_MAX_COUNT}")
            return None
        return value

    def _pattern(name: str, pattern: re.Pattern[str]) -> str | None:
        value = payload.get(name)
        if not isinstance(value, str):
            findings.append(f"{name}: expected a string, got {type(value).__name__}")
            return None
        if not pattern.match(value):
            findings.append(
                f"{name}: does not match its permitted shape; a field that is merely "
                "a string is a free channel across the seam"
            )
            return None
        return value

    exit_status = payload.get("exit_status")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int) or not 0 <= exit_status <= 255:
        findings.append("exit_status: expected an integer process exit status in 0..255")
        exit_status = None

    holdout_count = _int("holdout_count")
    mutant_count = _int("mutant_count")
    killed_count = _int("killed_count")
    request_id = _pattern("generation_request_id", _ID_PATTERN)
    store_hash = _pattern("store_content_hash", _HASH_PATTERN)
    generator_version = _pattern("generator_version", _VERSION_PATTERN)
    oracle_version = _pattern("oracle_version", _VERSION_PATTERN)
    generated_at = _pattern("generated_at", _TIMESTAMP_PATTERN)

    # Neither of the two enumerated fields quotes the value it rejected. That is
    # deliberate: a free-form ``failure_class`` was already refused before this
    # change, but the refusal message carried the refused string into
    # ``rejected_because`` and out into the builder's evidence — the field was
    # closed and the complaint about it was not.
    raw_class = payload.get("failure_class")
    failure_class: FailureClass | None = None
    if raw_class is not None:
        if not isinstance(raw_class, str) or raw_class not in {f.value for f in FailureClass}:
            findings.append(
                "failure_class: not a typed class; only typed classes cross the seam"
            )
        else:
            failure_class = FailureClass(raw_class)

    raw_reason = payload.get("failure_reason")
    failure_reason: GeneratorFailureReason | None = None
    if raw_reason is not None:
        if (
            not isinstance(raw_reason, str)
            or raw_reason not in {r.value for r in GeneratorFailureReason}
        ):
            findings.append(
                "failure_reason: not a member of the closed vocabulary; the reason a "
                "generation failed crosses the seam as an enumerated token, never as a "
                "message, because stderr is discarded precisely so that generated text "
                "cannot ride back"
            )
        else:
            failure_reason = GeneratorFailureReason(raw_reason)

    raw_rate = payload.get("kill_rate")
    kill_rate: float | None = None
    if raw_rate is None:
        kill_rate = None
    elif isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float)):
        findings.append(f"kill_rate: expected a number, got {type(raw_rate).__name__}")
    elif not 0.0 <= float(raw_rate) <= 1.0:
        findings.append("kill_rate: outside 0.0..1.0")
    else:
        kill_rate = float(raw_rate)

    if findings:
        return None, findings

    # A failure that does not say why is the state this field exists to end.
    # Enforced rather than encouraged: an optional diagnosis is one a generator
    # can quietly stop sending, and the seam would be back to `ORACLE_INVALID`
    # with no way to tell which of four conditions produced it. The pairing is
    # symmetric — a run that minted a set has nothing to explain, so a reason
    # without a class is an inconsistent receipt and is rejected, not repaired.
    if failure_class is not None and failure_reason is None:
        findings.append(
            "failure_class is set but failure_reason is absent; a generator that "
            "reports a failure must report which failure, because the builder "
            "cannot read the generator's stderr or its log"
        )
    if failure_reason is not None and failure_class is None:
        findings.append(
            "failure_reason is set but failure_class is absent; a reason without a "
            "class is not a classified failure"
        )

    assert killed_count is not None and mutant_count is not None
    if killed_count > mutant_count:
        findings.append(
            f"killed_count {killed_count} exceeds mutant_count {mutant_count}; "
            "an inconsistent receipt is rejected, not repaired"
        )
    derived = (killed_count / mutant_count) if mutant_count else 0.0
    if kill_rate is None:
        kill_rate = round(derived, 6)
    elif abs(kill_rate - derived) > 1e-6:
        findings.append(
            f"kill_rate {kill_rate} disagrees with killed_count/mutant_count ({derived:.6f})"
        )

    if findings:
        return None, findings

    assert request_id and store_hash and generator_version and oracle_version and generated_at
    assert exit_status is not None and holdout_count is not None
    return (
        GenerationReceipt(
            generation_request_id=request_id,
            exit_status=exit_status,
            holdout_count=holdout_count,
            mutant_count=mutant_count,
            killed_count=killed_count,
            kill_rate=kill_rate,
            store_content_hash=store_hash,
            generator_version=generator_version,
            oracle_version=oracle_version,
            generated_at=generated_at,
            failure_class=failure_class,
            failure_reason=failure_reason,
        ),
        [],
    )


@dataclass
class SeamOutcome:
    """What the builder knows after asking the verifier identity to generate."""

    generation_request_id: str
    state: ProjectState | TaskState
    receipt: GenerationReceipt | None = None
    rejected_because: list[str] = field(default_factory=list)
    invoked_as: str | None = None
    stdout_bytes_discarded: int = 0

    @property
    def accepted(self) -> bool:
        return self.receipt is not None and not self.rejected_because

    def as_evidence(self) -> dict[str, Any]:
        return {
            "check": "role_separated_generation_under_verifier_identity",
            "decision_ref": "DEC-006",
            "contract_ref": "contract_17.2_and_12.2",
            "seam_version": SEAM_VERSION,
            "generation_request_id": self.generation_request_id,
            "state": self.state.value,
            "invoked_as": self.invoked_as,
            "permitted_receipt_fields": list(PERMITTED_RECEIPT_FIELDS),
            "receipt": self.receipt.as_body() if self.receipt else None,
            "rejected_because": self.rejected_because,
            "stdout_bytes_discarded": self.stdout_bytes_discarded,
            "stderr_read_by_builder": False,
            "holdout_content_in_builder_process": False,
            "recorded_at": utc_now(),
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.generation_receipt",
            created_by_alias="release-v04",
            body=self.as_evidence(),
        )


class GenerationSeam:
    """Invokes the generator under the verifier identity. Reads back a receipt."""

    def __init__(
        self,
        identity: VerifierIdentity | None = None,
        *,
        timeout_seconds: float = 900.0,
    ) -> None:
        self._identity = identity or default_identity()
        self._timeout = timeout_seconds

    @property
    def identity(self) -> VerifierIdentity:
        return self._identity

    def _argv(self, request: GenerationRequest) -> list[str]:
        # ``sudo -n``: never prompt. A seam that can block on a password prompt
        # in an autonomous run is a seam that hangs the project.
        return [
            "sudo", "-n", "-u", self._identity.user,
            str(self._identity.generator),
            *request.as_argv(),
        ]

    def generate(self, request: GenerationRequest) -> SeamOutcome:
        if shutil.which("sudo") is None:
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=ProjectState.BLOCKED_EXTERNAL_ACCESS,
                rejected_because=["sudo is unavailable; the verifier identity cannot be entered"],
            )
        if not self._identity.generator.exists():
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=ProjectState.BLOCKED_EXTERNAL_ACCESS,
                rejected_because=[
                    f"the generator is not installed at {self._identity.generator}; "
                    "run deploy/verifier/provision.sh under the owner's authority"
                ],
            )

        argv = self._argv(request)
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=False,
                stdout=subprocess.PIPE,
                # Never read. A traceback can contain holdout text; the generator
                # writes its own log inside the sealed store instead.
                stderr=subprocess.DEVNULL,
                timeout=self._timeout,
                text=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired:
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=[f"generator exceeded {self._timeout}s"],
                invoked_as=self._identity.user,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=ProjectState.BLOCKED_EXTERNAL_ACCESS,
                rejected_because=[f"{type(exc).__name__}: {exc}"],
                invoked_as=self._identity.user,
            )

        raw = proc.stdout or b""
        discarded = max(0, len(raw) - MAX_STDOUT_BYTES)
        # Bounded read. Whatever exceeds the cap is dropped here and never
        # placed in a value this function returns.
        text = raw[:MAX_STDOUT_BYTES].decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        discarded += sum(len(ln) for ln in lines[:-1])

        if not lines:
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=[f"generator exited {proc.returncode} and emitted no receipt"],
                invoked_as=self._identity.user,
                stdout_bytes_discarded=discarded,
            )

        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=["the generator's final stdout line is not a JSON receipt"],
                invoked_as=self._identity.user,
                stdout_bytes_discarded=discarded,
            )

        receipt, findings = validate_receipt(payload)
        if findings:
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=findings,
                invoked_as=self._identity.user,
                stdout_bytes_discarded=discarded,
            )

        assert receipt is not None
        if receipt.generation_request_id != request.generation_request_id:
            return SeamOutcome(
                generation_request_id=request.generation_request_id,
                state=TaskState.FAILED_PROVENANCE,
                rejected_because=[
                    f"receipt is for {receipt.generation_request_id!r}, not the submitted request"
                ],
                invoked_as=self._identity.user,
                stdout_bytes_discarded=discarded,
            )

        return SeamOutcome(
            generation_request_id=request.generation_request_id,
            state=TaskState.PASSED if receipt.mint_accepted else TaskState.VERIFYING,
            receipt=receipt,
            invoked_as=self._identity.user,
            stdout_bytes_discarded=discarded,
        )
