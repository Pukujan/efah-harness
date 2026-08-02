"""The two LiteLLM deployments, and the split between them (DEC-002).

The owner runs the same binary twice with opposite policies:

===========  =====================================================================
production   ``num_retries`` 5+5, up to 3 pooled routes per alias, cooldowns on,
             ``drop_params: true``, ``max_parallel_requests: 8``
eval         ``num_retries`` 0, exactly one route per alias, cooldowns disabled,
             ``drop_params: false``, no queueing, DB-less, separate master key
===========  =====================================================================

Routing a gate-bearing role to production does not fail -- it *succeeds*, with a
provenance record that says something untrue. Retries mean the recorded run is
not the run that happened; pooling re-rolls onto another upstream key without
touching ``num_retries``; cooldowns replace the true upstream error with a router
error; ``drop_params`` silently strips ``reasoning_effort``. Contract Section 18
requires the recorded configuration to be the one that ran, so DEC-002 makes the
split binding and the violation ``FAILED_PROVENANCE``.

Three obligations are implemented here rather than documented:

1. **Gateway selection is derived, never passed in.** ``client_for_role`` reads
   the pack. An explicit ``gateway=`` argument that disagrees raises.
2. **Client-side zero retry.** The proxy cannot stop a client from retrying, and
   both the OpenAI and Anthropic SDKs default to ``max_retries=2`` while
   ``urllib3.Retry`` and most ``HTTPAdapter`` presets retry by default. The eval
   client is built on its own ``httpx`` transport with ``retries=0`` and a 120s
   timeout, and it is a *separate object* from the production client -- a shared
   session would void the guarantee from outside the proxy.
3. **Preflight before an evaluation campaign.** DEC-002 requires
   ``validate_eval_config`` to pass first; until it has, eval dispatch raises.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Sequence

import httpx

from governance.envelope import content_hash, utc_now
from integrations.secrets import SecretRef, SecretResolver
from models.errors import (
    FailedOracleError,
    FailedProvenanceError,
    GatewayRequestError,
    ModelUnavailableError,
    RateLimitError,
    TransientProviderError,
)
from models.policy import ModelPolicy, load_environments, load_model_policy
from models.throttle import GlobalThrottle

#: The route the eval deployment carries specifically so a preflight can prove
#: it fails fast instead of silently falling back (DEC-002 "Preflight obligation").
CANARY_MODEL = "__canary_invalid"

#: Measured 1.22s for the canary on the eval gateway; with 5 retries at
#: ``retry_after: 2`` the same failure takes >= 10s. The bound is deliberately
#: generous -- it separates "no retries" from "retries", not fast from slow.
CANARY_FAST_FAIL_SECONDS = 5.0

#: How long a passing preflight authorises eval dispatch.
PREFLIGHT_VALIDITY_SECONDS = 3600.0


class GatewayClass(StrEnum):
    PRODUCTION = "production"
    EVAL = "eval"


@dataclass(frozen=True)
class GatewayEndpoint:
    name: str
    gateway_class: GatewayClass
    base_url: str
    api_key_ref: str
    max_retries: int
    timeout_seconds: int
    valid_for_evidence: bool

    @property
    def is_eval(self) -> bool:
        return self.gateway_class is GatewayClass.EVAL


@dataclass
class ChatResponse:
    """A completed model call, reduced to what provenance needs.

    The upstream model name is *not* retained. Section 18 wants a configuration
    hash, and ``configuration_hash`` covers the real model id and every request
    parameter without carrying the identity itself into a record an agent may
    read (Section 12.3).
    """

    role: str
    alias: str
    gateway: str
    http_status: int
    latency_seconds: float
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    configuration_hash: str = ""
    input_hash: str = ""
    output_hash: str = ""
    throttle_wait_seconds: float = 0.0
    streamed: bool = False
    completed_at: str = field(default_factory=utc_now)

    def as_body(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "alias": self.alias,
            "gateway": self.gateway,
            "http_status": self.http_status,
            "latency_seconds": self.latency_seconds,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "configuration_hash": self.configuration_hash,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "throttle_wait_seconds": self.throttle_wait_seconds,
            "streamed": self.streamed,
            "completed_at": self.completed_at,
            "emitted_tool_call": bool(self.tool_calls),
        }


@dataclass(frozen=True)
class CanaryResult:
    """Outcome of the DEC-002 live preflight probe."""

    base_url: str
    http_status: int | None
    elapsed_seconds: float
    fast: bool
    errored: bool
    detail: str

    @property
    def passed(self) -> bool:
        return self.errored and self.fast


def transport_retries(client: httpx.AsyncClient) -> int | None:
    """Read the retry count out of a live httpx client's connection pool.

    Introspecting the real object matters: DEC-002's client-side obligation is
    about what the transport will actually do, not about what we intended when
    we constructed it. Returns ``None`` for a transport with no pool (a test
    ``MockTransport``, which never retries).
    """
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    return getattr(pool, "_retries", None)


def gateway_class_for_role(role: str, policy: ModelPolicy | None = None) -> GatewayClass:
    """DEC-002 role -> gateway class, read from the pack."""
    policy = policy or load_model_policy()
    return GatewayClass(policy.gateway_routing.gateway_for_role(role))


def assert_gateway_for_role(
    role: str, gateway: GatewayClass | str, policy: ModelPolicy | None = None
) -> GatewayClass:
    """Raise ``FAILED_PROVENANCE`` if *role* may not use *gateway*."""
    policy = policy or load_model_policy()
    required = gateway_class_for_role(role, policy)
    actual = GatewayClass(gateway)
    if actual is not required:
        detail = (
            "gate-bearing role on the production gateway: retries, pooling, cooldowns and "
            "drop_params would silently falsify the recorded run"
            if required is GatewayClass.EVAL
            else "candidate role on the eval gateway"
        )
        raise FailedProvenanceError(
            f"DEC-002: role {role!r} must use the {required.value!r} gateway, not {actual.value!r}"
            f" -- {detail}"
        )
    return required


class LiteLLMGateway:
    """Owns both endpoints and every outbound model request.

    Every call passes through the account-wide throttle and the ``max_tokens``
    floor check, because this is the only place they can be enforced once.
    """

    def __init__(
        self,
        *,
        policy: ModelPolicy | None = None,
        environments: dict[str, Any] | None = None,
        resolver: SecretResolver | None = None,
        throttle: GlobalThrottle | None = None,
        environment: str = "dev",
        require_eval_preflight: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy or load_model_policy()
        self._env_doc = environments or load_environments()
        self._resolver = resolver or SecretResolver()
        self.throttle = throttle or GlobalThrottle.from_policy(self.policy)
        self.environment = environment
        self.require_eval_preflight = require_eval_preflight
        self._test_transport = transport
        self.endpoints = self._build_endpoints()
        self._clients: dict[GatewayClass, httpx.AsyncClient] = {}
        self._eval_preflight_valid_until: float = 0.0
        self._assert_keys_differ()

    # -- endpoints ----------------------------------------------------------
    def _build_endpoints(self) -> dict[GatewayClass, GatewayEndpoint]:
        env = self._env_doc["environments"][self.environment]
        requirements = self.policy.gateway_routing.client_requirements
        endpoints: dict[GatewayClass, GatewayEndpoint] = {}
        for key, gateway_class in (
            ("litellm_production", GatewayClass.PRODUCTION),
            ("litellm_eval", GatewayClass.EVAL),
        ):
            cfg = env[key]
            reqs = requirements.get(gateway_class.value, {})
            if gateway_class is GatewayClass.EVAL:
                # DEC-002 "Client-side obligation". Not configurable.
                max_retries = int(reqs.get("sdk_max_retries", 0))
                if max_retries != 0:
                    raise FailedProvenanceError(
                        "the eval client requires sdk_max_retries=0; the pack asked for "
                        f"{max_retries}"
                    )
                timeout = int(reqs.get("sdk_timeout_seconds", 120))
            else:
                max_retries = self.policy.retry_policy.max_retries_per_work_unit
                timeout = int(reqs.get("sdk_timeout_seconds", 120))
            endpoints[gateway_class] = GatewayEndpoint(
                name=key,
                gateway_class=gateway_class,
                base_url=str(cfg["base_url"]).rstrip("/"),
                api_key_ref=str(cfg["api_key_ref"]),
                max_retries=max_retries,
                timeout_seconds=timeout,
                valid_for_evidence=bool(cfg.get("valid_for_evidence", False)),
            )
        return endpoints

    def _secret_ref(self, endpoint: GatewayEndpoint) -> SecretRef:
        refs = self._pack_secret_refs()
        short = endpoint.api_key_ref.split(".")[-1]
        entry = refs.get(short) or {}
        return SecretRef(name=short, reference=entry.get("ref", f"env:{short.upper()}"))

    def _pack_secret_refs(self) -> dict[str, dict[str, Any]]:
        import yaml  # local: keeps the pack read confined to this helper

        path = Path(self.policy.source_path).parent / "secrets.refs.yaml"
        data = yaml.safe_load(path.read_text())
        return dict(data.get("refs") or {})

    def api_key(self, gateway_class: GatewayClass) -> str:
        endpoint = self.endpoints[GatewayClass(gateway_class)]
        value = self._resolver.resolve(self._secret_ref(endpoint))
        assert value is not None  # resolver raises MISSING_REQUIRED_CREDENTIAL otherwise
        return value

    def _assert_keys_differ(self) -> None:
        """``secrets.refs.yaml`` -> ``must_not_equal``. Reusing the production
        key on the eval gateway would collapse the DB-less isolation."""
        try:
            production = self.api_key(GatewayClass.PRODUCTION)
            evaluation = self.api_key(GatewayClass.EVAL)
        except Exception:  # noqa: BLE001 - a missing credential is reported at call time
            return
        if production == evaluation:
            raise FailedProvenanceError(
                "LITELLM_MASTER_KEY equals LITELLM_EVAL_MASTER_KEY; the two gateways must not "
                "share a credential (secrets.refs.yaml -> litellm_eval_key.must_not_equal)"
            )

    # -- clients ------------------------------------------------------------
    def client(self, gateway_class: GatewayClass | str) -> httpx.AsyncClient:
        """Return the dedicated client for a gateway class.

        Each class gets its **own** client object. The eval client's transport is
        constructed with ``retries=0`` explicitly rather than relying on the
        library default, so a future httpx default change cannot quietly
        reintroduce a retry on the evidence path.
        """
        gateway_class = GatewayClass(gateway_class)
        client = self._clients.get(gateway_class)
        if client is None:
            endpoint = self.endpoints[gateway_class]
            transport = self._test_transport or httpx.AsyncHTTPTransport(retries=0)
            client = httpx.AsyncClient(
                base_url=endpoint.base_url,
                timeout=httpx.Timeout(float(endpoint.timeout_seconds)),
                transport=transport,
                follow_redirects=False,
            )
            # Introspectable by the contract test; not used for control flow.
            client.efah_max_retries = endpoint.max_retries  # type: ignore[attr-defined]
            client.efah_gateway_class = gateway_class.value  # type: ignore[attr-defined]
            self._clients[gateway_class] = client
        return client

    def client_for_role(self, role: str, gateway: GatewayClass | str | None = None):
        """The only sanctioned way to obtain a client for a role."""
        required = gateway_class_for_role(role, self.policy)
        if gateway is not None:
            assert_gateway_for_role(role, gateway, self.policy)
        return self.client(required), self.endpoints[required]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def __aenter__(self) -> "LiteLLMGateway":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- preflight ----------------------------------------------------------
    def mark_eval_preflight(self, *, passed: bool, valid_for: float = PREFLIGHT_VALIDITY_SECONDS) -> None:
        self._eval_preflight_valid_until = time.monotonic() + valid_for if passed else 0.0

    @property
    def eval_preflight_valid(self) -> bool:
        return time.monotonic() < self._eval_preflight_valid_until

    async def canary_probe(self) -> CanaryResult:
        """DEC-002 live preflight half: ``__canary_invalid`` must fail *fast*.

        A 200 means something silently fell back, which is the exact failure the
        eval gateway exists to make impossible.
        """
        endpoint = self.endpoints[GatewayClass.EVAL]
        client = self.client(GatewayClass.EVAL)
        body = {
            "model": CANARY_MODEL,
            "messages": [{"role": "user", "content": "canary"}],
            "max_tokens": self.policy.request_policy.min_max_tokens_for_tool_calls,
        }
        await self.throttle.acquire_async()
        started = time.perf_counter()
        status: int | None = None
        detail = ""
        try:
            response = await client.post(
                "/v1/chat/completions",
                json=body,
                headers=self._headers(GatewayClass.EVAL),
            )
            status = response.status_code
            detail = response.text[:300]
        except httpx.HTTPError as exc:
            detail = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        return CanaryResult(
            base_url=endpoint.base_url,
            http_status=status,
            elapsed_seconds=round(elapsed, 3),
            fast=elapsed < CANARY_FAST_FAIL_SECONDS,
            errored=status is None or status >= 400,
            detail=detail,
        )

    # -- dispatch -----------------------------------------------------------
    def _headers(self, gateway_class: GatewayClass) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key(gateway_class)}",
            "Content-Type": "application/json",
        }

    def _enforce_token_floor(self, max_tokens: int, tools: Sequence[dict[str, Any]] | None) -> int:
        rp = self.policy.request_policy
        if tools and max_tokens < rp.min_max_tokens_for_tool_calls:
            raise FailedOracleError(
                f"max_tokens={max_tokens} is below min_max_tokens_for_tool_calls="
                f"{rp.min_max_tokens_for_tool_calls}; reasoning models emit reasoning_content "
                "before tool_calls, so a smaller budget records a false negative on tool support"
            )
        if max_tokens < rp.hard_floor_max_tokens:
            raise FailedOracleError(
                f"max_tokens={max_tokens} is below hard_floor_max_tokens={rp.hard_floor_max_tokens}"
            )
        return max_tokens

    async def chat_completion(
        self,
        *,
        role: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        model: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        stream: bool = False,
        extra_params: dict[str, Any] | None = None,
        gateway: GatewayClass | str | None = None,
    ) -> ChatResponse:
        """Dispatch one bounded model call.

        ``model`` is the real upstream identifier. It is resolved by the dispatch
        layer from the protected identity store and never appears in the returned
        :class:`ChatResponse`; only its configuration hash does.
        """
        required = gateway_class_for_role(role, self.policy)
        if gateway is not None:
            assert_gateway_for_role(role, gateway, self.policy)
        row = self.policy.role(role)
        upstream_model = model or row.litellm_model

        if required is GatewayClass.EVAL and self.require_eval_preflight and not self.eval_preflight_valid:
            raise FailedProvenanceError(
                "an evaluation campaign call was made without a passing eval preflight; "
                "DEC-002 requires validate_eval_config before every evaluation campaign"
            )

        self._enforce_token_floor(max_tokens, tools)

        body: dict[str, Any] = {
            "model": upstream_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"
        if extra_params:
            body.update(extra_params)
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}

        configuration_hash = content_hash(
            {
                "gateway": required.value,
                "base_url": self.endpoints[required].base_url,
                "policy_hash": self.policy.policy_hash,
                "max_retries": self.endpoints[required].max_retries,
                "timeout_seconds": self.endpoints[required].timeout_seconds,
                "request": {k: v for k, v in body.items() if k != "messages"},
            }
        )
        input_hash = content_hash(messages)

        reservation = await self.throttle.acquire_async()
        client = self.client(required)
        started = time.perf_counter()
        try:
            if stream:
                status, text, tool_calls, finish, usage = await self._post_stream(client, required, body)
            else:
                status, text, tool_calls, finish, usage = await self._post(client, required, body)
        except httpx.TimeoutException as exc:
            raise TransientProviderError(f"{required.value} gateway timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"{required.value} gateway transport error: {exc}") from exc
        latency = time.perf_counter() - started

        return ChatResponse(
            role=role,
            alias=row.alias,
            gateway=required.value,
            http_status=status,
            latency_seconds=latency,
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=usage,
            configuration_hash=configuration_hash,
            input_hash=input_hash,
            output_hash=content_hash({"text": text, "tool_calls": tool_calls}),
            throttle_wait_seconds=round(reservation.waited_seconds, 3),
            streamed=stream,
        )

    async def _post(self, client, gateway_class: GatewayClass, body: dict[str, Any]):
        response = await client.post(
            "/v1/chat/completions", json=body, headers=self._headers(gateway_class)
        )
        self._raise_for_status(response.status_code, response.text, gateway_class)
        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return (
            response.status_code,
            message.get("content") or "",
            list(message.get("tool_calls") or []),
            choice.get("finish_reason"),
            dict(payload.get("usage") or {}),
        )

    async def _post_stream(self, client, gateway_class: GatewayClass, body: dict[str, Any]):
        """``prefer_streaming: true`` -- better TTFB and it avoids proxy timeouts
        on long generations. Tool-call deltas are reassembled by index."""
        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish: str | None = None
        usage: dict[str, Any] = {}
        async with client.stream(
            "POST", "/v1/chat/completions", json=body, headers=self._headers(gateway_class)
        ) as response:
            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", "replace")
                self._raise_for_status(response.status_code, raw, gateway_class)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if event.get("usage"):
                    usage = dict(event["usage"])
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index", 0))
                        slot = tool_calls.setdefault(
                            index, {"id": call.get("id"), "type": "function", "function": {"name": "", "arguments": ""}}
                        )
                        function = call.get("function") or {}
                        if function.get("name"):
                            slot["function"]["name"] = function["name"]
                        if function.get("arguments"):
                            slot["function"]["arguments"] += function["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
            status = response.status_code
        return status, "".join(text_parts), [tool_calls[i] for i in sorted(tool_calls)], finish, usage

    @staticmethod
    def _raise_for_status(status: int, text: str, gateway_class: GatewayClass) -> None:
        if status < 400:
            return
        excerpt = text[:300]
        if status == 429:
            raise RateLimitError(f"{gateway_class.value} gateway returned 429: {excerpt}")
        if status >= 500:
            raise TransientProviderError(f"{gateway_class.value} gateway returned {status}: {excerpt}")
        lowered = excerpt.lower()
        if status == 404 or "no available channel" in lowered or "not found" in lowered:
            raise ModelUnavailableError(f"{gateway_class.value} gateway returned {status}: {excerpt}")
        raise GatewayRequestError(f"{gateway_class.value} gateway returned {status}: {excerpt}")
