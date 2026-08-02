"""OPTIONAL Claude worker adapter.

This is the **only** file in the repository permitted to reference a
Claude/Anthropic client (GATE-D1-07 ``ADAPTER_ALLOWLIST``, and the architecture
test's ``adapters`` exemption). Three rules govern it and all three are load
bearing:

* nothing may depend on it. It is not imported by the router, the gateway, the
  session layer, or the registry's construction path -- the registry imports it
  lazily and only when explicitly enabled;
* it must be swappable. It implements exactly the same
  :class:`~workers.adapters.base.WorkerAdapter` port as the LiteLLM adapter;
* disabling it must leave a working adapter behind (GATE-D1-07 A5). With
  ``EFAH_ENABLE_CLAUDE_ADAPTER`` unset -- the default, and the state of this
  build -- ``is_available()`` is ``False`` and the registry hands out the
  vendor-neutral LiteLLM adapter instead.

The SDK is an *optional* import. The default posture of this build is that the
``anthropic`` package is not installed and no Anthropic credential exists, and
that must remain a working configuration rather than an ImportError.
"""

from __future__ import annotations

import os
import time

from governance.states import TaskState
from models.errors import AdapterUnavailableError
from models.policy import ModelPolicy, load_environments, load_model_policy
from models.router import RoutingDecision
from workers.adapters.base import WorkerOutcome
from workers.session import WorkerSession, WorkUnit

try:  # pragma: no cover - absent by design in the vendor-neutral configuration
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

#: Opt-in only. Unset means "not present", which is the GATE-D1-07 posture.
ENABLE_FLAG = "EFAH_ENABLE_CLAUDE_ADAPTER"
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY")


class ClaudeCodeWorkerAdapter:
    """Optional adapter. Never the default, never a dependency."""

    name = "claude_code"

    def __init__(
        self,
        *,
        model: str | None = None,
        policy: ModelPolicy | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.policy = policy or load_model_policy()
        self._model = model or os.environ.get("EFAH_CLAUDE_ADAPTER_MODEL", "")
        # GATE-D1-05 A2: no model call bypasses the LiteLLM proxy. Even the
        # optional vendor adapter is pointed at the gateway's Anthropic-shaped
        # endpoint rather than at the vendor directly.
        self._base_url = base_url or self._default_base_url()
        self._api_key = api_key

    def _default_base_url(self) -> str:
        try:
            env = load_environments()["environments"]["dev"]["litellm_production"]
            return str(env["base_url"]).rstrip("/")
        except Exception:
            return ""

    def is_enabled(self) -> bool:
        return os.environ.get(ENABLE_FLAG, "").lower() in {"1", "true", "yes"}

    def has_credential(self) -> bool:
        if self._api_key or any(os.environ.get(name) for name in CREDENTIAL_VARS):
            return True
        # Routed through the gateway, the proxy credential is the credential.
        return bool(self._base_url and os.environ.get("LITELLM_MASTER_KEY"))

    def is_available(self) -> bool:
        return bool(self.is_enabled() and anthropic is not None and self.has_credential() and self._model)

    def unavailable_reason(self) -> str:
        if not self.is_enabled():
            return f"{ENABLE_FLAG} is not set; the Claude adapter is disabled"
        if anthropic is None:
            return "the anthropic SDK is not installed"
        if not self.has_credential():
            return "no Anthropic credential is present"
        if not self._model:
            return "no model configured (set EFAH_CLAUDE_ADAPTER_MODEL)"
        return ""

    async def execute(self, work_unit: WorkUnit, decision: RoutingDecision) -> WorkerOutcome:
        if not self.is_available():
            raise AdapterUnavailableError(
                f"claude_code adapter is unavailable: {self.unavailable_reason()}"
            )

        session = WorkerSession.open(
            work_unit, alias=decision.alias, session_policy=self.policy.session_policy
        )
        messages = session.messages()
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        turns = [m for m in messages if m["role"] != "system"]

        client = anthropic.AsyncAnthropic(  # type: ignore[union-attr]
            base_url=self._base_url or None,
            api_key=self._api_key or os.environ.get("LITELLM_MASTER_KEY") or None,
            max_retries=decision.max_retries,
            timeout=float(decision.timeout_seconds),
        )
        started = time.perf_counter()
        response = await client.messages.create(
            model=self._model,
            max_tokens=max(work_unit.max_tokens, decision.max_tokens_floor),
            system=system or anthropic.NOT_GIVEN,  # type: ignore[union-attr]
            messages=turns,
            timeout=float(decision.timeout_seconds),
        )
        latency = time.perf_counter() - started

        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        session.record_turn("assistant", text)
        session.close()
        return WorkerOutcome(
            task_id=work_unit.task_id,
            role=decision.role,
            alias=decision.alias,
            gateway=decision.gateway,
            adapter=self.name,
            session_id=session.session_id,
            state=TaskState.CANDIDATE_COMPLETE,
            text=text,
            usage={"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
            latency_seconds=round(latency, 3),
            configuration_version=decision.configuration_version,
        )
