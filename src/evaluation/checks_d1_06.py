"""GATE-D1-06 — blinded model identity.

Contract Sections 11.2 and 12.3. Five assertions, all five executable against
the real subjects rather than against a description of them:

    A1 no agent-visible payload contains a vendor or model name
    A2 task and audit records reference aliases, not real identities
    A3 the real mapping lives in the separate protected database
    A4 the owner audit path can reveal the mapping, and only it can
    A5 no agent receives another agent's prestige ranking or cost tier

Four rules shaped every check in this module.

**The payload scanned is the payload that ships.**
:mod:`workers.adapters.litellm_worker` builds ``{"messages": session.messages(),
"routing": decision.as_body()}`` and hands exactly that object to
:func:`models.blinding.assert_task_payload_blinded` before it dispatches. A1
rebuilds that object for every mapped role, through the real router and a real
:class:`~workers.session.WorkerSession`, and additionally reads the adapter's
own source to confirm the blinding call still stands *before* the gateway call.
A scan of a payload nobody sends would be a green about this file.

**A scanner that rejects everything blinds nothing.** Every check here carries
both arms. The clean subject must pass -- and the clean prompts deliberately
contain ``code``, ``max`` and ``flash``, which are fragments of real model ids
and also ordinary English in a software-engineering harness -- while an injected
leak must be caught, and caught for the reason the assertion names.

**Nothing revealed is written down.** A4 exercises the owner reveal against the
protected instance and never records the provider or model id it gets back. It
records that the reveal *matched the pack's mapping*, by comparing content
hashes, so the evidence proves the mapping resolves without becoming the leak
the gate exists to prevent. The artifact is named
``owner_reveal_transcript_redacted`` for that reason.

**The protected instance is not assumed.** A4's live half needs the owner's
protected credential and a reachable instance. Where either is absent the check
reports ``UNVERIFIABLE`` for that half with the offline half's results attached.
It does not report PASS: Section 14.4 requires services to be exercised with
evidence, and "the credential was not present" is not evidence that a reveal
works.

This module holds no route to the protected instance. It imports the endpoint
and database constants from :mod:`integrations.protected_identity`, which is the
one module Section 11.2 permits to hold them -- writing the port here as a
literal would trip the architecture scan that keeps that boundary honest.
"""

from __future__ import annotations

import ast
import dataclasses
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from api.context import (
    ALIAS_SCOPES,
    IdentityKind,
    Principal,
    RequestContext,
    reset_context,
    set_context,
)
from api.middleware.audit import REDACTED_HEADERS, AuditMiddleware
from evaluation.async_bridge import run_sync
from governance.envelope import Envelope, content_hash
from integrations.protected_identity import (
    PROTECTED_DATABASE,
    PROTECTED_ENDPOINT,
    AliasView,
    OwnerAuditRequest,
    ProtectedIdentityAccessError,
    ProtectedIdentityStore,
    probe_credential_against_protected,
    protected_store_from_env,
)
from integrations.secrets import MissingRequiredCredential
from integrations.terminusdb import DEFAULT_ENDPOINT, TerminusAuthError, TerminusError
from models.blinding import (
    FORBIDDEN_PAYLOAD_KEYS,
    PackIdentityStore,
    assert_task_payload_blinded,
    scan_task_payload,
)
from models.errors import BlindingViolationError, ProtectedAccessError
from models.policy import ModelPolicy, load_model_policy
from models.router import ModelRouter, RoutingDecision, RoutingRequest
from observability.identity import (
    RANKING_FIELDS,
    ProtectedIdentityLeak,
    assert_alias_only,
    scan_for_leaks,
)
from ontology.schema import TaskEvent, TaskEventType
from owner_surface.gateway import DEFAULT_DATABASE
from tasks.ledger import EVENT_RESULTING_STATE, Actor, ActorKind, TaskLedger
from workers.session import WorkerSession, WorkUnit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext
    from evaluation.gate_spec import AssertionSpec, GateSpec


