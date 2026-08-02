"""GATE-D2-21 — scope drift and security expansion blocked.

Contract Sections 19.1, 19.2 and 19.5. Five assertions, all five executable
today against the real drift engine (:mod:`drift.engine`), the real Section 19.5
boundary (:mod:`drift.security`) and a real compilation of the owner's pack:

    A1 an injected unlinked task is detected as UNLINKED_TASK
    A2 an injected requirement weakening is detected *and blocks*
    A3 a change outside the allowed paths is rejected
    A4 an unmapped security finding becomes an OUT_OF_SCOPE_OBSERVATION
    A5 a blocking security finding carries all three Section 19.5 conditions

Nothing here mocks the subject. Every arm runs ``DriftEngine.scan`` over a real
:class:`~contracts.compiler.CompiledProject`; the only things injected are the
defects the assertions name, and they are injected into deep copies so the
cached compilation the other checks read is never vandalised.

Three rules shaped every check below.

**The gate's own wording makes the injection the positive arm.** A1 says "an
*injected* unlinked task is detected". So the arm that proves the assertion is
the one where the detector fires, and the negative control is the inverse: the
clean state must *not* be flagged. That inversion is the whole point of the
gate. A drift engine that returned every finding type for every input would
satisfy all five assertions read literally and would be worthless -- it would
halt the build permanently and tell nobody anything. Every check here therefore
scans a conformant state through the same engine and requires silence from it.

**"Detected" and "blocked" are different claims, and only A2, A3 and A4 name
the second.** A1's ``expected`` is ``detected``; A2's is
``detected_and_blocked``; A3's is ``rejected``; A4's is
``classified_as_observation_and_does_not_block``. Where the gate asks about
blocking, the check reads ``Finding.blocks``, ``DriftReport.blocking``,
``unresolved_scope_drift`` *and* ``terminal_state`` -- because a finding flagged
``blocks=True`` that never reaches ``FAILED_CONTRACT`` has been detected and not
blocked, and A4's whole content is that a downgraded finding leaves
``ProjectState.RUNNING`` intact.

**A finding type is not interchangeable with its neighbours.** Section 19.2's
vocabulary is closed, and a detector that stamped ``OUTSIDE_ALLOWED_PATHS`` on
everything it disliked would pass A3 while destroying the vocabulary's meaning.
A3 therefore also drives a sealed-asset path through the same loop and requires
``PROTECTED_ASSET_ACCESS`` instead, and A4 requires an in-scope security finding
to stay *out* of the observation list.
"""

from __future__ import annotations

import copy
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any

from contracts.compiler import CompiledProject, compile_pack
from drift import security
from drift.engine import ActiveTask, DriftEngine, DriftReport, DriftScanInput
from governance.envelope import CONTRACT_VERSION, content_hash
from governance.states import DriftFinding, ProjectState
from integrations.pack import load_pack

if TYPE_CHECKING:  # pragma: no cover - typing only
    from evaluation.checks import AssertionOutcome, Check, GateContext
    from evaluation.gate_spec import AssertionSpec, GateSpec


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


#: A requirement id the compiled catalog demonstrably does not contain. A1's
#: second arm needs a task that *looks* linked, because "declares no
#: requirement_ids" is the easy half of the property and "declares ids nobody
#: approved" is the half a naive detector misses.
PHANTOM_REQUIREMENT_ID = "REQ-PHANTOM-D2-21-NEGATIVE-CONTROL"

#: A path no allowed-path pattern in the pack can match, used by A3.
UNGOVERNED_PATH = "/etc/passwd"


# ===========================================================================
# Shared subjects
# ===========================================================================


@functools.lru_cache(maxsize=4)
def _compiled(repo_root: Path) -> CompiledProject:
    """The compilation every arm scans. Cached, and therefore never mutated."""
    return compile_pack(load_pack(repo_root / "project-pack"), repo_root=repo_root)


@functools.lru_cache(maxsize=4)
def _engine(repo_root: Path) -> DriftEngine:
    """One engine over that compilation.

    ``DriftEngine.scan`` builds a fresh :class:`DriftReport` per call and never
    writes back to the engine, so sharing it across arms cannot leak state from
    an injected defect into a control.
    """
    return DriftEngine(_compiled(repo_root))


def _scan(repo_root: Path, **kwargs: Any) -> DriftReport:
    return _engine(repo_root).scan(DriftScanInput(compiled=_compiled(repo_root), **kwargs))


def _reference_task(project: CompiledProject) -> tuple[str, dict[str, Any]]:
    """A real compiled task to build active-task probes from.

    Chosen from the compiled plan rather than invented, so every "clean" arm is
    clean by the contract's own definition and not by this file's opinion of it.
    """
    for task_id in sorted(project.tasks):
        task = project.tasks[task_id]
        if task.get("requirement_ids") and task.get("allowed_paths"):
            return task_id, task
    return "", {}


def _active_task(project: CompiledProject, task_id: str, **overrides: Any) -> ActiveTask:
    """An active task that matches the compiled plan exactly, before overrides.

    Every field a drift detector consults is copied from the plan, which is what
    a conformant worker would honestly report. A probe built from hand-written
    defaults could be flagged for some unrelated reason and record a green for a
    rule it never reached.
    """
    task = project.tasks[task_id]
    params: dict[str, Any] = {
        "task_id": task_id,
        "title": str(task.get("title", "")),
        "requirement_ids": tuple(task.get("requirement_ids") or ()),
        "contract_version": CONTRACT_VERSION,
        "state": str(task.get("state", "RUNNING")),
        "changed_paths": (),
        "allowed_paths": tuple(task.get("allowed_paths") or ()),
        "prohibited_paths": tuple(task.get("prohibited_paths") or ()),
    }
    params.update(overrides)
    return ActiveTask(**params)


def _finding_bodies(report: DriftReport, finding: DriftFinding) -> list[dict[str, Any]]:
    return [f.as_body() for f in report.of_type(finding)]


