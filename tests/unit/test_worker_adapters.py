"""T-033 — fresh bounded sessions and the vendor-neutral worker adapter.

Contract Section 10.5 and GATE-D1-05: fresh per-invocation sessions, no
persistent conversational memory, context bounded by the work unit.
GATE-D1-07 A3/A5: the LiteLLM adapter is the primary one, it works with every
Anthropic credential unset, and disabling the optional Claude adapter leaves it
working.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from governance.states import TaskState
from integrations.secrets import SecretResolver
from models.availability import CapabilityRegistry, ModelCapability
from models.errors import (
    AdapterUnavailableError,
    FailedOracleError,
    SessionReuseError,
)
from models.gateway import LiteLLMGateway
from models.policy import load_model_policy
from models.router import ModelRouter, RoutingRequest
from models.throttle import GlobalThrottle
from workers.adapters.base import WorkerAdapter, WorkerOutcome
from workers.adapters.litellm_worker import LiteLLMWorkerAdapter
from workers.registry import WorkerAdapterRegistry, build_registry
from workers.session import WorkerSession, WorkUnit

SRC = Path(__file__).resolve().parents[2] / "src"

FAKE_ENV = {
    "LITELLM_MASTER_KEY": "sk-test-production",
    "LITELLM_EVAL_MASTER_KEY": "sk-test-eval",
}


def json_response(text: str = "OK", tool_calls=None) -> httpx.Response:
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        },
    )


def sse_response(text: str = "OK", tool_calls=None) -> httpx.Response:
    """An SSE body, optionally carrying tool-call deltas.

    Tool calls arrive as indexed deltas, not as a whole message, and
    ``LiteLLMGateway._post_stream`` reassembles them by index. A double that
    returns a plain JSON message would let the adapter pass a test it could not
    pass in production — which is what happened when streaming became the
    default for tool work.
    """
    chunks: list[dict] = []
    if tool_calls:
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": i, "id": tc["id"], "type": "function",
             "function": {"name": tc["function"]["name"],
                          "arguments": tc["function"]["arguments"]}}
            for i, tc in enumerate(tool_calls)
        ]}}]})
    if text:
        chunks.append({"choices": [{"index": 0, "delta": {"content": text}}]})
    chunks.append({"choices": [{"index": 0, "delta": {},
                                "finish_reason": "tool_calls" if tool_calls else "stop"}],
                   "usage": {"total_tokens": 13}})
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def make_gateway(handler=None, tmp_path=None, **kwargs) -> LiteLLMGateway:
    def default_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return sse_response() if body.get("stream") else json_response()

    return LiteLLMGateway(
        resolver=SecretResolver(environ=dict(FAKE_ENV)),
        transport=httpx.MockTransport(handler or default_handler),
        throttle=GlobalThrottle(
            max_requests_per_minute=90,
            min_interval_seconds=0.0,
            state_path=(tmp_path or Path("/tmp")) / "throttle-test.json",
        ),
        require_eval_preflight=False,
        **kwargs,
    )


@pytest.fixture
def policy():
    return load_model_policy()


@pytest.fixture
def router(policy):
    registry = CapabilityRegistry()
    for row in policy.roles.values():
        registry.record(ModelCapability(alias=row.alias, gateway=row.gateway, available=True))
    return ModelRouter(policy=policy, capabilities=registry)


@pytest.fixture
def work_unit():
    return WorkUnit(
        task_id="T-101",
        role="implementer",
        instructions="Implement the function described by the failing test.",
        inputs={"requirement_id": "R-014"},
        max_tokens=512,
    )


# -- sessions: Section 10.5 / GATE-D1-05 ----------------------------------
def test_a_new_session_has_no_prior_turns(work_unit):
    session = WorkerSession.open(work_unit, alias="implementer-i12")
    assert session.prior_turn_count == 0
    assert session.turn_count == 0


def test_each_invocation_gets_a_distinct_session(work_unit):
    a = WorkerSession.open(work_unit, alias="implementer-i12")
    b = WorkerSession.open(work_unit, alias="implementer-i12")
    assert a.session_id != b.session_id
    a.record_turn("assistant", "first answer")
    assert b.turn_count == 0


def test_a_closed_session_cannot_be_reused(work_unit):
    session = WorkerSession.open(work_unit, alias="implementer-i12")
    session.record_turn("assistant", "answer")
    summary = session.close()
    assert summary["turns"] == 1
    with pytest.raises(SessionReuseError):
        session.messages()
    with pytest.raises(SessionReuseError):
        session.record_turn("user", "and another thing")


def test_context_is_bounded_by_the_work_unit(work_unit):
    session = WorkerSession.open(work_unit, alias="implementer-i12")
    rendered = " ".join(m["content"] for m in session.messages())
    assert "R-014" in rendered
    assert "project" not in rendered.lower()


def test_the_transcript_is_not_retained_after_close(work_unit):
    session = WorkerSession.open(work_unit, alias="implementer-i12")
    session.record_turn("assistant", "secret reasoning")
    session.close()
    assert session._turns == []


def test_disabling_fresh_sessions_in_the_pack_is_refused(work_unit, policy):
    from dataclasses import replace

    broken = replace(policy.session_policy, fresh_per_invocation_worker_sessions=False)
    with pytest.raises(SessionReuseError):
        WorkerSession.open(work_unit, alias="implementer-i12", session_policy=broken)


# -- the vendor-neutral adapter -------------------------------------------
async def test_litellm_adapter_completes_a_work_unit(router, work_unit, tmp_path):
    gateway = make_gateway(tmp_path=tmp_path)
    adapter = LiteLLMWorkerAdapter(gateway)
    decision = router.route(RoutingRequest(role="implementer"))
    outcome = await adapter.execute(work_unit, decision)
    await gateway.aclose()

    assert isinstance(outcome, WorkerOutcome)
    assert outcome.state is TaskState.CANDIDATE_COMPLETE
    assert outcome.text == "OK"
    assert outcome.alias == "implementer-i12"
    assert outcome.adapter == "litellm"
    assert outcome.configuration_hash.startswith("sha256:")
    assert outcome.input_hash.startswith("sha256:")
    assert outcome.output_hash.startswith("sha256:")


async def test_a_worker_never_claims_a_gate_only_state(router, work_unit, tmp_path):
    """Section 9.3: workers submit CANDIDATE_COMPLETE; only gates produce PASSED."""
    from governance.states import GATE_ONLY_STATES, WORKER_SUBMITTABLE_STATES

    gateway = make_gateway(tmp_path=tmp_path)
    adapter = LiteLLMWorkerAdapter(gateway)
    outcome = await adapter.execute(work_unit, router.route(RoutingRequest(role="implementer")))
    await gateway.aclose()
    assert outcome.state in WORKER_SUBMITTABLE_STATES
    assert outcome.state not in GATE_ONLY_STATES


async def test_the_adapter_sends_the_real_model_id_but_returns_only_the_alias(
    router, work_unit, tmp_path
):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return sse_response()

    gateway = make_gateway(handler, tmp_path=tmp_path)
    outcome = await LiteLLMWorkerAdapter(gateway).execute(
        work_unit, router.route(RoutingRequest(role="implementer"))
    )
    await gateway.aclose()
    assert seen["model"] == "gpt-5.6-luna"  # the wire, which is not agent-facing
    assert "gpt" not in json.dumps(outcome.as_body())


async def test_the_adapter_works_with_every_anthropic_credential_unset(
    router, work_unit, tmp_path, monkeypatch
):
    """GATE-D1-07 A2/A5: this is the credential-stripped configuration."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    gateway = make_gateway(tmp_path=tmp_path)
    outcome = await LiteLLMWorkerAdapter(gateway).execute(
        work_unit, router.route(RoutingRequest(role="implementer"))
    )
    await gateway.aclose()
    assert outcome.succeeded


