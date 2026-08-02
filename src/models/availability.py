"""Empirical availability probe and the ``ModelCapability`` record.

``model-policy.yaml -> availability_probe`` requires a probe before first
dispatch and records the result as ``ModelCapability``. The reason is measured,
not theoretical: the entire flat-rate ``[aws]`` lane answered normally in the
morning of 2026-08-01 and returned ``503 No available channel`` the same
afternoon. A static config that assumed availability failed at runtime with no
typed cause.

Two rules the probe itself must obey or it manufactures the failures it claims
to measure:

* ``probe_max_tokens: 512`` -- reasoning models emit ``reasoning_content``
  before ``tool_calls``. A smaller budget truncates the response before the tool
  call exists and the model looks incapable. That produced eleven false
  negatives in the owner's own benchmark. Enforced as ``FAILED_ORACLE``.
* the probe runs **serially under the global throttle**. A fan-out probe of
  fifteen aliases self-inflicts 429s, and a self-inflicted 429 recorded as a
  capability finding is fabricated evidence.

Capability records carry an alias and a gateway class only -- never a vendor.
They are safe to show an agent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from governance.envelope import utc_now
from models.errors import FailedOracleError
from models.policy import ModelPolicy, load_model_policy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from models.gateway import LiteLLMGateway

DEFAULT_REGISTRY_PATH = Path(".data") / "model-capabilities.json"

#: A trivially checkable tool. If a model can emit one tool call it supports
#: tool calling; if it cannot, that is a capability fact worth recording.
PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_status",
        "description": "Report probe status back to the harness.",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "description": "always the word ok"}},
            "required": ["status"],
        },
    },
}

PROBE_PROMPT = "Call the report_status tool once with status set to ok. Do not reply with prose."


@dataclass(frozen=True)
class ModelCapability:
    """Contract Section 11.1 ``capability and calibration records``.

    Alias-scoped on purpose: this record is agent-visible, so it must survive
    the GATE-D1-06 payload scan.
    """

    alias: str
    gateway: str
    available: bool
    probed_at: str = field(default_factory=utc_now)
    latency_seconds: float | None = None
    emitted_tool_call: bool = False
    max_tokens_used: int = 512
    http_status: int | None = None
    failure_class: str | None = None
    detail: str | None = None

    def as_body(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityRegistry:
    """In-memory registry with optional JSON persistence.

    Persisted under ``.data/`` (gitignored, rebuildable) because it is measured
    state, not authority. TerminusDB remains the authority for anything durable.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._records: dict[str, ModelCapability] = {}
        if self.path is not None and self.path.is_file():
            self.load()

    def record(self, capability: ModelCapability) -> ModelCapability:
        self._records[capability.alias] = capability
        return capability

    def get(self, alias: str) -> ModelCapability | None:
        return self._records.get(alias)

    def is_known_unavailable(self, alias: str) -> bool:
        record = self._records.get(alias)
        return record is not None and not record.available

    def aliases(self) -> frozenset[str]:
        return frozenset(self._records)

    def all(self) -> list[ModelCapability]:
        return [self._records[a] for a in sorted(self._records)]

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("no path configured for the capability registry")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([r.as_body() for r in self.all()], indent=2))
        self.path = target
        return target

    def load(self, path: Path | str | None = None) -> None:
        target = Path(path) if path is not None else self.path
        if target is None or not target.is_file():
            return
        for entry in json.loads(target.read_text()):
            self._records[entry["alias"]] = ModelCapability(**entry)


class AvailabilityProbe:
    """Probes mapped aliases serially, through the gateway each one belongs to."""

    def __init__(
        self,
        gateway: LiteLLMGateway,
        policy: ModelPolicy | None = None,
        registry: CapabilityRegistry | None = None,
    ) -> None:
        self.gateway = gateway
        self.policy = policy or load_model_policy()
        self.registry = registry if registry is not None else CapabilityRegistry()
        probe_tokens = self.policy.availability_probe.probe_max_tokens
        floor = self.policy.request_policy.min_max_tokens_for_tool_calls
        if self.policy.availability_probe.probe_includes_tool_call and probe_tokens < floor:
            raise FailedOracleError(
                f"probe_max_tokens={probe_tokens} is below min_max_tokens_for_tool_calls={floor}; "
                "the probe would manufacture false negatives on tool support"
            )
        self.probe_max_tokens = probe_tokens

    def roles_to_probe(self, roles: Sequence[str] | None = None) -> list[str]:
        if roles is not None:
            return list(roles)
        return sorted(self.policy.roles)

    async def probe_role(self, role: str) -> ModelCapability:
        row = self.policy.role(role)
        include_tool = self.policy.availability_probe.probe_includes_tool_call
        try:
            response = await self.gateway.chat_completion(
                role=role,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
                max_tokens=self.probe_max_tokens,
                tools=[PROBE_TOOL] if include_tool else None,
            )
        except Exception as exc:
            capability = ModelCapability(
                alias=row.alias,
                gateway=row.gateway,
                available=False,
                max_tokens_used=self.probe_max_tokens,
                failure_class=getattr(exc, "typed_state", type(exc).__name__),
                detail=str(exc)[:300],
            )
            return self.registry.record(capability)

        return self.registry.record(
            ModelCapability(
                alias=row.alias,
                gateway=row.gateway,
                available=True,
                latency_seconds=round(response.latency_seconds, 3),
                emitted_tool_call=bool(response.tool_calls),
                max_tokens_used=self.probe_max_tokens,
                http_status=response.http_status,
            )
        )

    async def run(self, roles: Sequence[str] | None = None) -> list[ModelCapability]:
        """Probe serially. Concurrency here is exactly the forbidden fan-out."""
        results = []
        for role in self.roles_to_probe(roles):
            results.append(await self.probe_role(role))
        return results


async def _run_probe(roles: Sequence[str] | None, output: Path) -> list[ModelCapability]:
    from models.eval_preflight import validate_eval_config
    from models.gateway import LiteLLMGateway

    policy = load_model_policy()
    gateway = LiteLLMGateway(policy=policy)
    try:
        # Any eval-side probe is part of an evaluation campaign (DEC-002).
        preflight = await validate_eval_config(gateway=gateway)
        if not preflight.passed:
            raise SystemExit("eval preflight failed; refusing to probe the eval gateway")
        registry = CapabilityRegistry(output)
        probe = AvailabilityProbe(gateway, policy, registry)
        results = await probe.run(roles)
        registry.save(output)
        return results
    finally:
        await gateway.aclose()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    """Run the availability probe. Serial and throttled, never a fan-out."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Empirical model availability probe")
    parser.add_argument("--roles", nargs="*", default=None, help="subset of roles (default: all)")
    parser.add_argument("--output", default=str(DEFAULT_REGISTRY_PATH))
    args = parser.parse_args(argv)

    results = asyncio.run(_run_probe(args.roles, Path(args.output)))
    for record in results:
        status = "UP  " if record.available else "DOWN"
        extra = (
            f"{record.latency_seconds}s tool_call={record.emitted_tool_call}"
            if record.available
            else f"{record.failure_class}: {(record.detail or '')[:90]}"
        )
        print(f"  {status} {record.alias:<18} {record.gateway:<11} {extra}")
    down = [r.alias for r in results if not r.available]
    print(f"availability_probe: {len(results) - len(down)}/{len(results)} available; down={down}")
    return 0 if not down else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
