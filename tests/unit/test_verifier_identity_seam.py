"""DEC-006 option B — the seam carries a status and counts, and cannot carry content.

The seam is the only channel out of the verifier identity, so it is the only
place holdout content could ride back into the builder process. DEC-006 is
explicit that "even transiently" is a breach, so these tests are written the way
the §17.2 response tests are: shape first, then field shapes, then consistency,
with a negative control for each thing that could widen the channel.

The measurement tests deliberately assert *semantics* rather than the state of
this particular host. An unprovisioned machine must not read as a proven
boundary, and a measurement taken as root must not read as success — both are
failure modes that would make the evidence say something untrue.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from governance.states import FailureClass, ProjectState, TaskState
from verifier_identity.identity import (
    BUILDER_CANNOT_ESCALATE,
    IdentityMeasurement,
    PathFacts,
    VerifierIdentity,
    measure,
    path_facts,
)
from verifier_identity.seam import (
    MAX_STDOUT_BYTES,
    PERMITTED_RECEIPT_FIELDS,
    GenerationRequest,
    GenerationSeam,
    GeneratorFailureReason,
    validate_receipt,
)

GOOD = {
    "generation_request_id": "GEN-0001",
    "exit_status": 0,
    "holdout_count": 12,
    "mutant_count": 25,
    "killed_count": 25,
    "kill_rate": 1.0,
    "store_content_hash": "sha256:" + "a" * 64,
    "generator_version": "1.0.0",
    "oracle_version": "holdout-mint-1.0.0",
    "generated_at": "2026-08-02T13:49:19Z",
    "failure_class": None,
    "failure_reason": None,
}

#: A failed receipt. Both enumerated fields are set, because the seam requires
#: a failure to say why.
FAILED = {
    **GOOD,
    "exit_status": 1,
    "holdout_count": 0,
    "mutant_count": 0,
    "killed_count": 0,
    "kill_rate": 0.0,
    "failure_class": FailureClass.ORACLE_INVALID.value,
    "failure_reason": GeneratorFailureReason.MUTANT_AUTHOR_EMPTY_GENERATION.value,
}

#: What a leak would actually look like: a fragment of a holdout body. Used as
#: the negative control everywhere a field could be widened into a message.
HOLDOUT_FRAGMENT = "def test_refuses(): assert subject.Machine().to('ARMED') is False"


def test_a_well_formed_receipt_validates():
    receipt, findings = validate_receipt(dict(GOOD))
    assert findings == []
    assert receipt is not None
    assert receipt.holdout_count == 12
    assert receipt.mint_accepted is True


# -- the closed key set ----------------------------------------------------
def test_an_extra_field_is_rejected_not_ignored():
    """A seam that tolerates extra fields is one the other side can widen."""
    payload = {**GOOD, "holdout_bodies": "def test_x(): assert False"}
    receipt, findings = validate_receipt(payload)
    assert receipt is None
    assert any("outside the permitted set" in f for f in findings)


def test_the_permitted_set_is_exactly_what_dec_006_allows():
    assert set(PERMITTED_RECEIPT_FIELDS) == set(GOOD)


def test_a_missing_required_field_is_rejected():
    payload = {k: v for k, v in GOOD.items() if k != "store_content_hash"}
    receipt, findings = validate_receipt(payload)
    assert receipt is None
    assert any("omits required fields" in f for f in findings)


# -- per-field shapes ------------------------------------------------------
@pytest.mark.parametrize(
    "field,value",
    [
        # A hash field is the widest scalar in the shape, so it is pinned to
        # exactly 64 hex digits rather than "a string that starts with sha256".
        ("store_content_hash", "sha256:" + "a" * 63),
        ("store_content_hash", "sha256:def test_leak(): assert 1"),
        ("store_content_hash", "a" * 64),
        ("generator_version", "1.0.0; def leak(): pass"),
        ("oracle_version", "x" * 65),
        ("generated_at", "sometime on tuesday"),
        ("generation_request_id", "GEN-0001\ndef test(): ..."),
    ],
)
def test_a_field_that_is_merely_a_string_is_refused(field, value):
    receipt, findings = validate_receipt({**GOOD, field: value})
    assert receipt is None
    assert any(field in f for f in findings)


@pytest.mark.parametrize("field", ["holdout_count", "mutant_count", "killed_count"])
def test_counts_are_bounded_integers(field):
    for bad_value in (-1, 10**9, "12", 1.5, True):
        receipt, findings = validate_receipt({**GOOD, field: bad_value})
        assert receipt is None, f"{field}={bad_value!r} was accepted"
        assert any(field in f for f in findings)


def test_exit_status_must_be_a_process_exit_status():
    for bad_value in (-1, 256, "0", None):
        receipt, findings = validate_receipt({**GOOD, "exit_status": bad_value})
        assert receipt is None
        assert any("exit_status" in f for f in findings)


def test_failure_class_must_be_a_typed_class():
    receipt, findings = validate_receipt({**GOOD, "failure_class": "the model said no"})
    assert receipt is None
    assert any("not a typed class" in f for f in findings)

    # A class now travels with its reason; see the failure-reason block below.
    receipt, findings = validate_receipt(
        {
            **GOOD,
            "exit_status": 7,
            "failure_class": FailureClass.HOLDOUT_FAILURE.value,
            "failure_reason": GeneratorFailureReason.KILL_RATE_BELOW_THRESHOLD.value,
        }
    )
    assert findings == []
    assert receipt is not None and receipt.failure_class is FailureClass.HOLDOUT_FAILURE


# -- the failure reason: a diagnosis, not a message ------------------------
#
# stderr is sent to DEVNULL by design, so a generator failure is otherwise
# invisible from the build side: `exit 1, ORACLE_INVALID` covered an unanswered
# transport decision, a truncated mutant author and an absent pytest alike. The
# receipt therefore carries the reason itself — and the only way to do that
# without reopening the channel stderr was closed for is a closed vocabulary.
def test_a_free_form_failure_reason_is_refused():
    """The point of the field: an enum, never a string.

    A generator that could write prose here would have a text channel into the
    builder process, which is the one thing DEC-006 says must not exist. It does
    not matter that this particular string is plausible-looking English; what
    matters is that no string outside the vocabulary is accepted.
    """
    for value in (
        "the mutant author returned nothing",
        HOLDOUT_FRAGMENT,
        "MUTANT_AUTHOR_EMPTY_GENERATION: kimi-k2.7-code returned an empty generation",
        "mutant_author_empty_generation",  # the right token, wrong case
        "MUTANT_AUTHOR_EMPTY_GENERATION ",  # ... and with a trailing space
        "",
        42,
        ["MUTANT_AUTHOR_EMPTY_GENERATION"],
        {"reason": "MUTANT_AUTHOR_EMPTY_GENERATION"},
    ):
        receipt, findings = validate_receipt({**FAILED, "failure_reason": value})
        assert receipt is None, f"failure_reason={value!r} was accepted"
        assert any("closed vocabulary" in f for f in findings)


def test_a_rejected_failure_reason_is_not_quoted_back_to_the_builder():
    """The refusal must not become the channel the field is not.

    ``rejected_because`` is carried into the builder's evidence record, so a
    finding that quoted the value it rejected would deliver exactly the bytes
    the closed vocabulary refused to deliver.
    """
    receipt, findings = validate_receipt({**FAILED, "failure_reason": HOLDOUT_FRAGMENT})
    assert receipt is None
    blob = json.dumps(findings)
    assert HOLDOUT_FRAGMENT not in blob
    assert "subject.Machine" not in blob and "ARMED" not in blob


def test_a_rejected_failure_class_is_not_quoted_back_either():
    """Same hole, the older field. It was quoting the string it refused."""
    receipt, findings = validate_receipt({**FAILED, "failure_class": HOLDOUT_FRAGMENT})
    assert receipt is None
    assert HOLDOUT_FRAGMENT not in json.dumps(findings)
    assert any("not a typed class" in f for f in findings)


def test_an_out_of_range_count_is_not_quoted_back_either():
    """An unbounded integer is an unbounded number of bits."""
    payload_bits = 10 ** 400 + 12345
    receipt, findings = validate_receipt({**GOOD, "holdout_count": payload_bits})
    assert receipt is None
    assert str(payload_bits) not in json.dumps(findings)
    assert any("holdout_count" in f for f in findings)


@pytest.mark.parametrize("reason", sorted(r.value for r in GeneratorFailureReason))
def test_every_reason_in_the_vocabulary_is_accepted(reason):
    """The allowlist and the validator move together or the field is useless."""
    receipt, findings = validate_receipt({**FAILED, "failure_reason": reason})
    assert findings == []
    assert receipt is not None
    assert receipt.failure_reason is GeneratorFailureReason(reason)
    assert receipt.as_body()["failure_reason"] == reason


def test_a_reported_failure_must_say_which_failure():
    """`ORACLE_INVALID` with no reason is the state this field exists to end."""
    payload = {**FAILED, "failure_reason": None}
    receipt, findings = validate_receipt(payload)
    assert receipt is None
    assert any("must report which failure" in f for f in findings)

    # Absent is the same as null; a generator cannot opt out by omission.
    receipt, findings = validate_receipt({k: v for k, v in payload.items() if k != "failure_reason"})
    assert receipt is None
    assert any("must report which failure" in f for f in findings)


def test_a_reason_without_a_class_is_refused():
    receipt, findings = validate_receipt(
        {**GOOD, "failure_reason": GeneratorFailureReason.KILL_RATE_BELOW_THRESHOLD.value}
    )
    assert receipt is None
    assert any("not a classified failure" in f for f in findings)


def test_a_successful_receipt_carries_no_reason():
    receipt, findings = validate_receipt(dict(GOOD))
    assert findings == []
    assert receipt is not None and receipt.failure_reason is None
    assert receipt.as_body()["failure_reason"] is None


def test_the_vocabulary_distinguishes_the_causes_that_collapsed_into_one_class():
    """The measured motivation, asserted rather than described.

    On 2026-08-03 four conditions were indistinguishable at the seam because all
    four report ``ORACLE_INVALID``. Each must now be separately nameable.
    """
    names = {r.value for r in GeneratorFailureReason}
    assert {
        "TRANSPORT_DECISION_UNRECORDED",
        "TARGET_COUNT_NOT_POSITIVE",
        "MUTANT_AUTHOR_EMPTY_GENERATION",
        "TEST_RUNNER_UNAVAILABLE",
    } <= names
    # And the exit-7 pair, which is the one that matters for a gate: a weak
    # holdout set and a set that measured nothing both exit 7, HOLDOUT_FAILURE.
    assert {"KILL_RATE_BELOW_THRESHOLD", "BASELINE_HOLDOUTS_FAILED"} <= names


def test_the_generator_holds_the_same_vocabulary():
    """The generator cannot import this module, so the copies are compared.

    DEC-006: a generator that imported the builder's tree would be a generator
    the builder controls. The price is a duplicated constant; the check is this.
    A reason the seam does not know is a receipt the seam rejects, which would
    turn a diagnosable failure back into an unexplained one.
    """
    source = Path(__file__).resolve().parents[2] / "deploy" / "verifier" / "generator.py"
    tree = ast.parse(source.read_text())
    theirs: set[str] | None = None
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "FAILURE_REASONS" for t in targets):
            continue
        assert isinstance(node.value, ast.Tuple), "FAILURE_REASONS must be a literal tuple"
        theirs = {
            e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
    assert theirs is not None, "the generator no longer declares FAILURE_REASONS"
    assert theirs == {r.value for r in GeneratorFailureReason}


def _load_generator():
    """Import the sealed generator by path. It is stdlib-only, so this is safe.

    It is not on the import path and must not be: it is installed root-owned to
    ``/opt/efah-verifier/bin`` and runs under another identity. Loading the
    source here tests the copy that ``provision.sh`` installs.
    """
    import importlib.util

    source = Path(__file__).resolve().parents[2] / "deploy" / "verifier" / "generator.py"
    spec = importlib.util.spec_from_file_location("_sealed_generator_under_test", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generator_refuses_to_emit_a_reason_it_does_not_recognise():
    """Belt and braces, on the side that writes the field.

    The seam rejecting a free string protects the builder. This protects the
    *evidence*: a future edit that passed an exception message through would
    otherwise produce a receipt the seam refuses outright, losing the diagnosis
    entirely. Membership is checked once more at the point of emission, so an
    unrecognised value degrades to ``UNCLASSIFIED_EXCEPTION`` rather than
    escaping or destroying the receipt.
    """
    generator = _load_generator()

    assert generator.emit_failure_reason(None) is None
    assert (
        generator.emit_failure_reason("MUTANT_AUTHOR_EMPTY_GENERATION")
        == "MUTANT_AUTHOR_EMPTY_GENERATION"
    )
    for smuggled in (HOLDOUT_FRAGMENT, "kimi-k2.7-code returned an empty generation", ""):
        assert generator.emit_failure_reason(smuggled) == "UNCLASSIFIED_EXCEPTION"

    # And the coerced value is itself one the seam accepts.
    receipt, findings = validate_receipt(
        {**FAILED, "failure_reason": generator.emit_failure_reason(HOLDOUT_FRAGMENT)}
    )
    assert findings == []
    assert receipt is not None
    assert receipt.failure_reason is GeneratorFailureReason.UNCLASSIFIED_EXCEPTION


def test_the_generator_classifies_the_measured_failure_as_the_mutant_author():
    """The 2026-08-03 zero-holdout run, reproduced as a unit.

    ``kimi-k2.7-code`` returned ``finish_reason=length`` with zero content
    deltas. The generator raised, ``_classify`` fell through to
    ``ORACLE_INVALID``, and the receipt said ``holdout_count: 0`` — although the
    holdouts had been authored and written to the store. The class is unchanged
    (it is the right §10.6 class); the reason is the part that was missing.
    """
    generator = _load_generator()

    truncated = generator.GeneratorFailure(
        "MUTANT_AUTHOR_TRUNCATED", "kimi-k2.7-code generation was truncated"
    )
    assert generator._classify(truncated) == ("ORACLE_INVALID", "MUTANT_AUTHOR_TRUNCATED")

    empty = generator.GeneratorFailure(
        "MUTANT_AUTHOR_EMPTY_GENERATION", "kimi-k2.7-code returned an empty generation"
    )
    assert generator._classify(empty) == ("ORACLE_INVALID", "MUTANT_AUTHOR_EMPTY_GENERATION")

    # The same class, three different diagnoses — which is the whole complaint.
    assert generator._classify(
        generator.GeneratorFailure("TEST_RUNNER_UNAVAILABLE", "no pytest")
    ) == ("ORACLE_INVALID", "TEST_RUNNER_UNAVAILABLE")

    # An untyped exception is not silently promoted to a specific diagnosis.
    assert generator._classify(ValueError("something")) == (
        "ORACLE_INVALID",
        "UNCLASSIFIED_EXCEPTION",
    )
    # ... and a lie about the reason does not survive classification either.
    assert generator._classify(generator.GeneratorFailure(HOLDOUT_FRAGMENT, "x")) == (
        "ORACLE_INVALID",
        "UNCLASSIFIED_EXCEPTION",
    )


def test_the_generators_failure_receipt_validates_at_the_seam(capsys):
    """End to end on the shape: what the generator writes, the seam accepts.

    The two sides are separately written and separately validated, so the only
    thing that proves they meet is feeding one to the other.
    """
    generator = _load_generator()

    exit_status = generator.failure(
        "GEN-0001", 1, "ORACLE_INVALID", "sha256:" + "b" * 64, "MUTANT_AUTHOR_TRUNCATED"
    )
    assert exit_status == 1

    emitted = json.loads(capsys.readouterr().out.strip())
    assert set(emitted) <= set(PERMITTED_RECEIPT_FIELDS)

    receipt, findings = validate_receipt(emitted)
    assert findings == []
    assert receipt is not None
    assert receipt.failure_reason is GeneratorFailureReason.MUTANT_AUTHOR_TRUNCATED
    assert receipt.mint_accepted is False


def test_a_non_object_receipt_is_rejected():
    for payload in ("def test(): ...", ["a"], 12, None):
        receipt, findings = validate_receipt(payload)
        assert receipt is None and findings


# -- cross-field consistency ----------------------------------------------
def test_killed_cannot_exceed_mutants():
    receipt, findings = validate_receipt({**GOOD, "killed_count": 26})
    assert receipt is None
    assert any("exceeds mutant_count" in f for f in findings)


def test_a_claimed_kill_rate_must_match_the_counts_it_derives_from():
    receipt, findings = validate_receipt({**GOOD, "killed_count": 20, "kill_rate": 1.0})
    assert receipt is None
    assert any("disagrees with" in f for f in findings)


def test_kill_rate_is_derived_when_absent():
    payload = {k: v for k, v in GOOD.items() if k != "kill_rate"}
    payload["killed_count"] = 20
    receipt, findings = validate_receipt(payload)
    assert findings == []
    assert receipt is not None and receipt.kill_rate == pytest.approx(0.8)


# -- DEC-006's mint rule ---------------------------------------------------
def test_the_mint_refuses_a_kill_rate_below_one():
    """"A holdout that fails to kill any known-bad mutant tests nothing."""
    receipt, _ = validate_receipt({**GOOD, "killed_count": 24, "kill_rate": 0.96})
    assert receipt is not None
    assert receipt.mint_accepted is False


