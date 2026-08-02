"""``validate_eval_config`` -- the DEC-002 preflight.

DEC-002, "Preflight obligation": before **every** evaluation campaign this must
exit 0. It has two halves.

*Static half* -- the traps that live on our side of the wire and cannot be fixed
server-side: an eval client that can retry, a shared session object, a timeout
that is not 120s, the eval gateway reusing production's master key.

*Live half* -- calls ``__canary_invalid``, a route on the eval deployment that
points at a nonexistent upstream model, and asserts it fails **fast** (measured
1.22s; the same failure with 5 retries at ``retry_after: 2`` takes >= 10s) and
returns an **error**. A 200 would mean something silently fell back.

Run it::

    python -m models.eval_preflight --url https://litellm-eval-production.up.railway.app
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from governance.envelope import utc_now
from models.gateway import (
    CANARY_FAST_FAIL_SECONDS,
    CanaryResult,
    GatewayClass,
    LiteLLMGateway,
    transport_retries,
)
from models.policy import ModelPolicy, load_model_policy


@dataclass
class PreflightCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class PreflightResult:
    base_url: str
    checks: list[PreflightCheck] = field(default_factory=list)
    canary: CanaryResult | None = None
    checked_at: str = field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_body(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "checked_at": self.checked_at,
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
            "canary": asdict(self.canary) if self.canary else None,
        }


def static_checks(gateway: LiteLLMGateway, policy: ModelPolicy | None = None) -> list[PreflightCheck]:
    """Everything provable without spending a request."""
    policy = policy or gateway.policy
    checks: list[PreflightCheck] = []
    eval_endpoint = gateway.endpoints[GatewayClass.EVAL]
    prod_endpoint = gateway.endpoints[GatewayClass.PRODUCTION]

    checks.append(
        PreflightCheck(
            "eval_client_zero_retries",
            eval_endpoint.max_retries == 0,
            f"eval endpoint max_retries={eval_endpoint.max_retries} (DEC-002 requires 0)",
        )
    )
    checks.append(
        PreflightCheck(
            "eval_client_timeout_120",
            eval_endpoint.timeout_seconds == 120,
            f"eval endpoint timeout={eval_endpoint.timeout_seconds}s (DEC-002 requires 120)",
        )
    )

    eval_client = gateway.client(GatewayClass.EVAL)
    prod_client = gateway.client(GatewayClass.PRODUCTION)
    observed_retries = transport_retries(eval_client)
    checks.append(
        PreflightCheck(
            "eval_transport_retries_zero",
            observed_retries in (0, None),
            f"eval httpx connection pool reports retries={observed_retries}",
        )
    )
    checks.append(
        PreflightCheck(
            "eval_client_timeout_object",
            eval_client.timeout.read == 120.0 and eval_client.timeout.connect == 120.0,
            f"httpx timeout={eval_client.timeout}",
        )
    )
    checks.append(
        PreflightCheck(
            "no_shared_session_object",
            eval_client is not prod_client,
            "eval and production clients are distinct objects (a shared session voids the "
            "zero-retry guarantee from outside the proxy)",
        )
    )

    try:
        keys_differ = gateway.api_key(GatewayClass.EVAL) != gateway.api_key(GatewayClass.PRODUCTION)
        key_detail = "eval and production master keys differ"
    except Exception as exc:
        keys_differ = False
        key_detail = f"could not resolve both master keys: {type(exc).__name__}"
    checks.append(PreflightCheck("separate_master_keys", keys_differ, key_detail))

    gate_bearing = policy.gateway_routing.gate_bearing_roles
    misrouted = sorted(r for r in gate_bearing if policy.role(r).gateway != "eval")
    checks.append(
        PreflightCheck(
            "gate_bearing_roles_on_eval",
            not misrouted,
            f"{len(gate_bearing)} gate-bearing roles declared; misrouted={misrouted}",
        )
    )
    checks.append(
        PreflightCheck(
            "eval_endpoint_is_evidence_grade",
            eval_endpoint.valid_for_evidence and not prod_endpoint.valid_for_evidence,
            "environments.yaml marks eval valid_for_evidence and production not",
        )
    )
    return checks


async def validate_eval_config(
    base_url: str | None = None,
    *,
    gateway: LiteLLMGateway | None = None,
    live: bool = True,
    policy: ModelPolicy | None = None,
) -> PreflightResult:
    """Run the preflight. On success the gateway is authorised for eval dispatch."""
    policy = policy or load_model_policy()
    owns_gateway = gateway is None
    gateway = gateway or LiteLLMGateway(policy=policy, require_eval_preflight=False)
    try:
        endpoint = gateway.endpoints[GatewayClass.EVAL]
        result = PreflightResult(base_url=base_url or endpoint.base_url)
        if base_url is not None and base_url.rstrip("/") != endpoint.base_url:
            result.checks.append(
                PreflightCheck(
                    "url_matches_pack",
                    False,
                    f"--url {base_url} does not match environments.yaml {endpoint.base_url}",
                )
            )
        result.checks.extend(static_checks(gateway, policy))

        if live:
            canary = await gateway.canary_probe()
            result.canary = canary
            result.checks.append(
                PreflightCheck(
                    "canary_returns_error",
                    canary.errored,
                    f"__canary_invalid -> HTTP {canary.http_status}; a 200 means something "
                    "silently fell back",
                )
            )
            result.checks.append(
                PreflightCheck(
                    "canary_fails_fast",
                    canary.fast,
                    f"__canary_invalid failed in {canary.elapsed_seconds}s "
                    f"(< {CANARY_FAST_FAIL_SECONDS}s means no hidden retries)",
                )
            )

        gateway.mark_eval_preflight(passed=result.passed)
        return result
    finally:
        if owns_gateway:
            await gateway.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DEC-002 eval gateway preflight")
    parser.add_argument("--url", default=None, help="eval gateway base URL (must match the pack)")
    parser.add_argument("--no-live", action="store_true", help="static checks only")
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = parser.parse_args(argv)

    result = asyncio.run(validate_eval_config(args.url, live=not args.no_live))
    if args.json:
        print(json.dumps(result.as_body(), indent=2))
    else:
        for check in result.checks:
            print(f"  {'PASS' if check.passed else 'FAIL'}  {check.name}: {check.detail}")
        print("validate_eval_config:", "PASS" if result.passed else "FAIL")
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv[1:]))
