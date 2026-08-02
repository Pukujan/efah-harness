"""Durable run driver -- Contract Sections 10.3, 10.6, 10.7.

Ties the three pieces together: a registered graph (Section 10.2), the
checkpoint adapter (Section 10.3), and failure classification (Section 10.6).

The resume contract is the reason this class exists rather than callers using
``compiled.ainvoke`` directly. Section 10.6:

    Successful parallel nodes MUST not be rerun when another node fails if their
    outputs were checkpointed and remain valid.

LangGraph provides that: a resumed thread replays from the last checkpoint and
applies the pending writes of tasks that had already finished. What the runtime
adds is the discipline around it -- resume is only attempted after the failure
has been classified, and classes in ``NEVER_RETRY`` are not resumed at all,
because re-running work the contract has already invalidated is not recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from governance.states import FailureClass
from workflows.checkpoint import CheckpointRecord, SqliteCheckpointAdapter
from workflows.failures import NEVER_RETRY, RetryDecision, RetryPolicy, classify
from workflows.graphs import WorkflowServices, build
from workflows.state import WorkflowState, initial_state


@dataclass
class RunOutcome:
    """What one attempt produced. A failure is a result, not an exception."""

    graph_id: str
    thread_id: str
    completed: bool
    state: dict[str, Any] | None = None
    failure_class: FailureClass | None = None
    error: str = ""
    retry: RetryDecision | None = None
    resumed: bool = False
    node_log: list[str] = field(default_factory=list)


class WorkflowRuntime:
    """Runs and resumes a Section 10.2 graph against the Section 10.3 adapter."""

    def __init__(
        self,
        services: WorkflowServices,
        adapter: SqliteCheckpointAdapter,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.services = services
        self.adapter = adapter
        self.retry_policy = retry_policy or RetryPolicy()
        self._compiled: dict[str, Any] = {}

    def compiled(self, graph_id: str) -> Any:
        if graph_id not in self._compiled:
            self._compiled[graph_id] = build(graph_id, self.services).compile(
                checkpointer=self.adapter.saver()
            )
        return self._compiled[graph_id]

    def new_state(self, *, graph_id: str, work_unit_id: str, **overrides: Any) -> WorkflowState:
        pack = self.services.pack
        terminus = self.services.terminus
        params: dict[str, Any] = {
            "project_id": pack.project_id,
            "project_version": str(pack.yaml("project.yaml")["project"].get("contract_version", "1.0")),
            "contract_version": pack.contract_version,
            "terminus_database": terminus.database,
            "terminus_branch": terminus.branch,
            "terminus_commit": terminus.commit,
            "work_unit_id": work_unit_id,
            "graph_id": graph_id,
        }
        params.update(overrides)
        return initial_state(**params)

    async def run(
        self,
        graph_id: str,
        state: WorkflowState | None,
        *,
        thread_id: str,
        attempt: int = 1,
        resumed: bool = False,
    ) -> RunOutcome:
        """Invoke or resume ``graph_id`` on ``thread_id``.

        Passing ``state=None`` is LangGraph's resume signal: continue this
        thread from its checkpoint rather than starting over.
        """
        config = {"configurable": {"thread_id": thread_id}}
        try:
            result = await self.compiled(graph_id).ainvoke(state, config)
        # ``Exception``, not ``BaseException``: cancellation and KeyboardInterrupt
        # are the operator stopping the run, not a Section 10.6 failure class.
        except Exception as exc:  # noqa: BLE001 -- classification is the point
            failure_class = classify(exc)
            decision = self.retry_policy.decide(exc, attempt=attempt)
            return RunOutcome(
                graph_id=graph_id,
                thread_id=thread_id,
                completed=False,
                failure_class=failure_class,
                error=f"{type(exc).__name__}: {exc}",
                retry=decision,
                resumed=resumed,
            )
        return RunOutcome(
            graph_id=graph_id,
            thread_id=thread_id,
            completed=True,
            state=dict(result),
            resumed=resumed,
            node_log=list(result.get("node_log", [])),
        )

    async def resume(self, graph_id: str, *, thread_id: str, attempt: int = 2) -> RunOutcome:
        """Continue a thread from its checkpoint. No input, no reset."""
        return await self.run(graph_id, None, thread_id=thread_id, attempt=attempt, resumed=True)

    async def run_with_recovery(
        self,
        graph_id: str,
        state: WorkflowState,
        *,
        thread_id: str,
    ) -> list[RunOutcome]:
        """Run, classify, and resume within the retry budget.

        Returns every attempt, in order. The history is the evidence: an outcome
        list of length 2 whose second entry is ``resumed=True`` and
        ``completed=True`` is exactly GATE-D1-04's "resumes without restart".
        """
        attempts: list[RunOutcome] = []
        outcome = await self.run(graph_id, state, thread_id=thread_id, attempt=1)
        attempts.append(outcome)

        attempt = 1
        while not outcome.completed:
            decision = outcome.retry
            if decision is None or not decision.retry:
                break
            if decision.failure_class in NEVER_RETRY:
                break
            attempt += 1
            outcome = await self.resume(graph_id, thread_id=thread_id, attempt=attempt)
            attempts.append(outcome)
        return attempts

    async def checkpoints(self, thread_id: str) -> list[CheckpointRecord]:
        return await self.adapter.list_checkpoints(thread_id)