def test_the_mint_refuses_an_empty_holdout_set():
    receipt, _ = validate_receipt({**GOOD, "holdout_count": 0})
    assert receipt is not None
    assert receipt.mint_accepted is False


def test_the_mint_refuses_a_set_with_no_mutants_to_kill():
    """A kill rate of 1.0 over zero mutants is a division that means nothing."""
    receipt, _ = validate_receipt(
        {**GOOD, "mutant_count": 0, "killed_count": 0, "kill_rate": 0.0}
    )
    assert receipt is not None
    assert receipt.mint_accepted is False


# -- the request direction -------------------------------------------------
def test_the_builder_cannot_describe_what_the_holdouts_should_contain():
    """A builder that could shape the holdouts would be writing its own exam."""
    request = GenerationRequest(
        generation_request_id="GEN-1",
        candidate_commit="deadbeef",
        contract_version="1.1",
        target_count=5,
    )
    argv = request.as_argv()
    assert set(argv[::2]) == {
        "--request-id",
        "--candidate-commit",
        "--contract-version",
        "--target-count",
    }


# -- the seam's own refusals -----------------------------------------------
def test_an_uninstalled_generator_is_a_typed_blocker_not_a_fallback(tmp_path):
    """No local fallback. A locally-generated holdout is a circular one."""
    identity = VerifierIdentity(generator=tmp_path / "absent")
    outcome = GenerationSeam(identity).generate(
        GenerationRequest("GEN-2", "deadbeef", "1.1", 1)
    )
    assert outcome.state is ProjectState.BLOCKED_EXTERNAL_ACCESS
    assert outcome.receipt is None
    assert any("not installed" in r for r in outcome.rejected_because)


