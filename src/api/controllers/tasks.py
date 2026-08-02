"""Task use cases (contract Sections 9.3, 10.6, 11.5)."""

from __future__ import annotations

from datetime import UTC, datetime

from api.context import RequestContext
from api.errors import GateBypassRejected, NotFound
from api.ports import ControlPlaneReadPort, ControlPlaneWritePort, RuntimePort
from api.state import DecisionRecord, RunHandle, TaskRecord
from dashboard.projections import derived_durations
from governance.envelope import CONTRACT_VERSION
from governance.states import GATE_ONLY_STATES, TaskState
from observability.spans import Correlation, SpanKindName, efah_span


class TaskController:
    """``GET /tasks/{id}`` and ``POST /tasks/{id}/resume``."""

    def __init__(
        self,
        *,
        reader: ControlPlaneReadPort,
        writer: ControlPlaneWritePort,
        runtime: RuntimePort,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._runtime = runtime

    def get(self, *, task_id: str) -> dict:
        """The task plus its Section 9.8 derived durations.

        Durations are computed from the recorded system events on the way out.
        No agent estimate exists to be displayed, because the read model has no
        field to hold one.
        """
        task = self._reader.get_task(task_id)
        if task is None:
            raise NotFound(f"task {task_id} is not in the ledger")
        payload = task.model_dump(mode="json")
        payload["derived_durations"] = derived_durations(task.timing)
        payload["work_unit_durations"] = {
            unit.work_unit_id: derived_durations(unit.timing) for unit in task.work_units
        }
        return payload

    def resume(
        self,
        *,
        task_id: str,
        context: RequestContext,
        reason: str = "",
        owner_answer: str | None = None,
    ) -> RunHandle:
        """Section 10.6: resume from the checkpoint.

        If the caller supplies an owner answer, it is recorded as a Decision
        bound to the governing contract version *before* the resume is
        dispatched -- so the answer exists in the record even if the resume then
        fails, which is the ordering Section 18 requires.
        """
        task = self._reader.get_task(task_id)
        if task is None:
            raise NotFound(f"task {task_id} is not in the ledger")

        # Section 9.3: "Only gates may produce PASSED." A resume cannot be used
        # to walk a task into a gate-only state.
        if task.state in GATE_ONLY_STATES:
            raise GateBypassRejected(
                f"task {task_id} is {task.state}; resuming a gate-owned state would "
                "bypass the gate that produced it"
            )
        if task.state in {TaskState.CANCELED, TaskState.CLOSED}:
            raise GateBypassRejected(f"task {task_id} is {task.state} and cannot be resumed")

        if owner_answer:
            self._writer.record_decision(
                DecisionRecord(
                    decision_id=f"OWNER-{task_id}-{context.request_id[:8]}",
                    title=f"Owner answer for {task_id}",
                    outcome="answered",
                    decided_by=context.principal.subject,
                    decided_at=datetime.now(UTC).isoformat(),
                    contract_version=CONTRACT_VERSION,
                    rationale=owner_answer,
                )
            )

        with efah_span(
            "task.resume",
            kind=SpanKindName.TASK,
            correlation=Correlation(
                project_id=task.project_id, task_id=task_id, run_id=context.request_id
            ),
            attributes={"reason": reason},
        ):
            return self._runtime.resume_task(
                task_id=task_id,
                project_id=task.project_id,
                requested_by=context.principal.subject,
                correlation_id=context.correlation_id,
            )

    def record(self, task: TaskRecord) -> TaskRecord:
        """Ingest point used by the compiler and runtime workstreams."""
        upsert = getattr(self._writer, "upsert_task", None)
        if upsert is None:
            raise NotFound("the configured control plane does not accept task ingestion")
        return upsert(task)