def _report_summary(report: DriftReport) -> dict[str, Any]:
    """The report's verdict-bearing fields, without its full body."""
    return {
        "types_found": report.types_found(),
        "finding_count": len(report.findings),
        "blocking_count": len(report.blocking),
        "unresolved_scope_drift": report.unresolved_scope_drift,
        "terminal_state": report.terminal_state.value,
    }


def _evidence(
    ctx: GateContext,
    gate: GateSpec,
    execution_log: dict[str, Any],
    negative_control: dict[str, Any],
) -> dict[str, Any]:
    return {
        "gate_execution_log": execution_log,
        "negative_control_transcript": negative_control,
        "artifact_hashes_and_commit_binding": {
            "candidate_commit": ctx.binding.commit_sha,
            "contract_version": ctx.binding.contract_version,
            "compiled_contract_version": _compiled(ctx.repo_root).contract_version,
            "gate_source_hash": gate.source_hash,
            "transcript_hash": content_hash(
                {"execution": execution_log, "negative_control": negative_control}
            ),
        },
    }


# ===========================================================================
# A1 — an injected unlinked task is detected as UNLINKED_TASK
# ===========================================================================


def d2_21_a1(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``negative_control_inject_unlinked_task`` → ``detected`` (UNLINKED_TASK).

    Two injections, because "links to a Requirement" fails in two different ways
    and only one of them is trivial. The first task declares no requirement ids
    at all. The second declares one that does not exist in the compiled catalog
    -- it looks linked to anything that merely checks the field is populated,
    and Section 19.2 still calls it an unlinked task. Both must be reported
    against the injected task and against nothing else.

    The negative control is the same task with the requirement ids the compiled
    plan gives it, plus a sweep in which *every* compiled task is presented as
    active. A detector that flagged all of them would satisfy this assertion's
    wording and would be a broken engine; the sweep is what refuses that reading.
    """
    project = _compiled(ctx.repo_root)
    task_id, plan_task = _reference_task(project)
    if not task_id:
        return bad(
            [
                "the compiled plan carries no task with requirement ids and allowed paths, so "
                "there is nothing to inject an unlinked task against"
            ]
        )

    arms: dict[str, dict[str, Any]] = {}
    findings: list[str] = []

    injections = {
        "declares_no_requirement_ids": (
            {"requirement_ids": ()},
            "active task links to no compiled requirement",
        ),
        "declares_a_requirement_the_catalog_does_not_contain": (
            {"requirement_ids": (PHANTOM_REQUIREMENT_ID,)},
            "not in the compiled catalog",
        ),
    }
    for label, (override, expected_detail) in injections.items():
        report = _scan(ctx.repo_root, active_tasks=[_active_task(project, task_id, **override)])
        detected = _finding_bodies(report, DriftFinding.UNLINKED_TASK)
        arms[label] = {
            "injected": {k: list(v) for k, v in override.items()},
            "unlinked_task_findings": detected,
            "subjects": [f["subject"] for f in detected],
            "expected_detail_fragment": expected_detail,
            **_report_summary(report),
        }
        if not detected:
            findings.append(
                f"{label}: the injected task produced no {DriftFinding.UNLINKED_TASK} finding; "
                f"the types found were {report.types_found()}"
            )
            continue
        if [f["subject"] for f in detected] != [task_id]:
            findings.append(
                f"{label}: UNLINKED_TASK was reported against "
                f"{[f['subject'] for f in detected]}, not against the injected task {task_id!r} "
                "alone"
            )
        if not any(expected_detail in f["detail"] for f in detected):
            findings.append(
                f"{label}: the finding does not say why the task is unlinked (expected "
                f"{expected_detail!r}, got {[f['detail'] for f in detected]})"
            )
        if not all(f["blocks"] for f in detected):
            findings.append(f"{label}: an UNLINKED_TASK finding was recorded as non-blocking")

    # Negative control 1: the same task, exactly as the plan describes it.
    clean = _active_task(project, task_id)
    clean_report = _scan(ctx.repo_root, active_tasks=[clean])
    clean_unlinked = _finding_bodies(clean_report, DriftFinding.UNLINKED_TASK)

    # Negative control 2: every compiled task presented as active at once. No
    # changed paths, so no path rule can fire and the only property under
    # observation is requirement linkage.
    sweep_tasks = [_active_task(project, tid) for tid in sorted(project.tasks)]
    sweep_report = _scan(ctx.repo_root, active_tasks=sweep_tasks)
    sweep_unlinked = _finding_bodies(sweep_report, DriftFinding.UNLINKED_TASK)

    if clean_unlinked:
        findings.append(
            "negative control failed: the task exactly as the compiled plan describes it was "
            f"reported UNLINKED_TASK ({clean_unlinked})"
        )
    if clean_report.blocking:
        findings.append(
            "negative control failed: a conformant active task produced blocking drift "
            f"{[f.as_body() for f in clean_report.blocking]}"
        )
    if clean_report.terminal_state is not ProjectState.RUNNING:
        findings.append(
            "negative control failed: a conformant state terminated at "
            f"{clean_report.terminal_state.value}"
        )
    if sweep_unlinked:
        findings.append(
            f"negative control failed: {len(sweep_unlinked)} of the {len(sweep_tasks)} compiled "
            f"tasks were reported UNLINKED_TASK: {[f['subject'] for f in sweep_unlinked]}. A "
            "detector that fires on the approved plan is not a drift detector."
        )

    execution_log = {
        "check": a.method or "negative_control_inject_unlinked_task",
        "expected": a.expected,
        "failure_state": a.failure_state,
        "compiled_requirements": len(_engine(ctx.repo_root).requirement_ids),
        "compiled_tasks": len(project.tasks),
        "injection_target": task_id,
        "plan_requirement_ids": list(plan_task.get("requirement_ids") or ()),
        "arms": arms,
        "what_unlinked_means_here": (
            "an active task none of whose requirement_ids appears in the compiled requirement "
            "catalog. The catalog is derived from the owner's pack by the contract compiler, so "
            "a task cannot mint a link by naming an id that does not exist."
        ),
    }
    negative_control = {
        "probe": (
            "scan the same task with the requirement ids the compiled plan gives it, then scan "
            f"all {len(sweep_tasks)} compiled tasks as active at once"
        ),
        "why": (
            "'an injected unlinked task is detected' is satisfied perfectly by an engine that "
            "reports UNLINKED_TASK for every task it is ever shown. These arms must be silent, "
            "so the check distinguishes detection from indiscriminate flagging."
        ),
        "clean_task": {
            "task_id": task_id,
            "requirement_ids": list(clean.requirement_ids),
            "unlinked_task_findings": clean_unlinked,
            **_report_summary(clean_report),
        },
        "all_compiled_tasks_presented_as_active": {
            "tasks_scanned": len(sweep_tasks),
            "unlinked_task_findings": sweep_unlinked,
            **_report_summary(sweep_report),
        },
    }
    evidence = _evidence(ctx, gate, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            "an injected task with no requirement ids and an injected task citing "
            f"{PHANTOM_REQUIREMENT_ID!r} are both reported UNLINKED_TASK against {task_id} and "
            f"nothing else, while all {len(sweep_tasks)} compiled tasks scan clean"
        ),
    )


# ===========================================================================
# A2 — an injected requirement weakening is detected and blocked
# ===========================================================================


def _weakening_victim(project: CompiledProject) -> tuple[str, dict[str, Any]]:
    """A blocking gate with at least two assertions, chosen deterministically.

    Two assertions matter: stripping one from a gate that has only one would
    leave an assertion-less gate, and the finding could then be attributed to
    the gate being empty rather than to the assertion being gone.
    """
    for gate_id in sorted(project.gates):
        gate = project.gates[gate_id]
        if gate.get("blocking") and len(gate.get("assertions") or []) >= 2:
            return gate_id, gate
    return "", {}


def d2_21_a2(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``negative_control_weaken_requirement`` → ``detected_and_blocked``.

    Three ways a requirement is weakened without anybody editing a requirement,
    each injected into its own copy of the observed gate set so the resulting
    finding is attributable to one defect:

    * a blocking gate is observed as non-blocking;
    * an assertion is removed from a gate;
    * a gate the contract compiles is absent from the observed set entirely.

    ``detected_and_blocked`` is two claims. Detection is the
    ``REQUIREMENT_WEAKENING`` finding against the right subject; blocking is
    ``Finding.blocks``, a non-zero ``unresolved_scope_drift`` -- the value
    Section 21.2's auto-merge requirement reads -- and a terminal state of
    ``FAILED_CONTRACT``. A finding that is recorded and then does not stop the
    build has been noticed, not blocked.

    The negative control observes the compiled gate set verbatim: no weakening,
    no blocking findings, and the project still ``RUNNING``.
    """
    project = _compiled(ctx.repo_root)
    victim_id, victim = _weakening_victim(project)
    if not victim_id:
        return bad(
            [
                "the compiled contract declares no blocking gate with two or more assertions, so "
                "a requirement weakening cannot be injected without changing what is measured"
            ]
        )
    dropped_assertion = str((victim.get("assertions") or [])[-1].get("id"))

    def observed() -> dict[str, dict[str, Any]]:
        # Deep-copied: the arms mutate nested assertion lists, and a mutation
        # that leaked into the cached compilation would make a later check fail
        # for a reason that has nothing to do with the product.
        return copy.deepcopy(project.gates)

    blocking_gate_stops_blocking = observed()
    blocking_gate_stops_blocking[victim_id]["blocking"] = False

    assertion_removed = observed()
    assertion_removed[victim_id]["assertions"] = assertion_removed[victim_id]["assertions"][:-1]

    gate_deleted = observed()
    del gate_deleted[victim_id]

    arms_input = {
        "blocking_gate_observed_as_non_blocking": (
            blocking_gate_stops_blocking,
            victim_id,
            "no longer blocking",
        ),
        "assertion_removed_from_the_gate": (
            assertion_removed,
            f"{victim_id}.{dropped_assertion}",
            "assertion removed from the gate",
        ),
        "gate_absent_from_the_observed_set": (
            gate_deleted,
            victim_id,
            "absent from the observed gate set",
        ),
    }

    arms: dict[str, dict[str, Any]] = {}
    findings: list[str] = []
    for label, (gates, expected_subject, expected_detail) in arms_input.items():
        report = _scan(ctx.repo_root, observed_gates=gates)
        weakenings = _finding_bodies(report, DriftFinding.REQUIREMENT_WEAKENING)
        matching = [f for f in weakenings if f["subject"] == expected_subject]
        arms[label] = {
            "expected_subject": expected_subject,
            "expected_detail_fragment": expected_detail,
            "requirement_weakening_findings": weakenings,
            "matching_findings": matching,
            "blocks": [f["blocks"] for f in matching],
            **_report_summary(report),
        }
        if not matching:
            findings.append(
                f"{label}: no {DriftFinding.REQUIREMENT_WEAKENING} finding names "
                f"{expected_subject!r}; the findings were {weakenings}"
            )
            continue
        if not any(expected_detail in f["detail"] for f in matching):
            findings.append(
                f"{label}: the finding does not state the weakening (expected "
                f"{expected_detail!r}, got {[f['detail'] for f in matching]})"
            )
        if not all(f["blocks"] for f in matching):
            findings.append(f"{label}: the weakening was recorded as a non-blocking observation")
        if report.unresolved_scope_drift < 1:
            findings.append(
                f"{label}: unresolved_scope_drift is {report.unresolved_scope_drift}, so Section "
                "21.2's auto-merge requirement would not see the weakening"
            )
        if report.terminal_state is not ProjectState.FAILED_CONTRACT:
            findings.append(
                f"{label}: detected but not blocked -- the scan terminated at "
                f"{report.terminal_state.value}, not FAILED_CONTRACT"
            )

    # Negative control: the observed gate set is the compiled one, verbatim.
    control_report = _scan(ctx.repo_root, observed_gates=observed())
    control_weakenings = _finding_bodies(control_report, DriftFinding.REQUIREMENT_WEAKENING)
    if control_weakenings:
        findings.append(
            "negative control failed: the compiled gate set observed unchanged produced "
            f"{len(control_weakenings)} REQUIREMENT_WEAKENING findings: {control_weakenings}"
        )
    if control_report.blocking:
        findings.append(
            "negative control failed: an unweakened gate set produced blocking drift "
            f"{[f.as_body() for f in control_report.blocking]}"
        )
    if control_report.terminal_state is not ProjectState.RUNNING:
        findings.append(
            "negative control failed: an unweakened gate set terminated at "
            f"{control_report.terminal_state.value}"
        )

    execution_log = {
        "check": a.method or "negative_control_weaken_requirement",
        "expected": a.expected,
        "failure_state": a.failure_state,
        "gates_compiled": len(project.gates),
        "victim_gate": victim_id,
        "victim_gate_blocking_in_the_contract": bool(victim.get("blocking")),
        "victim_gate_assertion_ids": [str(x.get("id")) for x in victim.get("assertions") or []],
        "assertion_dropped_by_the_second_arm": dropped_assertion,
        "arms": arms,
        "what_blocked_means_here": (
            "Finding.blocks is true, DriftReport.unresolved_scope_drift -- the value Section "
            "21.2's auto_merge_requirements reads -- is non-zero, and DriftReport.terminal_state "
            "is FAILED_CONTRACT. A weakening that is recorded without reaching that state has "
            "been detected and not blocked."
        ),
    }
    negative_control = {
        "probe": "scan with the observed gate set equal to the compiled gate set, verbatim",
        "why": (
            "an engine that emitted REQUIREMENT_WEAKENING for every gate would satisfy the "
            "assertion's words and would block every merge forever. This arm must be silent for "
            "the three injected arms above to mean anything."
        ),
        "gates_observed": len(project.gates),
        "requirement_weakening_findings": control_weakenings,
        "blocking_findings": [f.as_body() for f in control_report.blocking],
        **_report_summary(control_report),
    }
    evidence = _evidence(ctx, gate, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"{victim_id} observed as non-blocking, with {dropped_assertion} removed, and absent "
            "altogether each produce a blocking REQUIREMENT_WEAKENING that drives the scan to "
            "FAILED_CONTRACT, while the unchanged gate set produces none"
        ),
    )


