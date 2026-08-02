"""The primary worker adapter: vendor-neutral, over the LiteLLM proxy.

This is the adapter the harness depends on. It speaks the proxy's OpenAI-shaped
HTTP API and therefore reaches every family the owner has configured -- OpenAI,
Google, DeepSeek, xAI, Qwen, Anthropic, Zhipu -- without importing any vendor
SDK. It runs with every Anthropic credential unset, which is the property
GATE-D1-07 exists to prove.

Responsibilities, in order:

1. open a **fresh** bounded session (Section 10.5, GATE-D1-05 A1/A3);
2. check the outgoing prompt is blinded (GATE-D1-06 A1) -- a worker learns its
   own alias and nothing about anybody's vendor;
3. resolve the alias to a real model id through the **protected identity store**
   (Section 11.2). This is the dispatch boundary; the identity goes onto the wire
   and into a configuration hash, never into a record or a payload;
4. dispatch through the gateway the router chose, under the account-wide
   throttle and above the tool-call token floor;
5. classify any failure into an existing ``FailureClass`` (Section 10.6) and
   return a typed outcome. Workers may only submit ``CANDIDATE_COMPLETE``
   (Section 9.3) -- this adapter never claims ``PASSED``.
"""

from __future__ import annotations

from typing import Any

from governance.states import TaskState
from models.blinding import (
    PackIdentityStore,
    ProtectedIdentityStore,
    assert_task_payload_blinded,
)
from models.errors import ModelPolicyError
from models.gateway import LiteLLMGateway
from models.policy import ModelPolicy, load_model_policy
from models.router import RoutingDecision
from workers.adapters.base import WorkerOutcome
from workers.session import WorkerSession, WorkUnit


class LiteLLMWorkerAdapter:
    """Vendor-neutral worker adapter. The default in every registry."""

    name = "litellm"

    def __init__(
        self,
        gateway: LiteLLMGateway,
        *,
        identity_store: ProtectedIdentityStore | None = None,
        policy: ModelPolicy | None = None,
    ) -> None:
        self.gateway = gateway
        self.policy = policy or gateway.policy or load_model_policy()
        self.identity_store = identity_store or PackIdentityStore(self.policy)

    def is_available(self) -> bool:
        """Usable whenever a gateway credential resolves. No vendor SDK needed."""
        try:
            return bool(self.gateway.api_key("production")) or bool(self.gateway.api_key("eval"))
        except Exception:  # noqa: BLE001 - a missing credential means unavailable
            return False

    async def execute(self, work_unit: WorkUnit, decision: RoutingDecision) -> WorkerOutcome:
        if work_unit.role != decision.role:
            raise ModelPolicyError(
                f"work unit role {work_unit.role!r} does not match routing decision role "
                f"{decision.role!r}"
            )

        session = WorkerSession.open(
            work_unit, alias=decision.alias, session_policy=self.policy.session_policy
        )
        messages = session.messages()
        # GATE-D1-06 A1: nothing vendor-identifying leaves for a worker.
        assert_task_payload_blinded(
            {"messages": messages, "routing": decision.as_body()}, self.policy
        )

        identity = await _resolve(self.identity_store, decision.alias)
        max_tokens = max(work_unit.max_tokens, decision.max_tokens_floor if work_unit.tools else 0)

        try:
            response = await self.gateway.chat_completion(
                role=decision.role,
                messages=messages,
                max_tokens=max_tokens,
                model=identity.litellm_model,
                tools=list(work_unit.tools) or None,
                stream=self.policy.request_policy.prefer_streaming and not work_unit.tools,
            )
        except ModelPolicyError as exc:
            session.close()
            return WorkerOutcome(
                task_id=work_unit.task_id,
                role=decision.role,
                alias=decision.alias,
                gateway=decision.gateway,
                adapter=self.name,
                session_id=session.session_id,
                state=TaskState.REWORK_REQUIRED,
                configuration_version=decision.configuration_version,
                input_hash=work_unit.input_hash,
                failure_class=str(exc.typed_state),
                detail=str(exc)[:500],
            )

        session.record_turn("assistant", response.text)
        session.close()
        return WorkerOutcome(
            task_id=work_unit.task_id,
            role=decision.role,
            alias=decision.alias,
            gateway=decision.gateway,
            adapter=self.name,
            session_id=session.session_id,
            state=TaskState.CANDIDATE_COMPLETE,
            text=response.text,
            tool_calls=response.tool_calls,
            usage=response.usage,
            latency_seconds=round(response.latency_seconds, 3),
            throttle_wait_seconds=response.throttle_wait_seconds,
            configuration_version=decision.configuration_version,
            configuration_hash=response.configuration_hash,
            input_hash=response.input_hash,
            output_hash=response.output_hash,
        )


async def _resolve(store: ProtectedIdentityStore, alias: str) -> Any:
    """Resolve an alias through the protected store.

    WS-B's TerminusDB adapter takes ``resolve_alias(alias)``; the pack-backed
    store additionally accepts a ``caller`` so it can refuse an unprivileged
    reveal. Support both without requiring either.
    """
    try:
        return await store.resolve_alias(alias, caller="dispatch_service")  # type: ignore[call-arg]
    except TypeError:
        return await store.resolve_alias(alias)