def test_a_receipt_for_another_request_is_refused(monkeypatch, tmp_path):
    generator = tmp_path / "gen"
    generator.write_text("#!/bin/sh\n")
    identity = VerifierIdentity(generator=generator)
    seam = GenerationSeam(identity)

    class _Proc:
        returncode = 0
        stdout = json.dumps({**GOOD, "generation_request_id": "GEN-OTHER"}).encode()

    monkeypatch.setattr("verifier_identity.seam.subprocess.run", lambda *a, **k: _Proc())
    monkeypatch.setattr("verifier_identity.seam.shutil.which", lambda _: "/usr/bin/sudo")

    outcome = seam.generate(GenerationRequest("GEN-3", "deadbeef", "1.1", 1))
    assert outcome.state is TaskState.FAILED_PROVENANCE
    assert any("not the submitted request" in r for r in outcome.rejected_because)


def test_oversized_stdout_is_discarded_and_counted(monkeypatch, tmp_path):
    """Bounded read. The excess never reaches a returned value."""
    generator = tmp_path / "gen"
    generator.write_text("#!/bin/sh\n")
    seam = GenerationSeam(VerifierIdentity(generator=generator))

    flood = ("x" * 200 + "\n") * 200  # well over the cap

    class _Proc:
        returncode = 0
        stdout = flood.encode()

    monkeypatch.setattr("verifier_identity.seam.subprocess.run", lambda *a, **k: _Proc())
    monkeypatch.setattr("verifier_identity.seam.shutil.which", lambda _: "/usr/bin/sudo")

    outcome = seam.generate(GenerationRequest("GEN-4", "deadbeef", "1.1", 1))
    assert outcome.state is TaskState.FAILED_PROVENANCE
    assert outcome.stdout_bytes_discarded > 0
    assert len(flood.encode()) > MAX_STDOUT_BYTES
    # Nothing from the flood is anywhere in the evidence record.
    assert "xxxx" not in json.dumps(outcome.as_evidence())