async def test_a_gateway_failure_is_classified_not_swallowed(router, work_unit, tmp_path):
    gateway = make_gateway(
        lambda request: httpx.Response(429, text="rate limit"), tmp_path=tmp_path
    )
    outcome = await LiteLLMWorkerAdapter(gateway).execute(
        work_unit, router.route(RoutingRequest(role="implementer"))
    )
    await gateway.aclose()
    assert outcome.state is TaskState.REWORK_REQUIRED
    assert outcome.failure_class == "RATE_LIMIT"


# -- min_max_tokens_for_tool_calls: FAILED_ORACLE --------------------------
async def test_a_tool_call_below_the_token_floor_is_failed_oracle(router, tmp_path, policy):
    """A budget under 512 truncates reasoning models before they emit tool
    calls and records a false negative on tool support."""
    tool = {
        "type": "function",
        "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}},
    }
    gateway = make_gateway(tmp_path=tmp_path)
    with pytest.raises(FailedOracleError):
        await gateway.chat_completion(
            role="implementer",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=policy.request_policy.min_max_tokens_for_tool_calls - 1,
            tools=[tool],
        )
    await gateway.aclose()


async def test_the_hard_floor_applies_even_without_tools(router, tmp_path, policy):
    gateway = make_gateway(tmp_path=tmp_path)
    with pytest.raises(FailedOracleError):
        await gateway.chat_completion(
            role="implementer",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=policy.request_policy.hard_floor_max_tokens - 1,
        )
    await gateway.aclose()


