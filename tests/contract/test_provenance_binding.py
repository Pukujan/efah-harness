"""Contract Sections 8, 9.2, 9.3, 9.8 and GATE-D1-02, exercised without a server.

The ledger fold is pure, so the projection and duration arithmetic can be driven
from a synthetic event stream with a controlled clock. That is deliberate: a
duration test that depends on wall-clock timing proves nothing repeatable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from composition.inventory import first_party_modules
from governance.envelope import CONTRACT_ID, Envelope
from governance.states import GATE_ONLY_STATES, WORKER_SUBMITTABLE_STATES, ProjectState, TaskState
from integrations.pack import load_pack
from ontology.schema import Project, TaskEvent, TaskEventType
from provenance.binding import (
    REQUIRED_ENVELOPE_FIELDS,
    MissingProvenanceBinding,
    StaleContractVersion,
    assert_fully_bound,
    entity_body,
    require_current_contract,
    seal_entity,
    verify_entity,
)
from provenance.importer import (
    LOCKFILE_NAME,
    NO_IMPORT_SITE,
    UNRESOLVED_VERSION,
    build_pack_entities,
    compatibility_constraints_for,
    component_import_prefixes,
    find_lockfile,
    load_lockfile_versions,
    make_import_branch_name,
    modules_importing,
)
from tasks.ledger import Actor, ActorKind, LedgerAuthorityViolation, TaskLedger

PACK_ROOT = Path(__file__).resolve().parents[2] / "project-pack"
T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _project(**overrides) -> Project:
    payload = {
        "entity_id": "EFAH-001",
        "envelope": Envelope(schema_id="efah.project", created_by_alias="ws-b"),
        "name": "EFAH",
        "mode": "autonomous",
        "state": ProjectState.RUNNING,
        "pack_manifest_hash": "sha256:abc",
    }
    payload.update(overrides)
    return Project(**payload)


# -- GATE-D1-02 A1: every envelope field present ----------------------------


def test_required_envelope_fields_are_the_gate_s_eleven():
    gate = {
        "schema_id",
        "schema_version",
        "contract_id",
        "contract_version",
        "methodology_version",
        "terminus_database",
        "terminus_branch",
        "terminus_commit",
        "content_hash",
        "created_by_alias",
        "created_at",
    }
    assert set(REQUIRED_ENVELOPE_FIELDS) == gate


def test_unbound_entity_fails_the_binding_assertion():
    with pytest.raises(MissingProvenanceBinding) as exc:
        assert_fully_bound(_project())
    assert "terminus_commit" in str(exc.value)


def test_two_phase_seal_produces_a_fully_bound_entity():
    phase_one = seal_entity(_project(), database="efah", branch="import-1")
    assert phase_one.envelope.terminus_commit is None
    phase_two = seal_entity(phase_one, commit="abc123")
    assert_fully_bound(phase_two)
    assert phase_two.envelope.terminus_commit == "abc123"


# -- GATE-D1-02 A4: content hash recomputes ---------------------------------


def test_content_hash_verifies_after_binding():
    sealed = seal_entity(_project(), database="efah", branch="b", commit="c")
    assert verify_entity(sealed)


def test_tampering_with_the_body_breaks_the_hash():
    sealed = seal_entity(_project(), database="efah", branch="b", commit="c")
    tampered = sealed.model_copy(update={"pack_manifest_hash": "sha256:different"})
    assert not verify_entity(tampered)


def test_rebinding_reseals_rather_than_leaving_a_stale_hash():
    phase_one = seal_entity(_project(), database="efah", branch="b")
    phase_two = seal_entity(phase_one, commit="c")
    assert phase_one.envelope.content_hash != phase_two.envelope.content_hash
    assert verify_entity(phase_two)


def test_entity_body_excludes_the_envelope():
    body = entity_body(seal_entity(_project(), database="efah", branch="b", commit="c"))
    assert "envelope" not in body
    assert body["entity_id"] == "EFAH-001"


# -- GATE-D1-02 A2: stale contract version is rejected, not migrated --------


def test_stale_contract_version_is_rejected():
    stale = Envelope(schema_id="efah.project", created_by_alias="ws-b", contract_version="0.9")
    with pytest.raises(StaleContractVersion):
        require_current_contract(stale)


def test_future_contract_version_is_rejected():
    ahead = Envelope(schema_id="efah.project", created_by_alias="ws-b", contract_version="2.0")
    with pytest.raises(StaleContractVersion):
        require_current_contract(ahead)


def test_foreign_contract_is_rejected():
    foreign = Envelope(schema_id="efah.project", created_by_alias="ws-b", contract_id="OTHER-001")
    with pytest.raises(StaleContractVersion):
        require_current_contract(foreign)


def test_v1_0_object_is_still_valid_under_v1_1():
    """v1.1 is v1.0 plus an additive amendment; a v1.0 object is not stale."""
    older = Envelope(schema_id="efah.project", created_by_alias="ws-b", contract_version="1.0")
    require_current_contract(older)
    assert older.contract_id == CONTRACT_ID


# -- Pack import entity construction (GATE-D1-01 A2/A4) ---------------------


def test_pack_entities_bind_to_the_pack_contract_id_and_version():
    pack = load_pack(PACK_ROOT)
    entities = build_pack_entities(pack, author_alias="ws-b")
    by_id = {e.document_id: e for e in entities}
    contract = by_id["Contract/EFAH-CONTRACT-001"]
    assert contract.contract_key == pack.contract_id
    assert contract.current_version == pack.contract_version


def test_pack_import_never_writes_a_real_model_identity_into_the_main_graph():
    """GATE-D1-06: aliases cross to the main graph, vendors and model ids do not."""
    pack = load_pack(PACK_ROOT)
    policy = pack.yaml("model-policy.yaml")
    secrets = {
        str(block["litellm_model"]) for block in policy["aliases"].values() if "litellm_model" in block
    } | {str(block["family"]) for block in policy["aliases"].values() if "family" in block}

    serialised = repr([e.model_dump() for e in build_pack_entities(pack, author_alias="ws-b")])
    leaked = sorted(s for s in secrets if s in serialised)
    assert not leaked, f"real model identity leaked into the main graph: {leaked}"


# -- Section 16.3 exact_version_and_lockfile_source -------------------------


def _dependency_versions(pack, **kwargs) -> dict[str, object]:
    return {
        e.component: e
        for e in build_pack_entities(pack, author_alias="ws-b", **kwargs)
        if type(e).__name__ == "DependencyVersion"
    }


def test_dependency_versions_are_read_from_the_lockfile_not_the_pack(tmp_path):
    """Section 16.3 wants an exact version *and* the file it was pinned in.

    Every ``selected_stack`` entry in the pack reads ``version:
    TODO_builder_probe``, so a DependencyVersion naming the pack as its
    lockfile source was asserting a pin that no file contained. The version
    must come from the resolver output, which this proves by locking a
    deliberately impossible version: a pass-through of the pack's declaration
    could not produce it.
    """
    lock = tmp_path / LOCKFILE_NAME
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "fastapi"\nversion = "9.9.9"\n\n'
        '[[package]]\nname = "inspect-ai"\nversion = "0.3.251"\n'
    )
    deps = _dependency_versions(load_pack(PACK_ROOT), lock_path=lock)

    assert deps["fastapi"].exact_version == "9.9.9"
    assert deps["fastapi"].lockfile_source == LOCKFILE_NAME
    # `inspect_ai` is the pack's nickname; `inspect-ai` is the distribution.
    # PEP 503 normalisation is what bridges them, not a hand-maintained alias.
    assert deps["inspect_ai"].exact_version == "0.3.251"


def test_components_absent_from_the_lockfile_stay_visibly_unresolved(tmp_path):
    """A Python lockfile cannot pin terminusdb, plane or promptfoo.

    The failure mode worth guarding is not the missing version -- it is
    claiming a lockfile source for it anyway, which would read downstream as a
    pin that can be verified. Unresolved must stay legible as unresolved.
    """
    lock = tmp_path / LOCKFILE_NAME
    lock.write_text('version = 1\n\n[[package]]\nname = "fastapi"\nversion = "9.9.9"\n')
    deps = _dependency_versions(load_pack(PACK_ROOT), lock_path=lock)

    for component in ("terminusdb", "plane", "promptfoo"):
        assert deps[component].exact_version == UNRESOLVED_VERSION
        assert deps[component].lockfile_source == "project-pack/dependency-policy.yaml"
        assert LOCKFILE_NAME not in deps[component].lockfile_source


def test_no_lockfile_anywhere_fabricates_no_versions(tmp_path):
    """A checkout that has not been locked must not silently gain pins."""
    assert find_lockfile(tmp_path) is None


def test_lockfile_is_found_by_walking_up_from_the_pack():
    """The pack lives at ``<repo>/project-pack``; the lock lives at ``<repo>``."""
    found = find_lockfile(PACK_ROOT)
    assert found is not None, "run `uv lock` -- the repository lockfile is missing"
    assert found == PACK_ROOT.parent / LOCKFILE_NAME


def test_real_lockfile_pins_every_python_component_in_the_selected_stack():
    """Regression for the state this replaced: all sixteen read TODO_builder_probe."""
    deps = _dependency_versions(load_pack(PACK_ROOT))
    locked = {c: d for c, d in deps.items() if d.lockfile_source == LOCKFILE_NAME}

    assert {"fastapi", "pydantic", "langgraph", "docling", "lancedb", "inspect_ai"} <= set(locked)
    for component, dep in locked.items():
        assert dep.exact_version != UNRESOLVED_VERSION, component
        assert dep.exact_version[0].isdigit(), f"{component}: {dep.exact_version} is not a version"


def test_lockfile_parser_normalises_distribution_names(tmp_path):
    lock = tmp_path / LOCKFILE_NAME
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "Inspect_AI"\nversion = "0.3.251"\n\n'
        '[[package]]\nname = "opentelemetry.sdk"\nversion = "1.44.0"\n'
    )
    assert load_lockfile_versions(lock) == {
        "inspect-ai": "0.3.251",
        "opentelemetry-sdk": "1.44.0",
    }


def test_import_branch_name_is_terminusdb_safe():
    """Measured: a ``/`` in a branch name is parsed as a path separator."""
    name = make_import_branch_name("sha256:4a0a3dae2f23", now=T0)
    assert name == "import-pack-4a0a3dae-20260802T120000Z"
    assert "/" not in name


# -- Section 9.3 authority --------------------------------------------------


def _actor(kind: ActorKind) -> Actor:
    return Actor(alias=f"{kind}-1", role=str(kind), kind=kind)


def test_only_gates_may_produce_passed():
    for kind in (ActorKind.WORKER, ActorKind.SYSTEM, ActorKind.OWNER):
        with pytest.raises(LedgerAuthorityViolation):
            TaskLedger.check_authority(TaskEventType.GatePassed, _actor(kind), TaskState.PASSED)
    TaskLedger.check_authority(TaskEventType.GatePassed, _actor(ActorKind.GATE), TaskState.PASSED)


def test_worker_may_submit_candidate_complete():
    TaskLedger.check_authority(
        TaskEventType.ArtifactSubmitted, _actor(ActorKind.WORKER), TaskState.CANDIDATE_COMPLETE
    )


def test_worker_may_not_write_any_gate_only_state():
    for state in GATE_ONLY_STATES:
        with pytest.raises(LedgerAuthorityViolation):
            TaskLedger.check_authority(TaskEventType.TaskClosed, _actor(ActorKind.WORKER), state)


def test_worker_authorable_states_are_the_kernel_s_submittable_set():
    from tasks.ledger import WORKER_AUTHORABLE_STATES

    assert WORKER_SUBMITTABLE_STATES <= WORKER_AUTHORABLE_STATES
    assert not (GATE_ONLY_STATES & WORKER_AUTHORABLE_STATES)


def test_a_worker_cannot_author_a_gate_event_type_at_all():
    with pytest.raises(LedgerAuthorityViolation):
        TaskLedger.check_authority(TaskEventType.GateFailed, _actor(ActorKind.WORKER), None)


# -- Section 9.8 durations from system events -------------------------------


class _FakeWriter:
    """Enough of ProvenanceWriter for the pure fold. No network."""

    database = "efah"
    branch = "import-1"
    author_alias = "ws-b"


def _event(seq: int, event_type: TaskEventType, offset: int, **kw) -> TaskEvent:
    return TaskEvent(
        entity_id=f"EV-T-010-{seq:06d}",
        envelope=Envelope(schema_id="efah.task_event", created_by_alias="system"),
        task="Task/T-010",
        sequence=seq,
        event_type=event_type,
        actor_alias=kw.pop("actor", "system"),
        actor_role="control-plane",
        recorded_at=T0 + timedelta(seconds=offset),
        resulting_state=kw.pop("state", None),
        payload=kw.pop("payload", {}),
    )


def _ledger() -> TaskLedger:
    return TaskLedger(_FakeWriter())  # type: ignore[arg-type]


def test_fold_derives_every_section_9_8_timestamp():
    stream = [
        _event(1, TaskEventType.TaskCreated, 0, state=TaskState.PROPOSED),
        _event(2, TaskEventType.TaskReady, 10, state=TaskState.READY),
        _event(3, TaskEventType.TaskAssigned, 30, state=TaskState.CLAIMED, payload={"assigned_alias": "impl-1"}),
        _event(4, TaskEventType.WorkerStarted, 40, state=TaskState.RUNNING),
        _event(5, TaskEventType.TaskBlocked, 60, state=TaskState.BLOCKED_DEPENDENCY),
        _event(6, TaskEventType.WorkerStarted, 120, state=TaskState.RUNNING),
        _event(7, TaskEventType.ArtifactSubmitted, 140, state=TaskState.CANDIDATE_COMPLETE),
        _event(8, TaskEventType.EvaluationStarted, 150, state=TaskState.VERIFYING),
        _event(9, TaskEventType.GateFailed, 170, state=TaskState.FAILED_VISIBLE_TEST),
        _event(10, TaskEventType.TaskReworked, 175, state=TaskState.REWORK_REQUIRED),
        _event(11, TaskEventType.ArtifactSubmitted, 205, state=TaskState.CANDIDATE_COMPLETE),
        _event(12, TaskEventType.EvaluationStarted, 210, state=TaskState.VERIFYING),
        _event(13, TaskEventType.GatePassed, 230, state=TaskState.PASSED),
        _event(14, TaskEventType.TaskCompleted, 240, state=TaskState.PASSED),
        _event(15, TaskEventType.TaskMerged, 260, state=TaskState.MERGED),
    ]
    projection = _ledger().fold("Task/T-010", stream)

    assert projection.state is TaskState.MERGED
    assert projection.queued_at == T0
    assert projection.claimed_at == T0 + timedelta(seconds=30)
    assert projection.started_at == T0 + timedelta(seconds=40)
    assert projection.blocked_at == T0 + timedelta(seconds=60)
    assert projection.resumed_at == T0 + timedelta(seconds=120)
    assert projection.candidate_submitted_at == T0 + timedelta(seconds=205)
    assert projection.verification_started_at == T0 + timedelta(seconds=210)
    assert projection.completed_at == T0 + timedelta(seconds=240)
    assert projection.merged_at == T0 + timedelta(seconds=260)
    assert projection.assigned_alias == "impl-1"

    assert projection.queue_seconds == 30.0
    assert projection.blocked_seconds == 60.0
    assert projection.evaluation_seconds == 40.0  # 150->170 and 210->230
    assert projection.rework_seconds == 30.0  # 175->205
    assert projection.active_seconds == 140.0  # 40->240 minus 60 blocked
    assert projection.total_wall_clock_seconds == 260.0
    assert projection.gate_failures == 1
    assert projection.rework_count == 1
    assert projection.event_count == 15


def test_fold_is_deterministic():
    stream = [
        _event(1, TaskEventType.TaskCreated, 0, state=TaskState.PROPOSED),
        _event(2, TaskEventType.TaskReady, 5, state=TaskState.READY),
    ]
    first = _ledger().fold("Task/T-010", stream)
    second = _ledger().fold("Task/T-010", stream)
    assert first.model_dump(exclude={"envelope"}) == second.model_dump(exclude={"envelope"})


def test_fold_refuses_an_empty_stream():
    from tasks.ledger import LedgerIntegrityError

    with pytest.raises(LedgerIntegrityError):
        _ledger().fold("Task/T-010", [])


def test_append_has_no_timestamp_parameter():
    """Section 9.8: time is measured from system events, never supplied by an agent."""
    import inspect

    params = set(inspect.signature(TaskLedger.append).parameters)
    assert not params & {"recorded_at", "timestamp", "at", "duration", "elapsed"}


# -- Section 16.3 modules_and_contracts_using_it ----------------------------
#
# The field was `[str(component)]`: the dependency's own selected_stack key.
# `langgraph` reported that it was used by "workflow_runtime", which is the
# dependency's name spelled a second way rather than an answer to the question.
# Every assertion below would pass trivially against that implementation unless
# it names real modules, so each one does.

#: Measured 2026-08-03 by AST sweep of src/. Recorded here rather than derived
#: so that a *change* in the coupling is a test failure a human reads, not a
#: silently updated number.
LANGGRAPH_IMPORTERS = [
    "owner_surface.graph",
    "workflows.checkpoint",
    "workflows.graphs",
    "workflows.graphs.assurance",
    "workflows.graphs.build",
    "workflows.graphs.contract",
    "workflows.graphs.dependencies",
    "workflows.graphs.intake",
    "workflows.graphs.planning",
    "workflows.graphs.project",
    "workflows.interrupts",
]


def test_modules_using_names_modules_not_the_component_itself():
    """The regression guard: an echo of the component key must fail here.

    ``langgraph`` sits under the ``workflow_runtime`` key. Neither string is a
    module, and the previous implementation could produce nothing else.
    """
    deps = _dependency_versions(load_pack(PACK_ROOT))
    real = set(first_party_modules())

    for component, dep in deps.items():
        assert component not in dep.modules_using, f"{component}: echoes its own name"
        for entry in dep.modules_using:
            if entry == NO_IMPORT_SITE:
                continue
            assert entry in real, f"{component}: {entry!r} is not a module in src/"
            assert "." in entry or entry in real

    assert "workflow_runtime" not in deps["langgraph"].modules_using
    assert "langgraph" not in deps["langgraph"].modules_using


def test_langgraph_records_its_eleven_real_importers():
    deps = _dependency_versions(load_pack(PACK_ROOT))
    assert deps["langgraph"].modules_using == LANGGRAPH_IMPORTERS


def test_measured_import_counts_match_the_source_tree():
    deps = _dependency_versions(load_pack(PACK_ROOT))
    assert len(deps["fastapi"].modules_using) == 8
    assert len(deps["pydantic"].modules_using) == 21
    assert len(deps["opentelemetry"].modules_using) == 3
    assert deps["opentelemetry"].modules_using == [
        "evaluation.checks_audit_followup",
        "integrations.otel",
        "observability.spans",
    ]


def test_the_sqlite_saver_is_scoped_to_the_subtree_it_publishes():
    """Three distributions publish into ``langgraph``; they are not one component.

    ``langgraph-checkpoint-sqlite`` owns ``langgraph.checkpoint.sqlite`` and
    nothing else. Attributing the whole root to it would record all eleven
    LangGraph importers as users of the SQLite saver -- a claim as wrong as the
    one this field replaced, and harder to spot because it looks measured.
    """
    deps = _dependency_versions(load_pack(PACK_ROOT))
    assert deps["langgraph_async_sqlite_saver"].modules_using == ["workflows.checkpoint"]
    assert "workflows.graphs.build" not in deps["langgraph_async_sqlite_saver"].modules_using
    # The one module that uses both is recorded under both, correctly.
    assert "workflows.checkpoint" in deps["langgraph"].modules_using


def test_a_component_reached_over_http_says_so_rather_than_guessing():
    """litellm, terminusdb, plane and phoenix have zero Python import sites.

    They are reached over HTTP. ``NO_IMPORT_SITE`` records that the sweep ran
    and found none, which is a different statement from an empty list nobody
    populated -- the same distinction ``UNRESOLVED_VERSION`` draws for pins.
    Naming ``models/gateway.py`` here instead would answer a question the scan
    cannot verify.
    """
    deps = _dependency_versions(load_pack(PACK_ROOT))
    for component in ("litellm", "terminusdb", "plane", "phoenix"):
        assert deps[component].modules_using == [NO_IMPORT_SITE]
    # Declared and installed, but nothing imports them yet.
    for component in ("docling", "lancedb", "inspect_ai", "promptfoo", "llamaindex"):
        assert deps[component].modules_using == [NO_IMPORT_SITE]
    # Not importable packages at all.
    for component in ("python", "context7"):
        assert component_import_prefixes(component) == ()
        assert deps[component].modules_using == [NO_IMPORT_SITE]


def test_modules_using_is_read_from_the_scan_and_not_from_the_pack():
    """The lockfile test's trick, applied to imports: inject an impossible fact.

    A pass-through of the pack's declaration could not produce ``made.up.module``
    for a component the pack never mentions in those terms.
    """
    deps = _dependency_versions(
        load_pack(PACK_ROOT),
        import_sites={"fastapi.routing": ["made.up.module"], "pydantic": []},
    )
    assert deps["fastapi"].modules_using == ["made.up.module"]
    assert deps["pydantic"].modules_using == [NO_IMPORT_SITE]


def test_prefix_matching_does_not_run_past_a_name_boundary():
    """``fastapi_extras`` is a different distribution from ``fastapi``."""
    sites = {"fastapi_extras": ["a.b"], "fastapi.routing": ["c.d"], "fastapi": ["e.f"]}
    assert modules_importing("fastapi", sites) == ["c.d", "e.f"]


# -- Section 16.3 known_compatibility_constraints ---------------------------


def test_constraints_come_from_the_pinned_distribution_s_own_metadata():
    """``[]`` on all sixteen was not "no constraints", it was "not asked"."""
    deps = _dependency_versions(load_pack(PACK_ROOT))

    otel = deps["opentelemetry"].compatibility_constraints
    assert "opentelemetry-api==1.44.0" in otel, "the exact pin the SDK imposes"
    assert "Requires-Python: >=3.10" in otel

    # The ceiling that broke this environment before the lock existed.
    assert "click!=8.2.0,<8.2.2,>=8.1.3" in deps["inspect_ai"].compatibility_constraints


def test_constraints_exclude_requirements_behind_an_unrequested_extra():
    """``httpx`` is a fastapi requirement only under ``fastapi[standard]``."""
    constraints = _dependency_versions(load_pack(PACK_ROOT))["fastapi"].compatibility_constraints
    assert "starlette>=0.46.0" in constraints
    assert not [c for c in constraints if "extra" in c]


def test_constraints_are_refused_when_the_installed_version_is_not_the_pin():
    """Constraints are version-specific; a mismatch is not a near-enough answer."""
    assert compatibility_constraints_for("fastapi", "0.0.0-not-installed") == []
    assert compatibility_constraints_for("no-such-distribution-anywhere", "1.0") == []


def test_an_unpinned_component_claims_no_constraints():
    """No lockfile pin means no version whose metadata could be trusted."""
    deps = _dependency_versions(load_pack(PACK_ROOT))
    for component in ("terminusdb", "plane", "promptfoo", "litellm"):
        assert deps[component].exact_version == UNRESOLVED_VERSION
        assert deps[component].compatibility_constraints == []