def test_the_evidence_record_states_what_it_did_not_read():
    outcome = GenerationSeam(VerifierIdentity(generator=Path("/nonexistent"))).generate(
        GenerationRequest("GEN-5", "deadbeef", "1.1", 1)
    )
    evidence = outcome.as_evidence()
    assert evidence["stderr_read_by_builder"] is False
    assert evidence["holdout_content_in_builder_process"] is False


# -- identity measurement semantics ---------------------------------------
def test_a_denied_stat_is_recorded_as_a_denial_not_an_absence(tmp_path):
    """FINDING-004's lesson: absence of a signal is not success."""
    facts = path_facts(tmp_path / "genuinely-absent")
    assert facts.exists is False and facts.stat_denied is False


def _measurement(**overrides) -> IdentityMeasurement:
    base = {
        "identity": VerifierIdentity(),
        "builder_user": "builder",
        "builder_uid": 1000,
        "verifier_uid": 995,
        "identities_distinct": True,
        "home": PathFacts(exists=True, mode="0o700"),
        "store": PathFacts(exists=None, stat_denied=True, denial="PermissionError"),
        "generator": PathFacts(exists=True, mode="0o755"),
        "credential": PathFacts(exists=None, stat_denied=True),
        "builder_read_attempt": (False, "PermissionError: Permission denied"),
        "sudoers": {},
        "measured_as_root": False,
    }
    return IdentityMeasurement(**{**base, **overrides})


def test_an_unprovisioned_host_is_not_a_proven_boundary():
    """`store does not exist` must not read the same as `store is unreadable`."""
    m = _measurement(
        store=PathFacts(exists=False),
        builder_read_attempt=(False, "store does not exist; nothing has been provisioned"),
    )
    assert m.provisioned is False
    assert m.isolation_holds is False


def test_a_root_run_measurement_cannot_report_success():
    m = _measurement(measured_as_root=True, builder_uid=0)
    assert m.isolation_holds is False


def test_isolation_holds_when_the_kernel_refused_the_builder():
    assert _measurement().isolation_holds is True


def test_the_evidence_carries_its_own_defeat_condition():
    """An isolation claim that omits the escalation path is not an honest one."""
    body = _measurement().as_body()
    assert body["honest_debt"]["builder_cannot_escalate"] is False
    assert "passwordless sudo" in body["honest_debt"]["why"]
    assert BUILDER_CANNOT_ESCALATE is False


def test_measure_never_raises_on_this_host():
    """Whatever the host looks like, a measurement is a result, not an exception."""
    assert measure().as_body()["check"] == "verifier_service_identity_isolation"