async def test_the_adapter_raises_the_budget_to_the_floor_for_tool_work(router, tmp_path):
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        # Streaming is the production default for tool work now
        # (STREAMING-DISPATCH-FINDING-2026-07-19), so the double speaks SSE.
        return sse_response(
            "", tool_calls=[{"id": "c1", "type": "function",
                             "function": {"name": "noop", "arguments": "{}"}}]
        )

    tool = {
        "type": "function",
        "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}},
    }
    work_unit = WorkUnit(
        task_id="T-2", role="implementer", instructions="call noop", max_tokens=64, tools=(tool,)
    )
    gateway = make_gateway(handler, tmp_path=tmp_path)
    outcome = await LiteLLMWorkerAdapter(gateway).execute(
        work_unit, router.route(RoutingRequest(role="implementer"))
    )
    await gateway.aclose()
    assert sent["max_tokens"] == 512
    assert outcome.tool_calls


# -- registry: GATE-D1-07 A3/A5 -------------------------------------------
def test_the_default_adapter_is_the_vendor_neutral_one(tmp_path):
    gateway = make_gateway(tmp_path=tmp_path)
    registry = build_registry(gateway)
    assert registry.default().name == "litellm"
    assert "litellm" in registry.available()


def test_disabling_the_claude_adapter_leaves_a_working_adapter(tmp_path, monkeypatch):
    """GATE-D1-07 A5, stated as a test rather than as a claim."""
    monkeypatch.setenv("EFAH_ENABLE_CLAUDE_ADAPTER", "1")
    gateway = make_gateway(tmp_path=tmp_path)
    with_optional = build_registry(gateway)
    assert with_optional.default().name == "litellm"

    without_optional = build_registry(gateway, include_optional_vendor_adapters=False)
    assert without_optional.names() == ["litellm"]
    assert without_optional.default().name == "litellm"


def test_the_claude_adapter_is_unavailable_by_default(monkeypatch):
    monkeypatch.delenv("EFAH_ENABLE_CLAUDE_ADAPTER", raising=False)
    from workers.adapters.claude_code import ClaudeCodeWorkerAdapter

    adapter = ClaudeCodeWorkerAdapter()
    assert adapter.is_available() is False
    assert "disabled" in adapter.unavailable_reason()


async def test_the_disabled_claude_adapter_refuses_to_run(monkeypatch, router, work_unit):
    monkeypatch.delenv("EFAH_ENABLE_CLAUDE_ADAPTER", raising=False)
    from workers.adapters.claude_code import ClaudeCodeWorkerAdapter

    with pytest.raises(AdapterUnavailableError):
        await ClaudeCodeWorkerAdapter().execute(
            work_unit, router.route(RoutingRequest(role="implementer"))
        )


def test_an_empty_registry_fails_loudly():
    with pytest.raises(AdapterUnavailableError):
        WorkerAdapterRegistry().default()


def test_both_adapters_satisfy_the_same_port(tmp_path):
    from workers.adapters.claude_code import ClaudeCodeWorkerAdapter

    gateway = make_gateway(tmp_path=tmp_path)
    assert isinstance(LiteLLMWorkerAdapter(gateway), WorkerAdapter)
    assert isinstance(ClaudeCodeWorkerAdapter(), WorkerAdapter)


# -- vendor neutrality of the import graph --------------------------------
def test_only_the_claude_adapter_mentions_a_vendor_sdk():
    """The A1/A3 property, asserted from the test suite as well as the gate."""
    forbidden = {"anthropic", "claude_agent_sdk", "claude_code_sdk", "claude"}
    allowed = SRC / "workers" / "adapters" / "claude_code.py"
    offenders = []
    for path in SRC.rglob("*.py"):
        if path == allowed:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
        if roots & forbidden:
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []
