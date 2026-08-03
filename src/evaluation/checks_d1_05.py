"""GATE-D1-05 — fresh worker sessions execute bounded tasks.

Contract Sections 10.5 and 26. Two of the gate's four assertions are executed
here against the real :class:`workers.session.WorkerSession` and the real
``session_policy`` block of ``model-policy.yaml``:

    A1 each worker invocation opens a new session with empty prior history
    A3 worker context is bounded by the work unit, not by the project

A2 and A4 are **not** registered, and neither absence is laziness.

A2 asks that no model call bypasses the LiteLLM proxy, measured by
``egress_inspection``. The build side has no egress log: nothing here records
outbound connections, and reading the adapter's source to conclude that it only
calls the gateway would be a static claim answering a question whose method is
observational. GATE-D1-07's credential-stripped run already decides the static
half; A2 stays unimplemented until an egress record exists.

A4 asks that durable state survives the session ending, in TerminusDB *and*
git. ``WorkerSession.close`` returning a summary proves the transcript was
dropped, which is the half this module can see; the half that matters -- that
the work unit's output is still there afterwards -- needs a live TerminusDB.
Passing A4 on the visible half would report that durable state survived without
asking the store that holds it.

What both checks below have in common is that the property they test is
structural. ``WorkerSession`` has no resume path, no history parameter, and no
route from one invocation's transcript into the next; ``messages()`` is
assembled from the work unit's own fields and reads nothing else. Structural
properties are the easy ones to test badly, because the obvious probe -- open a
session and observe that it is empty -- passes against any implementation that
happens to be empty *this* time. So each check runs its predicate twice: once
against the real class, and once against a session that deliberately carries the
previous invocation's memory forward. The second run is what gives the first its
meaning, and the two run through the same predicate function so the control
cannot succeed by taking a different path.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from evaluation.gate_spec import AssertionSpec, GateSpec
from governance.envelope import content_hash
from models.errors import SessionReuseError
from models.policy import SessionPolicy, load_model_policy
from workers.session import WorkerSession, WorkUnit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope makes the pair circular -- and which side fails
# then depends on which one Python happens to load first, so the same code works
# through the gate runner and explodes under pytest. The annotations above are
# strings (``from __future__ import annotations``), so they cost nothing at
# import time; ``ok`` and ``bad`` are the only runtime needs, and resolving them
# on call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)


#: Method names that would constitute a resume path. Section 10.5 prohibits
#: persistent conversational memory *by default*, so the absence of a way to
#: reattach is not an accident of the current implementation -- it is the
#: mechanism. A class that grew any of these would have grown the thing the
#: gate forbids, whether or not any caller used it yet.
RESUME_SHAPED_NAMES: tuple[str, ...] = (
    "resume",
    "reopen",
    "reattach",
    "attach",
    "restore",
    "load",
    "rehydrate",
    "continue_session",
    "from_transcript",
    "with_history",
)

#: Constructor parameters that could carry a previous invocation's context in.
HISTORY_SHAPED_PARAMETERS: tuple[str, ...] = (
    "history",
    "messages",
    "transcript",
    "turns",
    "prior_turns",
    "prior_turn_count",
    "previous_session",
    "session_id",
    "memory",
    "context",
)

#: The sentence a previous session says. It exists in this process, in a closed
#: session's lifetime, and must not appear in the next invocation's payload.
PRIOR_TURN_SENTINEL = "SENTINEL-PRIOR-TURN-4f2b7c: the earlier session said this"

#: Things that are true of the project but are not part of this work unit. A
#: session bounded by the project rather than the work unit would carry them.
PROJECT_CONTAMINANTS: dict[str, str] = {
    "sibling_work_unit_instructions": "SENTINEL-SIBLING-9ad13e: implement the unrelated exporter",
    "project_wide_brief": "SENTINEL-PROJECT-0c84fa: the project ships twenty-six work units",
    "another_tasks_input": "SENTINEL-OTHER-INPUT-71bd05: R-999",
}

_ALIAS = "MODEL-A"


# ===========================================================================
# Subjects
# ===========================================================================


def _work_unit() -> WorkUnit:
    """The bounded unit of work both checks hand to a session."""
    return WorkUnit(
        task_id="TSK-D1-05",
        role="implementer",
        instructions="Implement the function described by the failing test.",
        inputs={"requirement_id": "R-014", "failing_test": "tests/unit/test_widget.py::test_area"},
        max_tokens=512,
        system="You are executing one bounded work unit.",
    )


def _session_policy(ctx: GateContext) -> SessionPolicy:
    """The owner's own ``session_policy``, not a value invented here."""
    return load_model_policy(ctx.repo_root / "project-pack" / "model-policy.yaml").session_policy