# ===========================================================================
# A3 — a change outside the allowed paths is rejected
# ===========================================================================


def _concrete_path_for(pattern: str) -> str:
    """A real-looking path underneath a glob taken from the pack.

    The engine matches with :func:`fnmatch.fnmatch`, so the probe has to be a
    path and not the pattern itself; deriving it from the pack's own pattern
    keeps the probe honest if the owner changes the policy.
    """
    return pattern.replace("**", "probe-d2-21.yaml").replace("*", "probe-d2-21")


def d2_21_a3(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``negative_control_write_outside_allowed_paths`` → ``rejected``.

    Two injections, matching the two branches the engine implements: a path that
    matches one of the work unit's *prohibited* patterns, and a path that
    matches none of its *allowed* ones. Both must be reported
    ``OUTSIDE_ALLOWED_PATHS``, both must block, and the scan must terminate at
    ``FAILED_CONTRACT`` -- "rejected" is not "mentioned in a report".

    A third arm is a discrimination test rather than a second injection. A
    sealed-asset path travels the same loop and must come back
    ``PROTECTED_ASSET_ACCESS``, not ``OUTSIDE_ALLOWED_PATHS``. Section 19.2's
    vocabulary is closed, and a detector that stamped one label on everything it
    disliked would pass A3 while making the label meaningless.

    The negative control is a change inside the work unit's own allowed paths,
    which must produce no path finding at all.

    Honest limit: the subject is the path policy the engine applies to a
    reported change set. This probe writes no file and reverts none; what is
    proven is that a change set containing such a path cannot pass the scan, not
    that a filesystem interceptor stopped the write.
    """
    project = _compiled(ctx.repo_root)
    engine = _engine(ctx.repo_root)
    task_id, plan_task = _reference_task(project)
    if not task_id:
        return bad(["the compiled plan carries no task with allowed paths to probe"])

    allowed = tuple(plan_task.get("allowed_paths") or ())
    prohibited = tuple(plan_task.get("prohibited_paths") or ())
    sealed = sorted(engine.sealed_names)
    findings: list[str] = []
    if not prohibited:
        findings.append(f"{task_id} declares no prohibited paths, so that branch cannot be probed")
    if not sealed:
        findings.append(
            "the compiled path policy names no sealed repositories, so PROTECTED_ASSET_ACCESS "
            "cannot be distinguished from OUTSIDE_ALLOWED_PATHS"
        )

    # Every prohibited pattern the work unit declares is probed, not just the
    # first: a policy is only enforced if all of it is. The expected finding
    # type is read off the pack rather than off the engine -- a prohibition
    # rooted at a sealed repository name is a protected asset, and Section 19.2
    # gives that its own type -- so this arm is a statement about the owner's
    # declaration and not a second copy of the engine's rule.
    arms_input: dict[str, tuple[str, str, str]] = {}
    for pattern in prohibited:
        rooted_at_a_sealed_repository = pattern.split("/", 1)[0] in engine.sealed_names
        arms_input[f"prohibited_pattern {pattern}"] = (
            _concrete_path_for(pattern),
            str(DriftFinding.PROTECTED_ASSET_ACCESS)
            if rooted_at_a_sealed_repository
            else str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
            "reaches a sealed asset"
            if rooted_at_a_sealed_repository
            else "matches a prohibited path pattern",
        )
    arms_input["matches_none_of_the_allowed_path_patterns"] = (
        UNGOVERNED_PATH,
        str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
        "outside the work unit's allowed paths",
    )
    if sealed:
        arms_input["reaches_a_sealed_asset"] = (
            f"{sealed[0]}/holdouts/probe-d2-21.py",
            str(DriftFinding.PROTECTED_ASSET_ACCESS),
            "reaches a sealed asset",
        )
    rejected_probes = [
        path
        for path, expected_type, _ in arms_input.values()
        if expected_type == str(DriftFinding.OUTSIDE_ALLOWED_PATHS)
    ]

    path_finding_types = {
        str(DriftFinding.OUTSIDE_ALLOWED_PATHS),
        str(DriftFinding.PROTECTED_ASSET_ACCESS),
    }
    arms: dict[str, dict[str, Any]] = {}
    for label, (path, expected_type, expected_detail) in arms_input.items():
        report = _scan(
            ctx.repo_root,
            active_tasks=[_active_task(project, task_id, changed_paths=(path,))],
        )
        typed = [f.as_body() for f in report.findings if f.finding == expected_type]
        other_path_types = sorted(
            {
                f.finding
                for f in report.findings
                if f.finding in path_finding_types and f.finding != expected_type
            }
        )
        arms[label] = {
            "changed_path": path,
            "expected_finding": expected_type,
            "expected_detail_fragment": expected_detail,
            "findings_of_the_expected_type": typed,
            "other_path_finding_types_produced": other_path_types,
            **_report_summary(report),
        }
        if not typed:
            findings.append(
                f"{label}: changed path {path!r} produced no {expected_type} finding; the types "
                f"found were {report.types_found()}"
            )
            continue
        if [f["subject"] for f in typed] != [task_id]:
            findings.append(
                f"{label}: {expected_type} was reported against {[f['subject'] for f in typed]}, "
                f"not against {task_id!r}"
            )
        if not any(expected_detail in f["detail"] for f in typed):
            findings.append(
                f"{label}: the finding does not state why the path was rejected (expected "
                f"{expected_detail!r}, got {[f['detail'] for f in typed]})"
            )
        if not all(path in f["evidence"] for f in typed):
            findings.append(
                f"{label}: the finding does not carry the offending path as evidence: {typed}"
            )
        if not all(f["blocks"] for f in typed):
            findings.append(f"{label}: the finding was recorded as a non-blocking observation")
        if report.terminal_state is not ProjectState.FAILED_CONTRACT:
            findings.append(
                f"{label}: detected but not rejected -- the scan terminated at "
                f"{report.terminal_state.value}, not FAILED_CONTRACT"
            )
        if other_path_types:
            findings.append(
                f"{label}: the same path also produced {other_path_types}; Section 19.2's "
                "finding types are not interchangeable"
            )

    # Negative control: a change inside the work unit's own allowed paths.
    inside = _concrete_path_for(allowed[0]) if allowed else ""
    control_report = _scan(
        ctx.repo_root,
        active_tasks=[_active_task(project, task_id, changed_paths=(inside,) if inside else ())],
    )
    control_outside = _finding_bodies(control_report, DriftFinding.OUTSIDE_ALLOWED_PATHS)
    control_protected = _finding_bodies(control_report, DriftFinding.PROTECTED_ASSET_ACCESS)
    if not inside:
        findings.append(f"{task_id} declares no allowed paths, so the control cannot be built")
    if control_outside:
        findings.append(
            f"negative control failed: a change to {inside!r}, which the work unit's own allowed "
            f"paths permit, was reported OUTSIDE_ALLOWED_PATHS ({control_outside})"
        )
    if control_protected:
        findings.append(
            f"negative control failed: a change to {inside!r} was reported "
            f"PROTECTED_ASSET_ACCESS ({control_protected})"
        )
    if control_report.blocking:
        findings.append(
            "negative control failed: a permitted change produced blocking drift "
            f"{[f.as_body() for f in control_report.blocking]}"
        )
    if control_report.terminal_state is not ProjectState.RUNNING:
        findings.append(
            "negative control failed: a permitted change terminated at "
            f"{control_report.terminal_state.value}"
        )

    execution_log = {
        "check": a.method or "negative_control_write_outside_allowed_paths",
        "expected": a.expected,
        "failure_state": a.failure_state,
        "work_unit": task_id,
        "allowed_paths": list(allowed),
        "prohibited_paths": list(prohibited),
        "sealed_repository_names_from_the_compiled_path_policy": sealed,
        "arms": arms,
        "what_rejected_means_here": (
            "a blocking OUTSIDE_ALLOWED_PATHS finding naming the offending path as evidence, and "
            "a scan that terminates at FAILED_CONTRACT. No file is written or reverted by this "
            "probe -- the subject is the path policy the engine applies to a reported change "
            "set, not a filesystem interceptor."
        ),
    }
    negative_control = {
        "probe": f"the same work unit changes {inside!r}, which its own allowed paths permit",
        "why": (
            "'a change outside the allowed paths is rejected' is satisfied by an engine that "
            "rejects every change ever reported. This arm must be silent, so the check "
            "distinguishes a path policy from a blanket refusal."
        ),
        "changed_path": inside,
        "outside_allowed_paths_findings": control_outside,
        "protected_asset_access_findings": control_protected,
        **_report_summary(control_report),
    }
    evidence = _evidence(ctx, gate, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            f"{len(rejected_probes)} paths {rejected_probes} are blocked as OUTSIDE_ALLOWED_PATHS "
            f"against {task_id} and drive the scan to FAILED_CONTRACT; the sealed paths among "
            f"{task_id}'s prohibitions are typed PROTECTED_ASSET_ACCESS instead, and a permitted "
            f"change to {inside} produces nothing"
        ),
    )


# ===========================================================================
# A4 — an unmapped security finding becomes an OUT_OF_SCOPE_OBSERVATION
# ===========================================================================


def _security_finding(**overrides: Any) -> security.SecurityFinding:
    """A well-formed security finding, complete unless an override removes something.

    Every arm starts from a submission that would block if it were in scope, so
    a demotion can only be attributed to the condition the override removes and
    never to some unrelated malformation.
    """
    params: dict[str, Any] = {
        "finding_id": "SEC-D2-21",
        "title": "the drift scan endpoint answers without authentication",
        "mapped_refs": (),
        "evidence": ("curl transcript: 200 from an unauthenticated request",),
        "executable_probe": "pytest tests/unit/test_drift.py -k security",
        "smallest_remediation": "require the existing service token on the drift scan endpoint",
        "severity": "high",
    }
    params.update(overrides)
    return security.SecurityFinding(**params)


def d2_21_a4(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``security_finding_classification_probe`` → ``classified_as_observation_and_does_not_block``.

    Two ways a finding fails to map, and the second is the one that matters. The
    first names no requirement at all. The second names ids that do not exist in
    the compiled catalog: a finding that could mint its own authority by
    inventing a reference would make Section 19.5's first condition
    unenforceable, so the approved set has to come from the compiled contract,
    and this arm proves it does.

    Both arms are followed through the whole engine, not just the classifier,
    because ``_apply_security_boundary`` is where an observation could still
    reach the blocking set by another door. What must hold at the end is the
    assertion's own second half: ``blocks`` is false, ``unresolved_scope_drift``
    is unchanged, and the project is still ``RUNNING``.

    Two negative controls. A fully qualified in-scope finding must *block* --
    otherwise "everything becomes an observation" would satisfy the assertion
    while disarming security review entirely. And an out-of-scope observation
    that proposes new requirements or tasks must be reported as
    ``OUT_OF_SCOPE_SECURITY_EXPANSION``, which is this assertion's own declared
    ``failure_state``: an observation that quietly becomes work is exactly the
    expansion Section 26 forbids.
    """
    engine = _engine(ctx.repo_root)
    approved = engine.approved_security_refs()
    if not approved:
        return bad(
            [
                "the compiled contract yields no approved requirement/threat/risk/policy "
                "references, so every security finding would be out of scope for a vacuous reason"
            ]
        )
    approved_ref = sorted(approved)[0]

    arms: dict[str, dict[str, Any]] = {}
    findings: list[str] = []
    out_of_scope = {
        "maps_to_nothing": _security_finding(finding_id="SEC-D2-21-UNMAPPED", mapped_refs=()),
        "maps_to_references_the_contract_does_not_contain": _security_finding(
            finding_id="SEC-D2-21-SELF-MINTED",
            mapped_refs=(PHANTOM_REQUIREMENT_ID, "THREAT-INVENTED-BY-THE-FINDING"),
        ),
    }
    for label, finding in out_of_scope.items():
        classification = security.classify(finding, approved)
        report = _scan(ctx.repo_root, security_findings=[finding])
        observations = _finding_bodies(report, DriftFinding.OUT_OF_SCOPE_OBSERVATION)
        expansions = _finding_bodies(report, DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION)
        arms[label] = {
            "submitted": finding.as_body(),
            "classification": classification.as_body(),
            "out_of_scope_observation_findings": observations,
            "out_of_scope_security_expansion_findings": expansions,
            **_report_summary(report),
        }
        if classification.blocks:
            findings.append(f"{label}: an unmapped finding was classified as blocking")
        if classification.finding_type != str(DriftFinding.OUT_OF_SCOPE_OBSERVATION):
            findings.append(
                f"{label}: classified as {classification.finding_type!r}, not "
                f"{DriftFinding.OUT_OF_SCOPE_OBSERVATION}"
            )
        if security.CONDITION_MAPPED not in classification.unsatisfied:
            findings.append(
                f"{label}: the demotion does not cite {security.CONDITION_MAPPED}: "
                f"{list(classification.unsatisfied)}"
            )
        if classification.admitted_refs:
            findings.append(
                f"{label}: references outside the approved set were admitted: "
                f"{list(classification.admitted_refs)}"
            )
        if [f["subject"] for f in observations] != [finding.finding_id]:
            findings.append(
                f"{label}: the scan reported OUT_OF_SCOPE_OBSERVATION for "
                f"{[f['subject'] for f in observations]}, not for {finding.finding_id!r}"
            )
        if any(f["blocks"] for f in observations):
            findings.append(f"{label}: the observation was recorded as blocking")
        if report.blocking:
            findings.append(
                f"{label}: an out-of-scope security finding produced blocking drift "
                f"{[f.as_body() for f in report.blocking]}"
            )
        if report.unresolved_scope_drift != 0:
            findings.append(
                f"{label}: unresolved_scope_drift rose to {report.unresolved_scope_drift}"
            )
        if report.terminal_state is not ProjectState.RUNNING:
            findings.append(
                f"{label}: an observation drove the project to {report.terminal_state.value}"
            )

    # Negative control 1: a fully qualified in-scope finding must block.
    in_scope = _security_finding(finding_id="SEC-D2-21-IN-SCOPE", mapped_refs=(approved_ref,))
    in_scope_classification = security.classify(in_scope, approved)
    in_scope_report = _scan(ctx.repo_root, security_findings=[in_scope])
    in_scope_observations = _finding_bodies(in_scope_report, DriftFinding.OUT_OF_SCOPE_OBSERVATION)
    if not in_scope_classification.blocks:
        findings.append(
            "negative control failed: a finding mapped to the approved reference "
            f"{approved_ref!r} with evidence and a stated smallest remediation did not block "
            f"({in_scope_classification.as_body()}). An engine that demotes everything satisfies "
            "A4's words and disarms security review."
        )
    if in_scope_observations:
        findings.append(
            "negative control failed: an in-scope finding was reported as an "
            f"OUT_OF_SCOPE_OBSERVATION ({in_scope_observations})"
        )
    if not in_scope_report.blocking:
        findings.append(
            "negative control failed: an in-scope security finding produced no blocking finding"
        )
    if in_scope_report.terminal_state is not ProjectState.FAILED_CONTRACT:
        findings.append(
            "negative control failed: an in-scope security finding left the project at "
            f"{in_scope_report.terminal_state.value}"
        )

    # Negative control 2: an observation that proposes work is an expansion.
    proposer = _security_finding(
        finding_id="SEC-D2-21-PROPOSES-WORK",
        mapped_refs=(),
        proposed_requirements=("REQ-NEW-D2-21",),
        proposed_tasks=("TSK-NEW-D2-21",),
    )
    proposer_report = _scan(ctx.repo_root, security_findings=[proposer])
    proposer_expansions = _finding_bodies(
        proposer_report, DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION
    )
    if not proposer_expansions:
        findings.append(
            "negative control failed: an out-of-scope observation proposing a new requirement "
            f"and a new task produced no {DriftFinding.OUT_OF_SCOPE_SECURITY_EXPANSION}; the "
            "observation silently became work"
        )
    if not proposer_report.blocking:
        findings.append(
            "negative control failed: a security-driven scope expansion did not block the build"
        )

    execution_log = {
        "check": a.method or "security_finding_classification_probe",
        "expected": a.expected,
        "failure_state": a.failure_state,
        "approved_reference_count": len(approved),
        "section_19_5_conditions": list(security.REQUIRED_CONDITIONS),
        "arms": arms,
        "where_the_approved_set_comes_from": (
            "DriftEngine.approved_security_refs(): the compiled requirement ids, the compiled "
            "gate ids, and the contract refs those requirements cite. It is never supplied by "
            "the finding, so a finding cannot mint its own authority."
        ),
    }
    negative_control = {
        "probe": (
            f"a fully qualified finding mapped to {approved_ref!r}, and an out-of-scope "
            "observation that proposes a new requirement and a new task"
        ),
        "why": (
            "'becomes an observation and does not block' is satisfied by an engine that demotes "
            "every security finding ever submitted -- which would disarm security review while "
            "passing the gate. The first arm must block. The second is this assertion's own "
            "failure_state: an observation that quietly turns into work is the expansion "
            "Section 19.5 exists to refuse."
        ),
        "in_scope_finding": {
            "submitted": in_scope.as_body(),
            "classification": in_scope_classification.as_body(),
            "blocking_findings": [f.as_body() for f in in_scope_report.blocking],
            "out_of_scope_observation_findings": in_scope_observations,
            **_report_summary(in_scope_report),
        },
        "observation_that_proposes_work": {
            "submitted": proposer.as_body(),
            "proposed_requirements": list(proposer.proposed_requirements),
            "proposed_tasks": list(proposer.proposed_tasks),
            "expansion_findings": proposer_expansions,
            **_report_summary(proposer_report),
        },
    }
    evidence = _evidence(ctx, gate, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            "a security finding mapping to nothing, and one mapping to references the compiled "
            "contract does not contain, both become non-blocking OUT_OF_SCOPE_OBSERVATIONs that "
            "leave the project RUNNING, while the same finding mapped to an approved reference "
            "blocks and an observation proposing new work is refused as an expansion"
        ),
    )