# ``checks.py`` imports this module to register its entries, so importing
# ``checks`` back at module scope would make the pair circular -- and which side
# breaks then depends on which module Python loads first, so the same code would
# work under the gate runner and explode under pytest. The annotations above are
# strings (``from __future__ import annotations``) and cost nothing at import
# time; the three constructors are the only runtime need, and resolving them on
# call keeps the cycle from ever forming.
def ok(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import ok as _ok

    return _ok(*args, **kwargs)


def bad(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import bad as _bad

    return _bad(*args, **kwargs)


def undecided(*args: Any, **kwargs: Any) -> AssertionOutcome:
    from evaluation.checks import undecided as _undecided

    return _undecided(*args, **kwargs)


#: Ordinary engineering English that happens to contain fragments of real model
#: identifiers. A clean payload that avoided these words would prove only that
#: the probe was careful; passing *with* them is what shows the scanner
#: distinguishes a model id from the language the harness actually speaks.
_NEAR_MISS_INSTRUCTIONS = (
    "Refactor the code so the failing test passes. Cap max retries at two, "
    "flash the firmware only after the gate is green, and keep the command "
    "surface small."
)

#: The alias the harness's own orchestrator writes ledger events under. It is a
#: harness identity, not a model: no row in ``model-policy.yaml`` maps to it, and
#: it is held to the same alias shape as every model alias.
_ORCHESTRATOR_ALIAS = "orchestrator-s01"

#: What the owner audit path calls itself when this gate exercises it. The
#: reveal runs with the owner's protected credential, so the record names the
#: audit path rather than impersonating the owner personally.
_OWNER_AUDIT_IDENTITY = "owner_audit_path"

#: Fields that would let one agent rank another: the union of the two lists this
#: harness enforces -- the dispatch boundary's forbidden keys
#: (:data:`models.blinding.FORBIDDEN_PAYLOAD_KEYS`) and the record surface's
#: ranking fields (:data:`observability.identity.RANKING_FIELDS`). Neither is a
#: superset of the other, and A5 reports which boundary catches each rather than
#: blurring the two into one claim.
_RANKING_AND_IDENTITY_FIELDS: frozenset[str] = RANKING_FIELDS | FORBIDDEN_PAYLOAD_KEYS

_ADAPTER_PATH = Path("src") / "workers" / "adapters" / "litellm_worker.py"


# ===========================================================================
# Shared subjects
# ===========================================================================


def _policy() -> ModelPolicy:
    return load_model_policy()


def _router(policy: ModelPolicy) -> ModelRouter:
    """A real router over the real pack.

    ``availability_probe_required=False`` is set on the *request*, not on the
    policy: this gate never dispatches, so blinding must not depend on whether a
    model answered a probe today. Recording a fabricated ``ModelCapability`` to
    satisfy the router would be the dishonest alternative, so the request
    declines the requirement instead of inventing the measurement.
    """
    return ModelRouter(policy=policy)


def _worker_payload(
    router: ModelRouter, policy: ModelPolicy, role: str
) -> tuple[RoutingDecision, dict[str, Any]]:
    """The exact object :mod:`workers.adapters.litellm_worker` gates on."""
    decision = router.route(RoutingRequest(role=role, availability_probe_required=False))
    work_unit = WorkUnit(
        task_id=f"TSK-D1-06-{role}",
        role=role,
        instructions=_NEAR_MISS_INSTRUCTIONS,
        inputs={
            "requirement_id": "REQ-D1-06",
            "acceptance": "deterministic oracle, no judge in the verdict path",
        },
    )
    session = WorkerSession.open(
        work_unit, alias=decision.alias, session_policy=policy.session_policy
    )
    return decision, {"messages": session.messages(), "routing": decision.as_body()}


def _scan(subject: Any, policy: ModelPolicy) -> dict[str, list[str]]:
    """Both scanners the harness enforces, reported separately.

    ``scan_task_payload`` is the dispatch boundary's: forbidden field names,
    vendor words, and complete real model identifiers. ``scan_for_leaks`` in
    strict mode is the record and telemetry surface's: ranking field *names*
    plus any vendor mention at all. Merging them into one verdict would hide
    which boundary is doing the work, so they are kept apart everywhere.
    """
    return {
        "dispatch_boundary": scan_task_payload(subject, policy),
        "record_surface": [
            f"{path}: {matched}" for path, matched in scan_for_leaks(subject, strict=True)
        ],
    }


def _findings_of(scan: dict[str, list[str]]) -> list[str]:
    return [f"{surface}: {finding}" for surface, hits in scan.items() for finding in hits]


def _rejected(payload: Any, policy: ModelPolicy) -> dict[str, Any]:
    """Run the real gate function and record whether -- and why -- it refused."""
    try:
        assert_task_payload_blinded(payload, policy)
    except BlindingViolationError as exc:
        detail = exc.detail if isinstance(exc.detail, list) else [exc.detail]
        return {
            "rejected": True,
            "raised": type(exc).__name__,
            "typed_state": str(exc.typed_state),
            "detail": [str(item) for item in detail][:6],
        }
    return {"rejected": False, "raised": None, "typed_state": None, "detail": []}


def _binding(ctx: GateContext, transcript: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_commit": ctx.binding.commit_sha,
        "contract_version": ctx.binding.contract_version,
        "transcript_hash": content_hash(transcript),
    }


def _ranking_keys(payload: Any) -> set[str]:
    """Every ranking or identity field name present anywhere in *payload*."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in _RANKING_AND_IDENTITY_FIELDS:
                found.add(str(key))
            found |= _ranking_keys(value)
    elif isinstance(payload, list | tuple):
        for value in payload:
            found |= _ranking_keys(value)
    return found


# ---------------------------------------------------------------------------
# The enforcement point, read from the adapter's own source
# ---------------------------------------------------------------------------


def _enforcement_point(repo_root: Path) -> dict[str, Any]:
    """Confirm the blinding call still guards the dispatch, in source order.

    A1 would otherwise prove that *this file* can construct a clean payload.
    What matters is that the adapter cannot send one it has not scanned, so the
    adapter's ``execute`` is parsed and three facts are read off it: the
    blinding call is present, the object it scans carries both the messages and
    the routing decision, and it stands before the gateway call. Source order is
    a weaker claim than a traced control flow, and the evidence says so rather
    than letting a reader infer more.
    """
    path = repo_root / _ADAPTER_PATH
    facts: dict[str, Any] = {
        "adapter": _ADAPTER_PATH.as_posix(),
        "found": path.is_file(),
        "blinding_call_line": None,
        "gateway_call_line": None,
        "scanned_keys": [],
        "blinding_precedes_dispatch": False,
        "how_this_is_read": (
            "static source order inside LiteLLMWorkerAdapter.execute, not a traced call. It "
            "shows the guard is written ahead of the dispatch, not that no branch skips it."
        ),
    }
    if not path.is_file():
        return facts

    tree = ast.parse(path.read_text(), filename=str(path))
    execute = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "execute"
        ),
        None,
    )
    if execute is None:
        return facts

    for node in ast.walk(execute):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "assert_task_payload_blinded":
            facts["blinding_call_line"] = node.lineno
            for argument in node.args:
                if isinstance(argument, ast.Dict):
                    facts["scanned_keys"] = sorted(
                        str(key.value)
                        for key in argument.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
        if isinstance(func, ast.Attribute) and func.attr == "chat_completion":
            facts["gateway_call_line"] = node.lineno

    if facts["blinding_call_line"] and facts["gateway_call_line"]:
        facts["blinding_precedes_dispatch"] = (
            facts["blinding_call_line"] < facts["gateway_call_line"]
        )
    return facts


# ===========================================================================
# A1 — no agent-visible payload contains a vendor or model name
# ===========================================================================


def d1_06_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A1 ``payload_scan_for_vendor_strings`` -- expected ``zero_matches``.

    Every role ``model-policy.yaml`` maps gets a real routing decision and a
    real worker session, and the object the adapter would send is scanned by the
    function the adapter calls. Four injected leaks -- a complete model id, a
    bare vendor word, a family field, and a forbidden field name -- must each be
    refused, or "zero matches" would be a statement about the probe rather than
    about the boundary.
    """
    policy = _policy()
    router = _router(policy)

    scanned: dict[str, Any] = {}
    findings: list[str] = []
    for role in sorted(policy.roles):
        decision, payload = _worker_payload(router, policy, role)
        scan = _scan(payload, policy)
        gate_result = _rejected(payload, policy)
        scanned[role] = {
            "alias": decision.alias,
            "gateway": decision.gateway,
            "payload_keys": sorted(payload),
            "dispatch_boundary_findings": scan["dispatch_boundary"],
            "record_surface_findings": scan["record_surface"],
            "accepted_by_assert_task_payload_blinded": not gate_result["rejected"],
            "payload_hash": content_hash(payload),
        }
        findings.extend(f"{role} ({decision.alias}): {f}" for f in _findings_of(scan))
        if gate_result["rejected"]:
            findings.append(
                f"{role}: a clean payload was refused by assert_task_payload_blinded "
                f"({gate_result['detail']}); a scanner that rejects everything blinds nothing"
            )

    enforcement = _enforcement_point(ctx.repo_root)
    if not enforcement["found"]:
        findings.append(f"the worker adapter is not at {_ADAPTER_PATH.as_posix()}")
    else:
        if enforcement["blinding_call_line"] is None:
            findings.append(
                "the worker adapter's execute() does not call assert_task_payload_blinded, so "
                "the payload scanned here is not the payload that ships"
            )
        elif not enforcement["blinding_precedes_dispatch"]:
            findings.append(
                "assert_task_payload_blinded does not precede the gateway call in the adapter, "
                "so an unscanned payload can reach the wire"
            )
        missing_keys = sorted({"messages", "routing"} - set(enforcement["scanned_keys"]))
        if enforcement["blinding_call_line"] is not None and missing_keys:
            findings.append(
                f"the adapter scans {enforcement['scanned_keys']}, omitting {missing_keys}"
            )

    # Negative controls, injected into a real payload for a real role. Each is a
    # different route by which a vendor identity reaches a worker, and each must
    # be caught by the function the adapter calls -- not by a copy of it here.
    _, victim = _worker_payload(router, policy, "implementer")
    implementer_row = policy.role("implementer")
    controls: dict[str, Any] = {}
    injections = {
        "complete_real_model_id_in_the_prompt": (
            "messages",
            f"\n(you are running on {implementer_row.litellm_model})",
        ),
        "bare_vendor_word_in_the_prompt": ("messages", " Ask the Anthropic model to confirm."),
        "vendor_family_on_the_routing_decision": ("routing", ("family", implementer_row.family)),
        "identity_field_name_on_the_routing_decision": ("routing", ("model", "redacted")),
    }
    for label, (target, mutation) in injections.items():
        probe = {
            "messages": [dict(message) for message in victim["messages"]],
            "routing": dict(victim["routing"]),
        }
        if target == "messages":
            probe["messages"][-1]["content"] += mutation
        else:
            key, value = mutation
            probe["routing"][key] = value
        outcome = _rejected(probe, policy)
        scan = _scan(probe, policy)
        controls[label] = {
            **outcome,
            "detected": outcome["rejected"],
            "dispatch_boundary_findings": scan["dispatch_boundary"],
        }
        if not outcome["rejected"]:
            findings.append(
                f"negative control did not fire: {label} passed assert_task_payload_blinded"
            )

    report = {
        "check": a.method or "payload_scan_for_vendor_strings",
        "expected": a.expected,
        "assertion": a.assertion_id,
        "what_was_scanned": (
            "the worker-facing object itself -- {'messages': WorkerSession.messages(), "
            "'routing': RoutingDecision.as_body()} -- built through the real router and a real "
            "session for every role model-policy.yaml maps"
        ),
        "roles_scanned": len(scanned),
        "total_matches": sum(
            len(record["dispatch_boundary_findings"]) + len(record["record_surface_findings"])
            for record in scanned.values()
        ),
        "per_role": scanned,
        "enforcement_point": enforcement,
        "clean_prompt_contains_near_miss_words": ["code", "max", "flash"],
        "negative_control": {
            "probe": (
                "inject a complete real model id, a bare vendor word, a vendor family field and "
                "a forbidden identity field name into a real implementer payload"
            ),
            "why": (
                "zero matches is also what a scanner that matches nothing reports. Each arm has "
                "to be refused by assert_task_payload_blinded -- the function the adapter calls "
                "-- for the leak it carries."
            ),
            "attempts": controls,
        },
        "binding": _binding(ctx, {"per_role": scanned, "controls": controls}),
    }
    evidence = {"payload_scan_report": report}
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"zero vendor or model matches across {len(scanned)} mapped roles' worker-facing "
        "payloads, while four injected leaks were each refused by the adapter's own gate",
    )


# ===========================================================================
# A2 — task and audit records reference aliases, not real identities
# ===========================================================================


def _event(
    *,
    task: str,
    sequence: int,
    event_type: TaskEventType,
    actor: Actor,
    state: Any,
    recorded_at: datetime,
    payload: dict[str, Any] | None = None,
) -> TaskEvent:
    """One ledger event, built as :meth:`TaskLedger.append` builds it.

    The Section 9.3 authority rule is not re-implemented here: the real
    :meth:`TaskLedger.check_authority` decides every event below, so an actor
    that may not author one is refused exactly as the ledger would refuse it.
    What is *not* exercised is the TerminusDB write, and the evidence says so.
    """
    TaskLedger.check_authority(event_type, actor, state)
    return TaskEvent(
        entity_id=f"EV-{task.split('/', 1)[-1]}-{sequence:06d}",
        envelope=Envelope(schema_id="efah.task_event", created_by_alias=actor.alias),
        task=task,
        sequence=sequence,
        event_type=event_type,
        actor_alias=actor.alias,
        actor_role=actor.role,
        recorded_at=recorded_at,
        resulting_state=state,
        payload=dict(payload or {}),
    )


def _synthesised_stream(policy: ModelPolicy) -> list[TaskEvent]:
    """A task's life from creation to a gate verdict, in ledger events."""
    task = "Task/TSK-D1-06"
    system = Actor(alias=_ORCHESTRATOR_ALIAS, role="orchestrator", kind=ActorKind.SYSTEM)
    worker = Actor(
        alias=policy.role("implementer").alias, role="implementer", kind=ActorKind.WORKER
    )
    gate_actor = Actor(
        alias=policy.role("release_verifier").alias,
        role="release_verifier",
        kind=ActorKind.GATE,
    )
    script: list[tuple[TaskEventType, Actor, dict[str, Any]]] = [
        (TaskEventType.TaskCreated, system, {"requirement_ids": ["REQ-D1-06"]}),
        (TaskEventType.TaskReady, system, {}),
        (TaskEventType.TaskAssigned, system, {"assigned_alias": worker.alias}),
        (TaskEventType.WorkerStarted, worker, {"session_id": "sess-d1-06"}),
        (
            TaskEventType.ArtifactSubmitted,
            worker,
            {"artifact_hashes": {"candidate": content_hash({"probe": "d1-06"})}},
        ),
        (TaskEventType.EvaluationStarted, gate_actor, {"gate_id": "GATE-D1-06"}),
        (TaskEventType.GatePassed, gate_actor, {"gate_id": "GATE-D1-06", "verdict": "PASS"}),
    ]
    base = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    return [
        _event(
            task=task,
            sequence=index,
            event_type=event_type,
            actor=actor,
            state=EVENT_RESULTING_STATE[event_type],
            payload=payload,
            recorded_at=base.replace(minute=index),
        )
        for index, (event_type, actor, payload) in enumerate(script, start=1)
    ]


def _audit_record(alias: str) -> dict[str, Any]:
    """A real audit record, produced by the real middleware.

    :class:`~api.middleware.audit.AuditMiddleware` is driven directly with an
    ASGI scope and a request context rather than through a running app: the
    record it writes is the object under test, and standing the whole stack up
    to obtain one would make this check depend on the graph being reachable.
    """
    from starlette.requests import Request
    from starlette.responses import Response

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/tasks/TSK-D1-06/events",
        "raw_path": b"/v1/tasks/TSK-D1-06/events",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer a-worker-credential"),
            (b"x-efah-alias", alias.encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }
    middleware = AuditMiddleware(app=None)
    principal = Principal(
        kind=IdentityKind.ALIAS,
        subject=f"agent:{alias}",
        scopes=ALIAS_SCOPES,
        alias=alias,
    )
    context = RequestContext(
        correlation_id="corr-d1-06",
        request_id="req-d1-06",
        principal=principal,
        contract_id="EFAH-CONTRACT-001",
        contract_version="1.1",
        project_id="PRJ-EFAH",
        provenance={"actor_alias": alias, "body_content_hash": content_hash({"probe": "d1-06"})},
    )

    async def drive() -> dict[str, Any]:
        async def call_next(_request: Any) -> Any:
            return Response(status_code=202)

        token = set_context(context)
        try:
            await middleware.dispatch(Request(scope), call_next)
        finally:
            reset_context(token)
        records = middleware.sink.records()
        return records[-1] if records else {}

    return run_sync(drive())


def d1_06_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A2 ``record_field_scan`` -- expected ``alias_only_in_task_records``.

    Two record kinds, because the assertion names both and they fail
    differently. A ledger event has no validator on ``actor_alias``, so a forged
    one is caught only by the scan; an audit record's identity comes from a
    :class:`~api.context.Principal`, which refuses to exist with a real model id
    in it. Both mechanisms are exercised, and the clean arm of each must pass.
    """
    policy = _policy()
    mapped_aliases = {row.alias for row in policy.roles.values()}
    findings: list[str] = []

    stream = _synthesised_stream(policy)
    events = [event.model_dump(mode="json") for event in stream]
    stream_scan = _scan(events, policy)
    findings.extend(f"task event stream: {f}" for f in _findings_of(stream_scan))

    alias_fields: dict[str, Any] = {}
    for event in stream:
        record: dict[str, Any] = {
            "sequence": event.sequence,
            "event_type": str(event.event_type),
            "actor_alias": event.actor_alias,
            "envelope_created_by_alias": event.envelope.created_by_alias,
            "is_a_mapped_model_alias": event.actor_alias in mapped_aliases,
        }
        for field_name, value in (
            ("actor_alias", event.actor_alias),
            ("envelope.created_by_alias", event.envelope.created_by_alias),
        ):
            try:
                assert_alias_only(value, field=field_name)
                record[f"{field_name}_alias_shaped"] = True
            except ProtectedIdentityLeak as exc:
                record[f"{field_name}_alias_shaped"] = False
                findings.append(f"event {event.sequence}: {exc}")
        if event.actor_alias != event.envelope.created_by_alias:
            findings.append(
                f"event {event.sequence}: actor_alias {event.actor_alias!r} and envelope "
                f"created_by_alias {event.envelope.created_by_alias!r} disagree"
            )
        alias_fields[event.entity_id] = record

    audit = _audit_record(policy.role("implementer").alias)
    audit_scan = _scan(audit, policy)
    findings.extend(f"audit record: {f}" for f in _findings_of(audit_scan))
    if not audit:
        findings.append("the audit middleware produced no record for a completed request")
    if audit.get("alias") not in mapped_aliases:
        findings.append(
            f"the audit record's alias {audit.get('alias')!r} is not one of the pack's aliases"
        )
    redacted_present = sorted(set(audit.get("headers_present") or []) & REDACTED_HEADERS)
    if redacted_present:
        findings.append(f"the audit record names redacted headers: {redacted_present}")

    # Negative controls: four ways a real identity reaches a record, each caught
    # by a different mechanism.
    row = policy.role("implementer")
    forged_actor = _event(
        task="Task/TSK-D1-06",
        sequence=99,
        event_type=TaskEventType.WorkerStarted,
        actor=Actor(alias=row.litellm_model, role="implementer", kind=ActorKind.WORKER),
        state=None,
        recorded_at=datetime(2026, 8, 2, 13, 0, tzinfo=UTC),
    ).model_dump(mode="json")
    forged_payload = _event(
        task="Task/TSK-D1-06",
        sequence=98,
        event_type=TaskEventType.ToolCallRecorded,
        actor=Actor(alias=row.alias, role="implementer", kind=ActorKind.WORKER),
        state=None,
        recorded_at=datetime(2026, 8, 2, 13, 1, tzinfo=UTC),
        payload={"model": row.litellm_model, "cost_tier": row.tier},
    ).model_dump(mode="json")
    leaking_audit = {**audit, "alias": row.litellm_model, "vendor": row.family}

    controls: dict[str, Any] = {}
    for label, subject in (
        ("real_model_id_as_the_actor_alias", forged_actor),
        ("real_model_id_and_cost_tier_in_an_event_payload", forged_payload),
        ("real_model_id_in_an_audit_record", leaking_audit),
    ):
        scan = _scan(subject, policy)
        detected = bool(scan["dispatch_boundary"] or scan["record_surface"])
        controls[label] = {"detected": detected, **scan}
        if not detected:
            findings.append(f"negative control did not fire: {label} scanned clean")

    try:
        Principal(
            kind=IdentityKind.ALIAS,
            subject=f"agent:{row.litellm_model}",
            scopes=ALIAS_SCOPES,
            alias=row.litellm_model,
        )
        controls["principal_constructed_with_a_real_model_id"] = {"refused": False}
        findings.append(
            "negative control did not fire: a Principal was constructed with a real model id as "
            "its alias, so every audit record it touched would carry a vendor identity"
        )
    except ProtectedIdentityLeak as exc:
        controls["principal_constructed_with_a_real_model_id"] = {
            "refused": True,
            "raised": type(exc).__name__,
            "field": exc.field,
            "matched": exc.matched,
        }

    report = {
        "check": a.method or "record_field_scan",
        "expected": a.expected,
        "assertion": a.assertion_id,
        "task_event_stream": {
            "task": "Task/TSK-D1-06",
            "events": len(stream),
            "event_types": [str(event.event_type) for event in stream],
            "alias_fields": alias_fields,
            "mapped_aliases_referenced": sorted({e.actor_alias for e in stream} & mapped_aliases),
            "harness_identities_referenced": sorted(
                {e.actor_alias for e in stream} - mapped_aliases
            ),
            "dispatch_boundary_findings": stream_scan["dispatch_boundary"],
            "record_surface_findings": stream_scan["record_surface"],
            "stream_hash": content_hash(events),
        },
        "audit_record": {
            "produced_by": "api.middleware.audit.AuditMiddleware.dispatch",
            "record": audit,
            "dispatch_boundary_findings": audit_scan["dispatch_boundary"],
            "record_surface_findings": audit_scan["record_surface"],
            "redacted_headers_absent": not redacted_present,
        },
        "how_the_stream_is_produced": (
            "TaskEvent objects built with the fields TaskLedger.append fills, each decided by "
            "the real TaskLedger.check_authority. No TerminusDB write occurs, so what is proven "
            "is that the record shape carries aliases only -- not that a stored event does."
        ),
        "negative_control": {
            "probe": (
                "a forged event whose actor_alias is a real model id, an event payload carrying "
                "a model id and a cost tier, an audit record with a vendor field, and a "
                "Principal constructed with a real model id"
            ),
            "why": (
                "'records reference aliases' is satisfied by any record set nobody looked at. "
                "Each arm puts a real identity where an alias belongs: the scan must find it, "
                "and the Principal must refuse to exist."
            ),
            "attempts": controls,
        },
        "binding": _binding(ctx, {"events": events, "audit": audit, "controls": controls}),
    }
    evidence = {"record_scan_report": report}
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"{len(stream)} ledger events and one middleware-produced audit record reference aliases "
        "only, while four forged identities were each caught",
    )


# ===========================================================================
# A3 — the mapping lives in the separate protected database
# ===========================================================================


def _locations_distinct(
    *, mapping_database: str, mapping_endpoint: str, main_database: str, main_endpoint: str
) -> dict[str, bool]:
    """The predicate A3 decides on, run over whatever pair it is handed.

    Both arms of A3 call this one function: the real constants, and a control
    that hands it the main location twice. A control that exercised a different
    comparison would prove nothing about the verdict.
    """
    distinct_database = mapping_database != main_database
    distinct_endpoint = mapping_endpoint != main_endpoint
    return {
        "distinct_database": distinct_database,
        "distinct_endpoint": distinct_endpoint,
        "separated": distinct_database and distinct_endpoint,
    }


def d1_06_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A3 ``database_location_assert`` -- expected ``mapping_db != main_project_db``.

    Three layers, because any one alone is weak. The code constants must
    separate; the owner's ``environments.yaml`` must declare the same
    separation, so the gate is measuring the location that was deployed rather
    than one this file believes in; and where the instance is reachable the main
    builder credential must actually be refused by it, because a separation that
    exists only in YAML is a plan.
    """
    findings: list[str] = []

    measured = _locations_distinct(
        mapping_database=PROTECTED_DATABASE,
        mapping_endpoint=PROTECTED_ENDPOINT,
        main_database=DEFAULT_DATABASE,
        main_endpoint=DEFAULT_ENDPOINT,
    )
    if not measured["distinct_database"]:
        findings.append(
            f"the mapping database and the main project database are the same name "
            f"({PROTECTED_DATABASE!r})"
        )
    if not measured["distinct_endpoint"]:
        findings.append(
            "the protected store and the main graph share an endpoint, so a database name is "
            "the only boundary -- and the pack records that a database is not a boundary on "
            "this image"
        )

    environments = ctx.pack_yaml("environments.yaml")
    dev = (environments.get("environments") or {}).get("dev") or {}
    main_block = dev.get("terminusdb") or {}
    protected_block = dev.get("terminusdb_protected") or {}
    declared: dict[str, Any] = {
        "main_database": main_block.get("main_database"),
        "main_holds_protected_identity_mappings": main_block.get(
            "holds_protected_identity_mappings"
        ),
        "protected_database": protected_block.get("database"),
        "isolated_instance": protected_block.get("isolated_instance"),
        "separate_container": protected_block.get("separate_container"),
        "separate_volume": protected_block.get("separate_volume"),
        "main_admin_credential_must_fail": protected_block.get("main_admin_credential_must_fail"),
        "withheld_from": protected_block.get("withheld_from"),
        "endpoints_declared_distinct": bool(protected_block.get("url"))
        and protected_block.get("url") != main_block.get("url"),
    }
    if declared["protected_database"] != PROTECTED_DATABASE:
        findings.append(
            f"the pack declares the protected database as {declared['protected_database']!r} "
            f"while the code constant is {PROTECTED_DATABASE!r}"
        )
    if declared["main_database"] != DEFAULT_DATABASE:
        findings.append(
            f"the pack declares the main database as {declared['main_database']!r} while the "
            f"code constant is {DEFAULT_DATABASE!r}"
        )
    if declared["main_holds_protected_identity_mappings"] is not False:
        findings.append(
            "environments.yaml does not declare that the main graph holds no protected identity "
            "mappings"
        )
    if declared["isolated_instance"] is not True:
        findings.append("the protected store is not declared an isolated instance")
    if not declared["endpoints_declared_distinct"]:
        findings.append("the pack declares one URL for both the main and protected instances")
    if declared["main_admin_credential_must_fail"] is not True:
        findings.append("the pack does not require the main admin credential to fail there")

    # A separate database reachable with the builder's own credential is a
    # directory, not a boundary. The pack must withhold the credential too.
    secrets_refs = ctx.pack_yaml("secrets.refs.yaml")
    protected_ref = ((secrets_refs.get("refs") or {}).get("terminusdb_protected_auth")) or {}
    withheld = list(protected_ref.get("withheld_from") or [])
    used_by = list(protected_ref.get("used_by") or [])
    if "all_task_participants" not in withheld or "all_worker_sessions" not in withheld:
        findings.append(
            f"the protected credential is not withheld from task participants and worker "
            f"sessions: withheld_from={withheld}"
        )
    if used_by != ["owner_audit_path"]:
        findings.append(f"the protected credential is declared used_by={used_by}")

    live: dict[str, Any] = {
        "attempted": False,
        "reason": "the main builder credential is not in this process's environment",
    }
    main_password = os.environ.get("TERMINUSDB_ADMIN_PASS")
    if main_password:
        try:
            probe = run_sync(
                probe_credential_against_protected(main_password, actor="builder/main-admin")
            )
        except Exception as exc:
            live = {
                "attempted": True,
                "reachable": False,
                "raised": type(exc).__name__,
                "reason": (
                    "the protected instance is not reachable from this host; the declared "
                    "separation stands but was not exercised"
                ),
            }
        else:
            live = {
                "attempted": True,
                "reachable": True,
                "actor": probe.actor,
                "status": probe.status,
                "api_error_type": probe.api_error_type,
                "denied": probe.is_denied,
                "probed_at": probe.probed_at,
            }
            if not probe.is_denied:
                findings.append(
                    f"the main builder credential reached the protected instance with HTTP "
                    f"{probe.status}; Section 11.2's boundary does not hold, and it must NOT be "
                    "repaired by granting access"
                )

    control = _locations_distinct(
        mapping_database=DEFAULT_DATABASE,
        mapping_endpoint=DEFAULT_ENDPOINT,
        main_database=DEFAULT_DATABASE,
        main_endpoint=DEFAULT_ENDPOINT,
    )
    if control["separated"]:
        findings.append(
            "negative control did not fire: the location predicate called one location "
            "separated from itself"
        )

    proof = {
        "check": a.method or "database_location_assert",
        "expected": a.expected,
        "assertion": a.assertion_id,
        "mapping_database": PROTECTED_DATABASE,
        "main_project_database": DEFAULT_DATABASE,
        **measured,
        "constants_read_from": (
            "integrations.protected_identity (the one module Section 11.2 permits to hold the "
            "protected route) and owner_surface.gateway"
        ),
        "why_no_endpoint_literal_appears_here": (
            "the endpoints are compared, never written down, so this check adds no second route "
            "to the protected instance for the architecture scan to find"
        ),
        "owner_declaration": declared,
        "credential_declaration": {"used_by": used_by, "withheld_from": withheld},
        "live_isolation_probe": live,
        "negative_control": {
            "probe": "hand the same predicate the main location as both arguments",
            "why": (
                "'mapping_db != main_project_db' is trivially true for any two strings somebody "
                "typed differently. The control shows the predicate reports a collision when "
                "there is one, and the pack cross-check shows these constants are the deployed "
                "ones."
            ),
            "identical_locations_reported_as_separated": control["separated"],
        },
        "binding": _binding(ctx, {"declared": declared, "live": live, "measured": measured}),
    }
    evidence = {"protected_db_location_proof": proof}
    if findings:
        return bad(findings, evidence)
    note = (
        f"the mapping lives in {PROTECTED_DATABASE!r} on an isolated instance while the main "
        f"graph is {DEFAULT_DATABASE!r} on a different endpoint, and environments.yaml declares "
        "the same separation"
    )
    if live.get("denied"):
        note += f"; the main builder credential is refused there (HTTP {live['status']})"
    return ok(evidence, note)


# ===========================================================================
# A4 — the owner audit path can reveal the mapping, and only it can
# ===========================================================================


def _offline_reveal_arms(policy: ModelPolicy, alias: str) -> tuple[dict[str, Any], list[str]]:
    """Everything about the reveal path that needs no protected instance."""
    findings: list[str] = []
    arms: dict[str, Any] = {}
    row = policy.role_for_alias(alias)
    expected_hash = content_hash({"provider": row.family, "model_id": row.litellm_model})

    # 1. The dispatch-side store resolves for the owner audit path and for
    #    nobody else. That is the "only" in the assertion, tested where it can
    #    be tested without a credential.
    pack_store = PackIdentityStore(policy)
    refusals: dict[str, Any] = {}
    for caller in ("researcher-r17", "implementer-i12", "judge-j03", "anonymous"):
        try:
            run_sync(pack_store.resolve_alias(alias, caller=caller))
            refusals[caller] = {"refused": False}
            findings.append(
                f"caller {caller!r} resolved {alias!r} to a real identity without owner authority"
            )
        except ProtectedAccessError as exc:
            refusals[caller] = {"refused": True, "raised": type(exc).__name__, "detail": str(exc)}
    owner_view = run_sync(pack_store.resolve_alias(alias, caller="owner_audit"))
    owner_hash = content_hash({"provider": owner_view.family, "model_id": owner_view.litellm_model})
    if owner_hash != expected_hash:
        findings.append(
            "the owner audit caller resolved an identity that does not match the pack mapping"
        )
    arms["dispatch_side_store"] = {
        "privileged_callers": sorted(PackIdentityStore.PRIVILEGED_CALLERS),
        "unprivileged_callers_refused": refusals,
        "owner_audit_caller_resolved": True,
        "resolved_matches_pack_mapping": owner_hash == expected_hash,
        "identity_redacted": "compared as a content hash; provider and model id are not recorded",
    }

    # 2. A reveal needs a real audit context. Both fields are required, and a
    #    duck-typed stand-in is refused before any I/O happens.
    request_arms: dict[str, Any] = {}
    for label, owner_identity, reason in (
        ("no_owner_identity", "", "audit"),
        ("no_reason", _OWNER_AUDIT_IDENTITY, ""),
        ("whitespace_only_owner", "   ", "audit"),
    ):
        try:
            OwnerAuditRequest(owner_identity=owner_identity, reason=reason)
            request_arms[label] = {"refused": False}
            findings.append(f"an OwnerAuditRequest with {label} was accepted")
        except ProtectedIdentityAccessError as exc:
            request_arms[label] = {"refused": True, "detail": str(exc)}

    class _NotAnAuditRequest:
        owner_identity = _OWNER_AUDIT_IDENTITY
        reason = "looks like an audit request"

    offline_store = ProtectedIdentityStore(password="unused", endpoint="http://127.0.0.1:1")
    try:
        run_sync(offline_store.reveal_for_owner_audit(alias, _NotAnAuditRequest()))  # type: ignore[arg-type]
        request_arms["duck_typed_request"] = {"refused": False}
        findings.append(
            "a duck-typed audit context was accepted; the reveal guard can be bypassed by any "
            "object with two attributes"
        )
    except ProtectedIdentityAccessError as exc:
        request_arms["duck_typed_request"] = {"refused": True, "detail": str(exc)}
    finally:
        run_sync(offline_store.aclose())
    arms["audit_context_required"] = request_arms

    # 3. With no credential the store refuses to exist rather than falling back
    #    to the main one. A silent fallback is how a protected boundary becomes
    #    a naming convention.
    try:
        protected_store_from_env(environ={})
        arms["absent_credential"] = {"refused": False}
        findings.append(
            "the protected store was constructed with no credential in the environment; a "
            "fallback to the main credential would dissolve Section 11.2"
        )
    except MissingRequiredCredential as exc:
        # The credential's *reference* is deliberately not recorded: the env var
        # name is one of the protected-route markers, and an evidence artifact
        # is a surface too.
        arms["absent_credential"] = {
            "refused": True,
            "raised": type(exc).__name__,
            "typed_blocker": "MISSING_REQUIRED_CREDENTIAL",
            "credential_ref_name": exc.ref_name,
        }
    return arms, findings


def _live_reveal(alias: str, expected_hash: str, reason: str) -> dict[str, Any]:
    """Exercise the owner reveal against the protected instance.

    Returns a transcript that never contains the revealed provider or model id.
    ``state`` is one of ``revealed``, ``no_mapping``, ``unauthenticated``,
    ``unreachable`` or ``no_credential``; only the first is a measurement of the
    property, and the caller decides what the others mean.
    """
    try:
        store = protected_store_from_env()
    except MissingRequiredCredential:
        return {
            "state": "no_credential",
            "detail": (
                "the owner's protected credential is not in this process's environment, so the "
                "owner audit path cannot be exercised from here"
            ),
        }

    async def run() -> dict[str, Any]:
        async with store:
            try:
                known = await store.known_aliases()
            except TerminusAuthError as exc:
                return {"state": "unauthenticated", "detail": str(exc)}
            except TerminusError as exc:
                return {"state": "unreachable", "detail": str(exc)}
            if alias not in known:
                return {
                    "state": "no_mapping",
                    "aliases_held": len(known),
                    "detail": (
                        f"the protected instance is reachable but holds no mapping for {alias!r}; "
                        "seeding it is an owner action (seed_from_model_policy), not something "
                        "this gate may do on the owner's behalf in order to pass"
                    ),
                }

            view = await store.alias_view(alias)
            trail_before = await store.audit_trail(alias)
            request = OwnerAuditRequest(owner_identity=_OWNER_AUDIT_IDENTITY, reason=reason)
            revealed = await store.reveal_for_owner_audit(alias, request)
            trail_after = await store.audit_trail(alias)
            if revealed is None:
                return {
                    "state": "no_mapping",
                    "aliases_held": len(known),
                    "detail": "the reveal returned nothing for an alias the store had listed",
                }
            newest = trail_after[-1] if trail_after else {}
            return {
                "state": "revealed",
                "aliases_held": len(known),
                "alias_view_fields": sorted(f.name for f in dataclasses.fields(AliasView)),
                "alias_view_carries_no_identity": (
                    view is not None
                    and not {"provider", "model_id"} & set(dataclasses.asdict(view))
                ),
                "revealed_matches_pack_mapping": (
                    content_hash({"provider": revealed.provider, "model_id": revealed.model_id})
                    == expected_hash
                ),
                "revealed_fields_present": sorted(
                    name
                    for name, value in dataclasses.asdict(revealed).items()
                    if value not in (None, "")
                ),
                "role": revealed.role,
                "gateway": revealed.gateway,
                "audit_records_before": len(trail_before),
                "audit_records_after": len(trail_after),
                "audit_record": {
                    "owner_identity": newest.get("owner_identity"),
                    "reason": newest.get("reason"),
                    "alias": newest.get("alias"),
                    "revealed_at": newest.get("revealed_at"),
                    "audit_id_present": bool(newest.get("audit_id")),
                },
            }

    try:
        return run_sync(run())
    except TerminusAuthError as exc:
        return {"state": "unauthenticated", "detail": str(exc)}
    except TerminusError as exc:
        return {"state": "unreachable", "detail": str(exc)}
    except Exception as exc:
        return {"state": "unreachable", "detail": f"{type(exc).__name__}: {exc}"}


def d1_06_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A4 ``owner_reveal_probe`` -- expected ``mapping_resolvable_under_owner_identity_only``.

    The offline half runs everywhere: unprivileged callers are refused, a
    malformed or duck-typed audit context is refused, and a store built with no
    credential raises the typed blocker instead of falling back. The live half
    performs the reveal itself against the protected instance and checks that it
    resolves to the pack's own mapping and leaves an audit record. Where the
    credential or the instance is absent the assertion is ``UNVERIFIABLE``: a
    set of refusals is not a reveal, and reporting PASS on refusals alone would
    let this gate go green on a host where the owner audit path does not exist.
    """
    policy = _policy()
    alias = policy.role("implementer").alias
    row = policy.role_for_alias(alias)
    expected_hash = content_hash({"provider": row.family, "model_id": row.litellm_model})

    offline, findings = _offline_reveal_arms(policy, alias)
    live = _live_reveal(
        alias,
        expected_hash,
        reason=f"GATE-D1-06 A4 owner_reveal_probe at candidate {ctx.binding.short}",
    )

    if live["state"] == "revealed":
        if not live["revealed_matches_pack_mapping"]:
            findings.append(
                "the protected instance revealed an identity that does not match the pack's "
                "mapping for this alias; the audit path resolves to something else"
            )
        if not live["alias_view_carries_no_identity"]:
            findings.append(
                "the task-facing alias view carries a provider or model id, so the reveal is "
                "not the only path to the real identity"
            )
        if live["audit_records_after"] <= live["audit_records_before"]:
            findings.append(
                "the reveal left no audit record; Section 11.2's guarantee is that a reveal "
                "cannot happen without a trace"
            )
        record = live["audit_record"]
        if record.get("owner_identity") != _OWNER_AUDIT_IDENTITY or not record.get("reason"):
            findings.append(f"the audit record does not name the caller and the reason: {record}")

    transcript = {
        "check": a.method or "owner_reveal_probe",
        "expected": a.expected,
        "assertion": a.assertion_id,
        "alias_probed": alias,
        "redaction": (
            "no provider, model id, credential or password appears in this artifact. The reveal "
            "is verified by comparing a content hash of {provider, model_id} against the pack's "
            "own mapping, so the transcript proves the mapping resolves without performing the "
            "leak the gate exists to prevent."
        ),
        "offline_arms": offline,
        "live_reveal": live,
        "honest_limits": [
            "the owner's protected credential is read from this process's environment, so what "
            "is proven is that the reveal path works when the owner credential is present -- not "
            "that a builder process cannot obtain it, which is GATE-D1-08's boundary",
            "the reveal performed here is itself audited: the protected instance's audit trail "
            "grows by one record per gate run, which is the mechanism working rather than a side "
            "effect to suppress",
            "the audit record names 'owner_audit_path', not the owner personally, because a gate "
            "exercising the path is not the owner",
        ],
        "negative_control": {
            "probe": (
                "resolve the same alias as a worker, a judge and an anonymous caller; submit an "
                "audit request with no owner, with no reason, and duck-typed; and build the "
                "protected store with no credential in the environment"
            ),
            "why": (
                "'the owner can reveal it' is satisfied by a store that reveals to anybody. Each "
                "arm is a caller that must be refused, so the owner's success means the audit "
                "path and not an open door."
            ),
            "arms": {
                "unprivileged_callers": offline["dispatch_side_store"][
                    "unprivileged_callers_refused"
                ],
                "audit_context_required": offline["audit_context_required"],
                "absent_credential": offline["absent_credential"],
            },
        },
        "binding": _binding(ctx, {"offline": offline, "live": live}),
    }
    evidence = {"owner_reveal_transcript_redacted": transcript}

    if findings:
        return bad(findings, evidence)

    if live["state"] != "revealed":
        reasons = {
            "no_credential": (
                "the owner's protected credential is not available to this process, so the live "
                "reveal could not be exercised"
            ),
            "unreachable": "the protected instance is not reachable from this host",
            "unauthenticated": (
                "the configured protected credential was refused by the protected instance, "
                "which is a configuration finding and not a passing reveal"
            ),
            "no_mapping": "the protected instance holds no mapping for the probed alias",
        }
        return undecided(
            (
                f"{reasons.get(live['state'], live['state'])}: {live.get('detail', '')} The "
                "offline half holds -- unprivileged callers, malformed audit contexts and a "
                "credential-less construction are all refused -- but a refusal to reveal is not "
                "a reveal, so this assertion is UNVERIFIABLE rather than PASS"
            ),
            evidence,
        )

    return ok(
        evidence,
        "the owner audit path revealed the mapping (verified by hash against the pack, never "
        "recorded), the reveal left an audit record, and every unprivileged caller, malformed "
        "audit context and credential-less construction was refused",
    )


# ===========================================================================
# A5 — no agent receives another agent's prestige ranking or cost tier
# ===========================================================================

#: Values that make each injected field look like the real thing rather than a
#: placeholder. A probe that injected ``"x"`` everywhere would test the field
#: name and nothing else.
_RANKING_PROBE_VALUES: dict[str, Any] = {
    "prestige": "frontier",
    "prestige_rank": 1,
    "prestige_ranking": "1 of 15",
    "leaderboard_rank": 3,
    "cost_per_token": 0.000012,
    "input_cost": "$5.00 per M",
    "output_cost": "$25.00 per M",
    "price": "$5.00/$25.00 per M",
    "pricing": {"input": 5.0, "output": 25.0},
    "ranking": ["first", "second"],
    "measured": {"median_latency_s": 1.7},
}


def d1_06_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """A5 ``payload_scan_for_ranking_fields`` -- expected ``zero_matches``.

    Zero here is structural, not lucky, and the check says which: the object a
    worker session receives is a :class:`~models.router.RoutingDecision`, whose
    field list contains no ranking or identity name, and the only projection of
    a policy row an agent may see drops ``tier``, ``family`` and
    ``litellm_model``. The scan over every mapped role then confirms it, and
    each ranking field name is injected in turn to show the instrument fires.
    """
    policy = _policy()
    router = _router(policy)
    findings: list[str] = []

    per_role: dict[str, Any] = {}
    for role in sorted(policy.roles):
        decision, payload = _worker_payload(router, policy, role)
        scan = _scan(payload, policy)
        present = sorted(_ranking_keys(payload))
        per_role[role] = {
            "alias": decision.alias,
            "ranking_or_identity_keys_present": present,
            "dispatch_boundary_findings": scan["dispatch_boundary"],
            "record_surface_findings": scan["record_surface"],
        }
        findings.extend(f"{role}: the payload carries ranking field {key!r}" for key in present)
        findings.extend(f"{role}: {f}" for f in _findings_of(scan))

    decision_fields = {f.name for f in dataclasses.fields(RoutingDecision)}
    leaking_decision_fields = sorted(decision_fields & _RANKING_AND_IDENTITY_FIELDS)
    if leaking_decision_fields:
        findings.append(
            f"RoutingDecision declares ranking or identity fields {leaking_decision_fields}; the "
            "object handed to a worker session can carry them by construction"
        )
    blinded = policy.role("implementer").blinded()
    leaking_projection = sorted(set(blinded) & _RANKING_AND_IDENTITY_FIELDS)
    if leaking_projection:
        findings.append(f"RoleModel.blinded() exposes {leaking_projection}")
    for protected_field in ("tier", "family", "litellm_model"):
        if protected_field in blinded:
            findings.append(f"RoleModel.blinded() carries the protected field {protected_field!r}")

    # Negative control: inject each ranking and identity field name, one at a
    # time, into a real payload. Every one must be caught, and the transcript
    # records *which* boundary caught it -- the two lists differ, and merging
    # them would credit the dispatch gate with names it does not know.
    _, victim = _worker_payload(router, policy, "judge")
    row = policy.role("judge")
    values = dict(_RANKING_PROBE_VALUES)
    values.update({"tier": row.tier, "cost_tier": row.tier, "price_tier": row.tier})
    injections: dict[str, Any] = {}
    caught_by_dispatch: list[str] = []
    caught_only_by_record_surface: list[str] = []
    for field_name in sorted(_RANKING_AND_IDENTITY_FIELDS):
        probe = {
            "messages": [dict(message) for message in victim["messages"]],
            "routing": dict(victim["routing"]),
        }
        probe["routing"][field_name] = values.get(field_name, "a-probe-value")
        scan = _scan(probe, policy)
        by_dispatch = bool(scan["dispatch_boundary"])
        by_surface = bool(scan["record_surface"])
        injections[field_name] = {
            "detected": by_dispatch or by_surface,
            "caught_by_dispatch_boundary": by_dispatch,
            "caught_by_record_surface": by_surface,
        }
        if by_dispatch:
            caught_by_dispatch.append(field_name)
        elif by_surface:
            caught_only_by_record_surface.append(field_name)
        else:
            findings.append(
                f"negative control did not fire: an injected {field_name!r} field passed both "
                "the dispatch-boundary scanner and the record-surface scanner"
            )

    # The control's own control. Without it, every "detected" above is equally
    # consistent with an instrument that flags everything it is shown.
    baseline = _scan(victim, policy)
    baseline_clean = not (baseline["dispatch_boundary"] or baseline["record_surface"])
    if not baseline_clean:
        findings.append(
            "the uninjected payload does not scan clean through the same instrument, so the "
            f"injections prove nothing: {baseline}"
        )

    report = {
        "check": a.method or "payload_scan_for_ranking_fields",
        "expected": a.expected,
        "assertion": a.assertion_id,
        "fields_searched_for": sorted(_RANKING_AND_IDENTITY_FIELDS),
        "roles_scanned": len(per_role),
        "total_matches": sum(
            len(record["ranking_or_identity_keys_present"])
            + len(record["dispatch_boundary_findings"])
            + len(record["record_surface_findings"])
            for record in per_role.values()
        ),
        "per_role": per_role,
        "structural_guarantees": {
            "routing_decision_fields": sorted(decision_fields),
            "routing_decision_carries_no_ranking_field": not leaking_decision_fields,
            "role_row_blinded_projection": sorted(blinded),
            "protected_fields_dropped_by_the_projection": ["tier", "family", "litellm_model"],
        },
        "detector_coverage": {
            "note": (
                "the two enforced lists are not the same list. The dispatch boundary refuses "
                f"{len(caught_by_dispatch)} of these field names outright; the remaining "
                f"{len(caught_only_by_record_surface)} are caught by the record surface's "
                "RANKING_FIELDS. Reported as measured coverage rather than merged, so the "
                "dispatch gate is not credited with names it does not know."
            ),
            "caught_by_dispatch_boundary": sorted(caught_by_dispatch),
            "caught_only_by_record_surface": sorted(caught_only_by_record_surface),
        },
        "negative_control": {
            "probe": (
                f"inject each of the {len(_RANKING_AND_IDENTITY_FIELDS)} ranking and identity "
                "field names, one at a time, into a real judge payload"
            ),
            "why": (
                "zero matches is also what a scanner with an empty pattern list reports. Every "
                "injected field must be caught by at least one enforced boundary, and the "
                "uninjected payload must still scan clean."
            ),
            "injections": injections,
            "baseline_scans_clean": baseline_clean,
        },
        "binding": _binding(ctx, {"per_role": per_role, "injections": injections}),
    }
    evidence = {"ranking_field_scan_report": report}
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        f"zero prestige, cost or ranking fields across {len(per_role)} mapped roles' payloads -- "
        f"structurally, since RoutingDecision declares none -- while each of the "
        f"{len(_RANKING_AND_IDENTITY_FIELDS)} injected field names was caught",
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
CHECKS_D1_06: dict[tuple[str, str], Check] = {
    ("GATE-D1-06", "A1"): d1_06_a1,
    ("GATE-D1-06", "A2"): d1_06_a2,
    ("GATE-D1-06", "A3"): d1_06_a3,
    ("GATE-D1-06", "A4"): d1_06_a4,
    ("GATE-D1-06", "A5"): d1_06_a5,
}