class SessionThatRemembers(WorkerSession):
    """A session that carries the previous invocation's conversation forward.

    This is the implementation Section 10.5 exists to prohibit, written out so
    the checks can be aimed at it: a process-wide transcript store keyed by task
    and alias, a ``prior_turn_count`` seeded from it, a ``resume`` classmethod,
    and a ``messages()`` that prepends what was said last time. Nothing else
    differs from the real class -- the point of a control is to be wrong in one
    identifiable way.
    """

    _memory: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def __init__(self, work_unit: WorkUnit, *, alias: str, session_policy: Any = None) -> None:
        super().__init__(work_unit, alias=alias, session_policy=session_policy)
        self._key = (work_unit.task_id, alias)
        carried = list(self._memory.get(self._key, []))
        self._turns = carried
        self.prior_turn_count = len(carried)

    @classmethod
    def resume(cls, work_unit: WorkUnit, *, alias: str) -> SessionThatRemembers:
        return cls(work_unit, alias=alias)

    def messages(self) -> list[dict[str, Any]]:
        carried = [
            {"role": turn["role"], "content": turn["content"]}
            for turn in self._memory.get(self._key, [])
        ]
        return carried + super().messages()

    def close(self) -> dict[str, Any]:
        self._memory[self._key] = list(self._turns)
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

    def _assert_open(self) -> None:  # the transcript is reusable, so reuse is allowed
        return


def _forget_control_memory() -> None:
    """Empty the control's process-wide transcript store.

    ``SessionThatRemembers`` keeps transcripts in a class attribute, which is
    what makes it the thing Section 10.5 prohibits. Left dirty between probes it
    would also make one probe's memory into the next one's evidence, so every
    check that uses it clears it on both sides. ``getattr`` because a test may
    substitute a different control to prove the control itself is load-bearing.
    """
    memory = getattr(SessionThatRemembers, "_memory", None)
    if memory is not None:
        memory.clear()


# ===========================================================================
# A1 — every invocation opens a new session with empty prior history
# ===========================================================================