# ===========================================================================
# A5 — a blocking security finding carries all three Section 19.5 conditions
# ===========================================================================


def d2_21_a5(ctx: GateContext, gate: GateSpec, a: AssertionSpec) -> AssertionOutcome:
    """``blocking_finding_schema_assert`` → ``all_three_present``.

    The positive arm is a finding that satisfies all three of Section 19.5's
    conditions: it maps to an approved reference, it carries concrete evidence
    *and* an executable probe, and it states the smallest compliant remediation.
    ``blocking_schema_violations`` must return nothing for it, and ``admit``
    must place it in the blocking set with all three conditions satisfied.

    Then one negative control per condition, because "all three present" is a
    conjunction and a check that removed only one of them would leave two
    untested:

    * evidence and probe both absent → ``CONDITION_EVIDENCE`` missing;
    * no smallest remediation → ``CONDITION_REMEDIATION`` missing;
    * no mapped references → ``CONDITION_MAPPED`` missing.

    Each control must name exactly the condition it removed -- naming a
    different one, or naming several, would mean the schema assert cannot say
    what is wrong -- and each must be demoted out of the blocking set.

    Two further arms guard the wording. The claim is "evidence *or* an
    executable probe", so evidence-only and probe-only submissions must each
    still block; and a remediation of pure whitespace must not count as stated,
    or the third condition would be satisfiable with a space bar.
    """
    engine = _engine(ctx.repo_root)
    approved = engine.approved_security_refs()
    if not approved:
        return bad(["the compiled contract yields no approved reference for a finding to map to"])
    approved_ref = sorted(approved)[0]
    findings: list[str] = []

    complete = _security_finding(finding_id="SEC-D2-21-COMPLETE", mapped_refs=(approved_ref,))
    complete_missing = security.blocking_schema_violations(complete)
    complete_classification = security.classify(complete, approved)
    complete_report = security.admit([complete], approved)

    if complete_missing:
        findings.append(
            "a finding carrying all three Section 19.5 conditions was reported to be missing "
            f"{complete_missing}"
        )
    if not complete_classification.blocks:
        findings.append(
            f"a complete finding did not block: {complete_classification.as_body()}"
        )
    if set(complete_classification.satisfied) != set(security.REQUIRED_CONDITIONS):
        findings.append(
            f"a complete finding satisfied {list(complete_classification.satisfied)}, not all of "
            f"{list(security.REQUIRED_CONDITIONS)}"
        )
    if len(complete_report.blocking) != 1 or complete_report.observations:
        findings.append(
            "a complete finding was not admitted to the blocking set alone: "
            f"blocking={len(complete_report.blocking)}, "
            f"observations={len(complete_report.observations)}"
        )

    controls_input = {
        security.CONDITION_EVIDENCE: {"evidence": (), "executable_probe": None},
        security.CONDITION_REMEDIATION: {"smallest_remediation": None},
        security.CONDITION_MAPPED: {"mapped_refs": ()},
    }
    controls: dict[str, dict[str, Any]] = {}
    for condition, override in controls_input.items():
        params: dict[str, Any] = {
            "finding_id": f"SEC-D2-21-WITHOUT-{condition}",
            "mapped_refs": (approved_ref,),
        }
        params.update(override)
        broken = _security_finding(**params)
        missing = security.blocking_schema_violations(broken)
        classification = security.classify(broken, approved)
        report = security.admit([broken], approved)
        controls[condition] = {
            "removed": {k: (list(v) if isinstance(v, tuple) else v) for k, v in override.items()},
            "blocking_schema_violations": missing,
            "classification": classification.as_body(),
            "admitted_to_blocking": [c.as_body() for c in report.blocking],
            "demoted_to_observation": [c.as_body() for c in report.observations],
        }
        if missing != [condition]:
            findings.append(
                f"removing {condition} produced blocking_schema_violations {missing}; the schema "
                "assert must name exactly the condition that is absent"
            )
        if classification.blocks:
            findings.append(f"a finding missing {condition} still blocked")
        if condition not in classification.unsatisfied:
            findings.append(
                f"the demotion for a finding missing {condition} cites "
                f"{list(classification.unsatisfied)} instead"
            )
        if report.blocking or len(report.observations) != 1:
            findings.append(
                f"a finding missing {condition} was not demoted out of the blocking set: "
                f"blocking={len(report.blocking)}, observations={len(report.observations)}"
            )

    if set(controls_input) != set(security.REQUIRED_CONDITIONS):
        findings.append(
            f"Section 19.5 declares {list(security.REQUIRED_CONDITIONS)} but only "
            f"{sorted(controls_input)} have a negative control"
        )

    # The claim says "evidence *or* an executable probe". Both halves of that
    # disjunction must be sufficient on their own, or the condition being
    # measured is narrower than the one the contract states.
    disjunction: dict[str, dict[str, Any]] = {}
    for label, override in (
        ("evidence_only", {"executable_probe": None}),
        ("executable_probe_only", {"evidence": ()}),
    ):
        params = {"finding_id": f"SEC-D2-21-{label}", "mapped_refs": (approved_ref,)}
        params.update(override)
        variant = _security_finding(**params)
        missing = security.blocking_schema_violations(variant)
        classification = security.classify(variant, approved)
        disjunction[label] = {
            "blocking_schema_violations": missing,
            "blocks": classification.blocks,
        }
        if missing or not classification.blocks:
            findings.append(
                f"{label}: Section 19.5 accepts evidence *or* an executable probe, but this "
                f"submission was rejected with {missing}"
            )

    # A remediation field containing only whitespace is not a stated remediation.
    whitespace = _security_finding(
        finding_id="SEC-D2-21-WHITESPACE-REMEDIATION",
        mapped_refs=(approved_ref,),
        smallest_remediation="   \n\t ",
    )
    whitespace_missing = security.blocking_schema_violations(whitespace)
    whitespace_classification = security.classify(whitespace, approved)
    if whitespace_missing != [security.CONDITION_REMEDIATION] or whitespace_classification.blocks:
        findings.append(
            "a remediation of pure whitespace was accepted as a stated smallest remediation "
            f"({whitespace_missing}, blocks={whitespace_classification.blocks})"
        )

    execution_log = {
        "check": a.method or "blocking_finding_schema_assert",
        "expected": a.expected,
        "failure_state": a.failure_state,
        "section_19_5_conditions": list(security.REQUIRED_CONDITIONS),
        "approved_reference_used": approved_ref,
        "complete_finding": {
            "submitted": complete.as_body(),
            "blocking_schema_violations": complete_missing,
            "classification": complete_classification.as_body(),
            "admitted_to_blocking": [c.as_body() for c in complete_report.blocking],
        },
        "evidence_or_probe_disjunction": disjunction,
        "whitespace_remediation": {
            "blocking_schema_violations": whitespace_missing,
            "blocks": whitespace_classification.blocks,
        },
        "what_all_three_present_means_here": (
            "blocking_schema_violations() returns the missing members of the three-condition "
            "schema, so an empty list is the assertion's 'all_three_present'. The three "
            "conditions are read from drift.security.REQUIRED_CONDITIONS rather than restated "
            "here, so adding a fourth condition without a control surfaces as a finding."
        ),
    }
    negative_control = {
        "probe": "one submission per Section 19.5 condition, each missing exactly that condition",
        "why": (
            "'all three present' is a conjunction, and a single broken submission would leave "
            "two conditions unmeasured -- a schema assert that only ever checked remediation "
            "would pass. One control per condition is the smallest set that proves each is "
            "load-bearing, and each must name the condition it removed and nothing else."
        ),
        "conditions": list(security.REQUIRED_CONDITIONS),
        "controls": controls,
    }
    evidence = _evidence(ctx, gate, execution_log, negative_control)
    if findings:
        return bad(findings, evidence)
    return ok(
        evidence,
        (
            "a finding mapped to an approved reference with evidence, an executable probe and a "
            "stated smallest remediation reports zero schema violations and is admitted to the "
            f"blocking set; removing each of the {len(security.REQUIRED_CONDITIONS)} conditions "
            "in turn names exactly that condition and demotes the finding"
        ),
    )


# ===========================================================================
# Registry
# ===========================================================================

#: Merge into :data:`evaluation.checks.CHECKS` to register this gate.
CHECKS_D2_21: dict[tuple[str, str], Check] = {
    ("GATE-D2-21", "A1"): d2_21_a1,
    ("GATE-D2-21", "A2"): d2_21_a2,
    ("GATE-D2-21", "A3"): d2_21_a3,
    ("GATE-D2-21", "A4"): d2_21_a4,
    ("GATE-D2-21", "A5"): d2_21_a5,
}
