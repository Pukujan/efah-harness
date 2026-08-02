"""GATE-D1-05's checks, and the proof that they can fail.

Contract Sections 10.5, 26 · Section 18. Both registered checks assert an
*absence*: no prior history at invocation, nothing in the payload the work unit
did not put there. Absences are the easiest thing in the world to verify by
accident -- a probe that opens an empty session and finds it empty passes
against any implementation, including one that would have carried a transcript
had there been one to carry.

So each check is run twice here: once against the real
:class:`workers.session.WorkerSession`, and once against a session that is
wrong in one identifiable way, with the finding it must produce named.

The broken subjects:

* ``SessionThatRemembers`` -- the check module's own control, a session that
  keeps every transcript in a process-wide map keyed by task and alias and
  reopens it on the next invocation. This is persistent conversational memory,
  which Section 10.5 prohibits by default;
* ``SessionThatOutlivesItsClose`` -- fresh at open, but a closed session keeps
  working. Reuse is how a transcript survives an invocation boundary without
  anybody deciding that it should;
* ``SessionThatBriefsOnTheProject`` -- fresh and non-reusable, but the payload
  carries a project-wide brief. Fresh sessions and bounded context are separate
  properties, and this subject exists to keep A1 and A3 from collapsing into one
  another: it must pass A1 and fail A3;
* and, for each check, an arm that leaves the *negative control* inert, because
  a control that cannot fire is a check that cannot fail.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from evaluation import checks_d1_05
from evaluation.binding import CandidateBinding
from evaluation.checks import CHECKS, AssertionStatus, GateContext
from evaluation.checks_d1_05 import CHECKS_D1_05, PRIOR_TURN_SENTINEL, SessionThatRemembers
from evaluation.gate_runner import Executability, GateRunner
from evaluation.gate_spec import AssertionSpec, GateSpec, load_all_gates
from governance.states import Verdict
from workers.session import WorkerSession, WorkUnit

GATE_ID = "GATE-D1-05"
REGISTERED = ("A1", "A3")
#: A2 needs an egress record; A4 needs a live TerminusDB. See CHECKS_D1_05.
UNREGISTERED = ("A2", "A4")

ARTIFACT = {"A1": "session_initialization_logs", "A3": "context_payload_sample"}

PROJECT_BRIEF = "SENTINEL-PROJECT-0c84fa: the project ships twenty-six work units"


@pytest.fixture(scope="module")
def gates() -> dict[str, GateSpec]:
    return load_all_gates()


@pytest.fixture(scope="module")
def gate(gates: dict[str, GateSpec]) -> GateSpec:
    return gates[GATE_ID]


@pytest.fixture(scope="module")
def ctx(gates: dict[str, GateSpec]) -> GateContext:
    return GateContext(binding=CandidateBinding(commit_sha="a" * 40), gates=gates)


def assertion(gate: GateSpec, assertion_id: str) -> AssertionSpec:
    return next(a for a in gate.assertions if a.assertion_id == assertion_id)


def run(ctx: GateContext, gate: GateSpec, assertion_id: str):
    check = CHECKS_D1_05[(GATE_ID, assertion_id)]
    return check(ctx, gate, assertion(gate, assertion_id))


@pytest.fixture(autouse=True)
def _clear_the_control_memory():
    """The control's transcript store is process-wide by construction.

    Left dirty it would leak between tests, and a leak in the *test* subject is
    indistinguishable from the leak the gate is looking for.
    """
    SessionThatRemembers._memory.clear()
    yield
    SessionThatRemembers._memory.clear()


# --- broken subjects -------------------------------------------------------


class SessionThatOutlivesItsClose(WorkerSession):
    """Fresh at open, but a closed session keeps answering.

    Nothing here carries memory forward on its own. What it removes is the
    boundary: a caller holding a closed session can keep talking to it, and the
    transcript survives the invocation that owned it.
    """

    def close(self) -> dict[str, Any]:
        summary = {
            "session_id": self.session_id,
            "task_id": self.work_unit.task_id,
            "role": self.work_unit.role,
            "alias": self.alias,
            "opened_at": self.opened_at,
            "closed_at": self.opened_at,
            "turns": len(self._turns),
            "input_hash": self.work_unit.input_hash,
        }
        self.closed = True
        return summary

    def _assert_open(self) -> None:
        return


class SessionThatBriefsOnTheProject(WorkerSession):
    """A fresh, non-reusable session whose payload is bounded by the project.

    A1 must still pass against it -- the session really is fresh -- and A3 must
    not. Without this subject the two assertions could both be satisfied by one
    property and nobody would notice.
    """

    def messages(self) -> list[dict[str, Any]]:
        messages = super().messages()
        messages.insert(0, {"role": "system", "content": PROJECT_BRIEF})
        return messages


# --- the registry ----------------------------------------------------------


def test_the_registry_holds_exactly_the_assertions_that_are_executable(gate: GateSpec):
    declared = {a.assertion_id for a in gate.assertions}
    registered = {aid for (gid, aid) in CHECKS_D1_05 if gid == GATE_ID}
    assert registered == set(REGISTERED)
    assert declared - registered == set(UNREGISTERED)
    assert all(gid == GATE_ID for gid, _ in CHECKS_D1_05)


def test_a2_and_a4_are_left_unregistered_rather_than_passed_on_their_visible_halves():
    """A2 is ``egress_inspection`` and A4 is ``post_session_state_query``.

    Both have a half this build can see -- the adapter's only outbound call goes
    through the gateway, and ``close()`` demonstrably drops the transcript -- and
    a half it cannot: no egress record exists, and no TerminusDB is running.
    Registering the visible half would report the whole assertion decided.
    """
    assert (GATE_ID, "A2") not in CHECKS_D1_05
    assert (GATE_ID, "A4") not in CHECKS_D1_05


def test_merged_entries_resolve_to_this_modules_checks():
    for key, check in CHECKS_D1_05.items():
        if key in CHECKS:
            assert CHECKS[key] is check, f"{key} resolves to a different check than this module's"


# --- A1 --------------------------------------------------------------------


def test_a1_passes_against_the_real_session(ctx, gate):
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    logs = outcome.evidence["session_initialization_logs"]
    real = logs["real_session"]
    assert real["second_invocation"]["prior_turn_count_at_open"] == 0
    assert real["second_invocation"]["distinct_session_id"] is True
    assert real["second_invocation"]["carries_the_first_sessions_words"] is False
    assert real["first_invocation"]["turns_at_close"] == 2
    assert real["first_invocation"]["transcript_retained_after_close"] == 0
    assert all(arm["refused"] for arm in real["closed_session_reuse"].values())
    assert real["structure"]["resume_shaped_attributes"] == []
    assert real["structure"]["history_shaped_constructor_parameters"] == []
    assert real["pack_disabling_fresh_sessions"]["refused"] is True


def test_a1_reads_the_owners_declared_session_policy(ctx, gate):
    declared = run(ctx, gate, "A1").evidence["session_initialization_logs"][
        "declared_session_policy"
    ]
    assert declared["fresh_per_invocation_worker_sessions"] is True
    assert declared["persistent_model_conversation_memory_default"] is False
    assert declared["chat_transcript_as_project_memory"] == "forbidden"


def test_a1_fails_against_a_session_that_reopens_the_previous_transcript(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_05, "WorkerSession", SessionThatRemembers)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("prior_turn_count=" in f for f in outcome.findings)
    assert any("previous invocation's words" in f for f in outcome.findings)


def test_a1_fails_when_a_closed_session_keeps_answering(ctx, gate, monkeypatch):
    """A distinct failure from carrying memory: the boundary is gone, so a
    transcript survives the invocation that owned it without anybody deciding
    that it should."""
    monkeypatch.setattr(checks_d1_05, "WorkerSession", SessionThatOutlivesItsClose)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("a closed session accepted" in f for f in outcome.findings)
    assert any("transcript was not discarded" in f for f in outcome.findings)


def test_a1_fails_when_the_negative_control_carries_nothing(ctx, gate, monkeypatch):
    """The control is load-bearing: point it at the real class and A1 must
    refuse to certify its own probe."""
    monkeypatch.setattr(checks_d1_05, "SessionThatRemembers", WorkerSession)
    outcome = run(ctx, gate, "A1")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in f for f in outcome.findings)


def test_a1_negative_control_is_caught_on_every_facet(ctx, gate):
    caught = run(ctx, gate, "A1").evidence["session_initialization_logs"]["negative_control"][
        "detector_caught"
    ]
    assert caught == {
        "prior_turns_carried": True,
        "previous_words_in_the_new_payload": True,
        "transcript_retained_at_close": True,
        "closed_session_reusable": True,
        "resume_path_exists": True,
    }


# --- A3 --------------------------------------------------------------------


def test_a3_passes_with_a_payload_bounded_by_the_work_unit(ctx, gate):
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.PASS, outcome.findings
    sample = outcome.evidence["context_payload_sample"]
    payload = sample["payload"]
    assert payload["payload_scope"] == "work_unit_inputs_only"
    assert payload["unaccounted_characters"] == 0
    assert payload["contaminants_present"] == []
    assert payload["unexpected_roles"] == []
    assert set(payload["message_roles"]) == {"system", "user"}


def test_a3_had_a_real_prior_transcript_available_to_leak(ctx, gate):
    """The contaminants are not hypothetical strings.

    One of them was recorded as a turn by an earlier session for the same task
    and alias, so a leaking implementation had something to find. An audit run
    where nothing could have leaked proves nothing.
    """
    sample = run(ctx, gate, "A3").evidence["context_payload_sample"]
    origin = sample["contaminants_in_scope_at_build_time"]["previous_session_transcript"]
    assert origin["text"] == PRIOR_TURN_SENTINEL
    assert "earlier session" in origin["where_it_lives"]


def test_a3_fails_when_the_payload_carries_the_previous_transcript(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_05, "WorkerSession", SessionThatRemembers)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("previous_session_transcript" in f for f in outcome.findings)
    assert any("not accounted for by any field" in f for f in outcome.findings)


def test_a3_fails_on_a_leak_no_sentinel_anticipated(ctx, gate, monkeypatch):
    """The residue arm is the one that matters.

    This subject leaks a string the check never searched for. A payload audit
    built only from sentinel lookups would pass it.
    """

    class SessionThatLeaksAnUnanticipatedString(WorkerSession):
        def messages(self) -> list[dict[str, Any]]:
            messages = super().messages()
            messages[-1]["content"] += "\nAlso: the release manager is on holiday until Tuesday."
            return messages

    monkeypatch.setattr(checks_d1_05, "WorkerSession", SessionThatLeaksAnUnanticipatedString)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("not accounted for by any field" in f for f in outcome.findings)
    assert not any("belongs to the project" in f for f in outcome.findings)


def test_a3_fails_when_the_negative_control_leaks_nothing(ctx, gate, monkeypatch):
    monkeypatch.setattr(checks_d1_05, "SessionThatRemembers", WorkerSession)
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("negative control did not fire" in f for f in outcome.findings)


def test_a1_and_a3_do_not_measure_the_same_property(ctx, gate, monkeypatch):
    """A session can be perfectly fresh and still over-briefed.

    If A1 also failed here, the two assertions would be one assertion and a
    project-scoped payload from a fresh session would have nothing watching it.
    """
    monkeypatch.setattr(checks_d1_05, "WorkerSession", SessionThatBriefsOnTheProject)
    assert run(ctx, gate, "A1").status is AssertionStatus.PASS
    outcome = run(ctx, gate, "A3")
    assert outcome.status is AssertionStatus.FAIL
    assert any("project_wide_brief" in f for f in outcome.findings)


# --- evidence and integration ---------------------------------------------


@pytest.mark.parametrize("assertion_id", REGISTERED)
def test_every_check_emits_its_named_artifact_bound_to_the_candidate(ctx, gate, assertion_id):
    outcome = run(ctx, gate, assertion_id)
    assert ARTIFACT[assertion_id] in outcome.evidence
    binding = outcome.evidence["artifact_hashes_and_commit_binding"]
    assert binding["candidate_commit"] == ctx.binding.commit_sha
    assert binding["transcript_hash"].startswith("sha256:")
    assert binding["work_unit_input_hash"].startswith("sha256:")


@pytest.mark.parametrize("assertion_id", REGISTERED)
def test_every_check_carries_a_negative_control_with_a_stated_reason(ctx, gate, assertion_id):
    control = run(ctx, gate, assertion_id).evidence[ARTIFACT[assertion_id]]["negative_control"]
    assert control["probe"]
    assert control["why"]
    assert control["detector_fires"] is True


def test_the_gate_named_evidence_this_workstream_produces(ctx, gate: GateSpec):
    """Two of the gate's four named artifacts are produced here.

    ``litellm_request_log`` belongs to A2 and ``post_session_state_dump`` to A4,
    and neither is produced. That is why the gate below reports UNVERIFIABLE
    rather than PASS.
    """
    produced = {name for aid in REGISTERED for name in run(ctx, gate, aid).evidence}
    assert {"session_initialization_logs", "context_payload_sample"} <= produced
    assert set(gate.evidence_required) - produced == {
        "litellm_request_log",
        "post_session_state_dump",
    }


def test_a_work_unit_is_the_only_thing_the_check_hands_a_session():
    """The subject the checks probe is the shipped one, unmodified.

    ``WorkUnit`` is a frozen dataclass, so the probe cannot smuggle an extra
    field into it and then congratulate the session for not reading it.
    """
    unit = checks_d1_05._work_unit()
    assert isinstance(unit, WorkUnit)
    with pytest.raises(FrozenInstanceError):
        unit.instructions = "mutated"  # type: ignore[misc]


def test_the_registered_checks_drive_the_gate_runner(monkeypatch):
    """End to end through the runner, against the real HEAD.

    PARTIALLY_EXECUTABLE and therefore UNVERIFIABLE is the correct report: two
    assertions execute and pass, two have no check, and the runner refuses to
    call a gate PASS on the assertions that happened to be implemented.
    """
    for key, check in CHECKS_D1_05.items():
        monkeypatch.setitem(CHECKS, key, check)
    runner = GateRunner()
    result = runner.run([GATE_ID]).results[0]

    assert result.executability is Executability.PARTIALLY_EXECUTABLE
    assert result.verdict is Verdict.UNVERIFIABLE
    assert result.failed == []
    assert result.executed_count == len(REGISTERED)
    statuses = {a.assertion_id: a.status for a in result.assertions}
    assert all(statuses[aid] is AssertionStatus.PASS for aid in REGISTERED)
    assert all(statuses[aid] is AssertionStatus.NOT_IMPLEMENTED for aid in UNREGISTERED)
    assert sorted(result.evidence_missing) == ["litellm_request_log", "post_session_state_dump"]