def _fresh_session_findings(
    session_class: type[WorkerSession], work_unit: WorkUnit, policy: SessionPolicy
) -> tuple[list[str], dict[str, Any]]:
    """The A1 predicate, run over whatever session class it is handed.

    Six observations, each of which a memory-carrying implementation fails:

    1. a first invocation records turns and closes;
    2. a second invocation for the *same* task and alias reports
       ``prior_turn_count == 0`` and holds no turns;
    3. the two invocations are distinct sessions;
    4. nothing the first session said reaches the second's payload;
    5. a closed session refuses every further operation rather than quietly
       continuing;
    6. there is no resume path and no constructor parameter that could carry a
       transcript in -- the absence is structural, not circumstantial.

    Then the policy arm: a pack that switched ``fresh_per_invocation_worker_
    sessions`` off must be refused at construction. Section 10.5 makes freshness
    the default, and a default that can be turned off by editing one line of
    owner data is a default nobody is defending.
    """
    findings: list[str] = []
    record: dict[str, Any] = {"session_class": session_class.__name__}

    first = session_class.open(work_unit, alias=_ALIAS, session_policy=policy)
    record["first_invocation"] = {
        "session_id": first.session_id,
        "prior_turn_count_at_open": first.prior_turn_count,
        "turn_count_at_open": first.turn_count,
    }
    first.record_turn("user", "the first invocation's prompt")
    first.record_turn("assistant", PRIOR_TURN_SENTINEL)
    first_summary = first.close()
    record["first_invocation"]["turns_at_close"] = first_summary.get("turns")
    record["first_invocation"]["transcript_retained_after_close"] = first.turn_count

    second = session_class.open(work_unit, alias=_ALIAS, session_policy=policy)
    rendered = json.dumps(second.messages())
    record["second_invocation"] = {
        "session_id": second.session_id,
        "prior_turn_count_at_open": second.prior_turn_count,
        "turn_count_at_open": second.turn_count,
        "distinct_session_id": second.session_id != first.session_id,
        "carries_the_first_sessions_words": PRIOR_TURN_SENTINEL in rendered,
        "message_roles": [message["role"] for message in second.messages()],
    }

    reuse: dict[str, Any] = {}
    for label, call in (
        ("messages", first.messages),
        ("record_turn", lambda: first.record_turn("user", "and another thing")),
        ("close", first.close),
    ):
        try:
            call()
        except SessionReuseError as exc:
            reuse[label] = {"refused": True, "raised": "SessionReuseError", "detail": str(exc)[:200]}
        except Exception as exc:
            reuse[label] = {"refused": False, "raised": type(exc).__name__, "detail": str(exc)[:200]}
        else:
            reuse[label] = {"refused": False, "raised": None, "detail": "the call succeeded"}
    record["closed_session_reuse"] = reuse

    resume_paths = sorted(name for name in RESUME_SHAPED_NAMES if hasattr(session_class, name))
    parameters = sorted(inspect.signature(session_class.__init__).parameters)
    history_parameters = sorted(set(parameters) & set(HISTORY_SHAPED_PARAMETERS))
    record["structure"] = {
        "constructor_parameters": parameters,
        "resume_shaped_attributes": resume_paths,
        "history_shaped_constructor_parameters": history_parameters,
    }

    disabled = replace(policy, fresh_per_invocation_worker_sessions=False)
    try:
        session_class.open(work_unit, alias=_ALIAS, session_policy=disabled)
    except SessionReuseError as exc:
        policy_arm = {"refused": True, "raised": "SessionReuseError", "detail": str(exc)[:200]}
    except Exception as exc:
        policy_arm = {"refused": False, "raised": type(exc).__name__, "detail": str(exc)[:200]}
    else:
        policy_arm = {
            "refused": False,
            "raised": None,
            "detail": "a pack disabling fresh sessions was accepted",
        }
    record["pack_disabling_fresh_sessions"] = policy_arm

    if second.prior_turn_count != 0:
        findings.append(
            f"a new invocation opened with prior_turn_count={second.prior_turn_count}; the "
            "assertion is that it is zero at invocation"
        )
    if second.turn_count != 0:
        findings.append(f"a new invocation already holds {second.turn_count} turn(s)")
    if second.session_id == first.session_id:
        findings.append("the second invocation reused the first session's identity")
    if PRIOR_TURN_SENTINEL in rendered:
        findings.append(
            "the previous invocation's words appear in the new session's payload, which is the "
            "persistent conversational memory Section 10.5 prohibits by default"
        )
    if first.turn_count != 0:
        findings.append(
            f"the closed session still holds {first.turn_count} turn(s); the transcript was not "
            "discarded at close"
        )
    for label, arm in reuse.items():
        if not arm["refused"]:
            findings.append(
                f"a closed session accepted {label}(): {arm['detail']}. Reuse carries a "
                "transcript forward silently, which is exactly what a fresh session prevents"
            )
    if resume_paths:
        findings.append(
            f"the session class exposes a resume path {resume_paths}; freshness that depends on "
            "callers not using an available reattach is not enforced"
        )
    if history_parameters:
        findings.append(
            f"the constructor accepts {history_parameters}, so a caller can seed an invocation "
            "with a previous one's context"
        )
    if not policy_arm["refused"]:
        findings.append(
            "a session_policy with fresh_per_invocation_worker_sessions=false was accepted: "
            f"{policy_arm['detail']}"
        )
    return findings, record


def d1_05_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A1 ``session_history_length_probe`` -- expected ``prior_turn_count == 0 at invocation``.

    ``prior_turn_count == 0`` is the cheapest possible assertion to satisfy: an
    attribute initialised to zero and never touched passes it, and so does a
    class that carries the whole previous conversation in a different field. So
    the probe measures the property the number stands for. It runs two
    invocations of the *same* task under the *same* alias -- the only shape in
    which carry-over could occur -- makes the first one say something
    identifiable, and requires the second to know nothing about it.

    The structural arm is deliberate. Section 10.5 prohibits persistent
    conversational memory by default, and a default is only worth its name if
    turning it off is refused rather than configured: no resume path exists on
    the class, no constructor parameter can carry a transcript in, and a pack
    that sets ``fresh_per_invocation_worker_sessions: false`` is rejected at
    construction instead of quietly honoured.

    The negative control is a session that keeps a process-wide transcript keyed
    by task and alias. It is wrong in one identifiable way, and the same
    predicate must report it.
    """
    work_unit = _work_unit()
    policy = _session_policy(ctx)

    findings, record = _fresh_session_findings(WorkerSession, work_unit, policy)
    _forget_control_memory()
    control_findings, control_record = _fresh_session_findings(
        SessionThatRemembers, work_unit, policy
    )
    _forget_control_memory()

    control_caught = {
        "prior_turns_carried": any("prior_turn_count=" in f for f in control_findings),
        "previous_words_in_the_new_payload": any(
            "previous invocation's words" in f for f in control_findings
        ),
        "transcript_retained_at_close": any("was not discarded at close" in f for f in control_findings),
        "closed_session_reusable": any("a closed session accepted" in f for f in control_findings),
        "resume_path_exists": any("exposes a resume path" in f for f in control_findings),
    }

    logs = {
        "check": a.method or "session_history_length_probe",
        "expected": a.expected,
        "declared_session_policy": {
            "fresh_per_invocation_worker_sessions": policy.fresh_per_invocation_worker_sessions,
            "persistent_model_conversation_memory_default": (
                policy.persistent_model_conversation_memory_default
            ),
            "durable_state_location": policy.durable_state_location,
            "chat_transcript_as_project_memory": policy.chat_transcript_as_project_memory,
        },
        "real_session": record,
        "negative_control": {
            "probe": (
                "a WorkerSession subclass that stores each transcript in a process-wide map "
                "keyed by (task_id, alias) and reopens it on the next invocation"
            ),
            "why": (
                "prior_turn_count == 0 is satisfied by any class that never sets it, including "
                "one that carries the whole previous conversation elsewhere. This arm makes the "
                "carry-over real and requires the same predicate to name it."
            ),
            "observations": control_record,
            "detector_findings": control_findings,
            "detector_caught": control_caught,
            "detector_fires": all(control_caught.values()),
        },
    }
    evidence = {
        "session_initialization_logs": logs,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "work_unit_input_hash": work_unit.input_hash,
            "transcript_hash": content_hash(logs),
        },
    }

    all_findings = list(findings)
    if not policy.fresh_per_invocation_worker_sessions:
        all_findings.append(
            "the pack's session_policy no longer requires fresh per-invocation worker sessions"
        )
    if policy.persistent_model_conversation_memory_default:
        all_findings.append(
            "the pack now defaults persistent model conversation memory on, which Section 10.5 "
            "prohibits by default"
        )
    missed = sorted(name for name, caught in control_caught.items() if not caught)
    if missed:
        all_findings.append(
            f"negative control did not fire for {missed}: a session that reopens the previous "
            f"transcript produced only {control_findings}"
        )
    if all_findings:
        return bad(all_findings, evidence)
    return ok(
        evidence,
        (
            "two invocations of one task under one alias open distinct sessions, the second at "
            "prior_turn_count 0 with nothing the first said in its payload; the closed session "
            "refuses messages(), record_turn() and close(); the class exposes no resume path and "
            "no history parameter; and a pack disabling fresh sessions is refused"
        ),
    )


# ===========================================================================
# A3 — worker context is bounded by the work unit, not by the project
# ===========================================================================


def _payload_findings(
    session: WorkerSession, work_unit: WorkUnit, contaminants: dict[str, str]
) -> tuple[list[str], dict[str, Any]]:
    """The A3 predicate: account for every character of the payload.

    Absence testing alone is weak -- searching a payload for three sentinel
    strings proves that those three strings are absent, not that the payload is
    bounded. So this works the other way round: every fragment the work unit
    itself authorises is removed from the rendered payload one occurrence at a
    time, and what remains must be nothing but the assembly scaffolding.
    Anything left over came from somewhere the work unit does not name, whatever
    it happens to say.

    The sentinel search is kept as well, because a residue check cannot say
    *what* leaked and a named contaminant can.
    """
    findings: list[str] = []
    messages = session.messages()
    rendered = "\n".join(str(message.get("content", "")) for message in messages)

    authorised: list[str] = []
    if work_unit.system:
        authorised.append(work_unit.system)
    authorised.append(work_unit.instructions)
    if work_unit.inputs:
        authorised.append("Work-unit inputs:")
        authorised.extend(f"{key}: {work_unit.inputs[key]}" for key in sorted(work_unit.inputs))

    residue = rendered
    for fragment in sorted(authorised, key=len, reverse=True):
        residue = residue.replace(fragment, "", 1)
    # What the assembler is allowed to add between authorised fragments: list
    # bullets, separators and whitespace. Everything else is unaccounted for.
    leftover = residue.replace("-", "").replace(":", "").strip()

    leaked = sorted(name for name, text in contaminants.items() if text in rendered)
    roles = [str(message.get("role")) for message in messages]
    unexpected_roles = sorted(set(roles) - {"system", "user"})

    record = {
        "messages": messages,
        "message_roles": roles,
        "authorised_fragments": authorised,
        "unaccounted_characters": len(leftover),
        "unaccounted_residue": leftover[:400],
        "contaminants_present": leaked,
        "unexpected_roles": unexpected_roles,
        "payload_scope": "work_unit_inputs_only" if not leftover and not leaked else "wider",
    }

    if not messages:
        findings.append("the session built no payload at all, so 'bounded' is vacuous")
    if leftover:
        findings.append(
            f"{len(leftover)} characters of the payload are not accounted for by any field of the "
            f"work unit: {leftover[:200]!r}"
        )
    findings.extend(
        f"the payload carries {name}, which belongs to the project and not to this work unit"
        for name in leaked
    )
    if unexpected_roles:
        findings.append(
            f"the payload carries {unexpected_roles} turns; a bounded invocation sends the work "
            "unit's system prompt and its instruction, not a conversation"
        )
    return findings, record


def d1_05_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A3 ``context_payload_audit`` -- expected ``payload_scope == work_unit_inputs_only``.

    The contaminants are not hypothetical strings. Before the payload is built,
    a previous session for the same task and alias records one of them as a turn
    and closes; the others stand for a sibling work unit and a project-wide
    brief. All three exist in this process at the moment the payload is
    assembled, so a session that reached beyond its work unit had somewhere to
    reach.

    The verdict, though, does not rest on their absence. It rests on the residue:
    every fragment the work unit authorises is subtracted from the rendered
    payload, and what remains must be assembly scaffolding and nothing else. That
    is the difference between "the three strings I thought of are missing" and
    "there is nothing here the work unit did not put here".

    The negative control appends the previous transcript to the payload and must
    be caught by the same function -- both by name, because the sentinel is
    found, and by arithmetic, because the residue is no longer empty.
    """
    work_unit = _work_unit()
    policy = _session_policy(ctx)
    contaminants = dict(PROJECT_CONTAMINANTS)
    contaminants["previous_session_transcript"] = PRIOR_TURN_SENTINEL

    # A real earlier invocation of the same task, so the transcript a leaking
    # implementation would find actually exists.
    _forget_control_memory()
    earlier = SessionThatRemembers.open(work_unit, alias=_ALIAS)
    earlier.record_turn("assistant", PRIOR_TURN_SENTINEL)
    earlier.close()

    session = WorkerSession.open(work_unit, alias=_ALIAS, session_policy=policy)
    findings, record = _payload_findings(session, work_unit, contaminants)

    leaking = SessionThatRemembers.open(work_unit, alias=_ALIAS)
    control_findings, control_record = _payload_findings(leaking, work_unit, contaminants)
    _forget_control_memory()

    control_caught = {
        "residue_detected": any("not accounted for by any field" in f for f in control_findings),
        "named_contaminant_detected": any("belongs to the project" in f for f in control_findings),
        "conversation_roles_detected": any("not a conversation" in f for f in control_findings),
    }

    sample = {
        "check": a.method or "context_payload_audit",
        "expected": a.expected,
        "work_unit": work_unit.as_body(),
        "work_unit_input_hash": work_unit.input_hash,
        "payload": record,
        "contaminants_in_scope_at_build_time": {
            name: {"text": text, "where_it_lives": _contaminant_origin(name)}
            for name, text in contaminants.items()
        },
        "how_the_bound_is_measured": (
            "every fragment the work unit authorises -- its system prompt, its instructions, the "
            "inputs header and one line per sorted input -- is removed from the rendered payload "
            "once each. What survives, after list bullets and separators, must be empty. Absence "
            "of named sentinels is reported too, but it is the weaker half: it can only find "
            "what somebody thought to look for."
        ),
        "negative_control": {
            "probe": (
                "the same predicate over a session that prepends the previous invocation's "
                "transcript to the payload"
            ),
            "why": (
                "a payload audit that only searches for sentinels passes against any leak nobody "
                "anticipated, and an audit run against a session that never had anything to leak "
                "proves nothing at all. This arm gives it something to find."
            ),
            "observations": control_record,
            "detector_findings": control_findings,
            "detector_caught": control_caught,
            "detector_fires": all(control_caught.values()),
        },
    }
    evidence = {
        "context_payload_sample": sample,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "work_unit_input_hash": work_unit.input_hash,
            "transcript_hash": content_hash(sample),
        },
    }

    all_findings = list(findings)
    if record["payload_scope"] != "work_unit_inputs_only":
        all_findings.append(f"payload scope measured as {record['payload_scope']!r}")
    missed = sorted(name for name, caught in control_caught.items() if not caught)
    if missed:
        all_findings.append(
            f"negative control did not fire for {missed}: a session that prepends the previous "
            f"transcript produced only {control_findings}"
        )
    if all_findings:
        return bad(all_findings, evidence)
    return ok(
        evidence,
        (
            f"the payload reduces to nothing once the work unit's own fields are subtracted, and "
            f"none of the {len(contaminants)} project-scope strings in play at build time appears "
            "in it; the same audit catches a session that prepends the previous transcript"
        ),
    )


def _contaminant_origin(name: str) -> str:
    return {
        "previous_session_transcript": (
            "recorded as a turn by an earlier session for this same task and alias, then closed"
        ),
        "sibling_work_unit_instructions": "another work unit in the same project",
        "project_wide_brief": "project-level context no single work unit owns",
        "another_tasks_input": "an input belonging to a different task",
    }.get(name, "project scope")


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
#:
#: A2 (``egress_inspection``) and A4 (``post_session_state_query``) are
#: deliberately absent: the first needs an egress record this build does not
#: produce, and the second needs a live TerminusDB. Both have a visible half that
#: would go green on its own, which is precisely why neither is registered --
#: passing the half that is reachable would report the whole assertion decided.
CHECKS_D1_05: dict[tuple[str, str], Check] = {
    ("GATE-D1-05", "A1"): d1_05_a1,
    ("GATE-D1-05", "A3"): d1_05_a3,
}
