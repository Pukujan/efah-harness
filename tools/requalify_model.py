#!/usr/bin/env python3
"""Re-qualify a prohibited model, properly.

``model-policy.yaml -> prohibited_models`` carries verdicts from the owner's
2026-08-01 benchmark. The owner has since reported using two of them
successfully elsewhere, which is a direct conflict between a recorded decision
and a live empirical fact — §7.2 resolves that class with a **fresh probe**, not
by preferring whichever is written down.

There is also a specific reason to distrust the original verdicts: the owner's
own ``MODELS.md`` records that an earlier tool-calling audit was wrong, and that
*"prior failures were measurement artifacts from insufficient max_tokens"*. A
prohibition derived from the same run may carry the same artifact.

So this probe is built to be hard to fool, per §7.4:

* **N repetitions, not one.** A single failure is an anecdote. ``latency_variance``
  in particular is a claim about a *distribution*, and one sample cannot support
  or refute it — the original verdict cited a 119.4s worst case, which only
  means anything against a population.
* **Adequate ``max_tokens``.** Reasoning models emit reasoning before tool
  calls; too small a budget truncates the response and the model looks
  incapable. This is the documented artifact, so the probe uses a generous
  budget and records it.
* **Every route separately.** ``gpt-5.6-sol`` is served by three codex groups
  plus two prefixed routes. "The model is unreliable" and "one of its five
  routes is unreliable" are different findings with different remedies, and a
  probe that does not separate them cannot tell you which you have.
* **Streaming and non-streaming.** ``minimax-m3`` was prohibited for emitting
  tool calls only when not streaming — a silent capability hole that a
  single-mode probe would miss entirely.
* **Paced.** FINDING-008: bursts produce 503s that are indistinguishable from a
  dead route, and the account limit is shared.

Output is a per-route record with a success rate, a latency distribution, and a
tool-call rate — enough to sustain *or* overturn the prohibition, and enough
that whoever reads it later can see which.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from models.throttle import GlobalThrottle  # noqa: E402

EVAL_BASE = "https://litellm-eval-production.up.railway.app"

#: Generous on purpose. The documented artifact is a budget too small for a
#: reasoning model to reach its tool call.
PROBE_MAX_TOKENS = 1024

#: Wider than the account floor. FINDING-008: the probe must not manufacture the
#: outage it reports.
SPACING_SECONDS = 1.6

PROBE_TOOL = {
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


@dataclass
class Attempt:
    ok: bool
    latency_seconds: float
    http_status: int | None = None
    tool_call: bool = False
    finish_reason: str | None = None
    error: str | None = None


@dataclass
class RouteResult:
    model: str
    condition: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def successes(self) -> list[Attempt]:
        return [a for a in self.attempts if a.ok]

    def as_body(self) -> dict[str, Any]:
        lat = sorted(a.latency_seconds for a in self.successes)
        return {
            "model": self.model,
            "condition": self.condition,
            "attempts": len(self.attempts),
            "successes": len(self.successes),
            "success_rate": round(len(self.successes) / len(self.attempts), 3) if self.attempts else 0.0,
            "tool_call_rate": (
                round(sum(a.tool_call for a in self.successes) / len(self.successes), 3)
                if self.successes
                else 0.0
            ),
            # A median alone is what let a 119.4s worst case hide behind a 4.7s
            # headline. Both, always.
            "latency_p50": round(statistics.median(lat), 2) if lat else None,
            "latency_max": round(max(lat), 2) if lat else None,
            "latency_all": [round(x, 2) for x in lat],
            "failures": [
                {"http_status": a.http_status, "error": (a.error or "")[:160]}
                for a in self.attempts
                if not a.ok
            ],
        }


def probe_once(client: httpx.Client, key: str, model: str, *, stream: bool, throttle) -> Attempt:
    throttle.acquire()
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        "tools": [PROBE_TOOL],
        "tool_choice": "auto",
    }
    if stream:
        body["stream"] = True

    started = time.monotonic()
    try:
        response = client.post(
            f"{EVAL_BASE}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    except httpx.HTTPError as exc:
        return Attempt(False, round(time.monotonic() - started, 3), error=f"{type(exc).__name__}: {exc}")

    latency = round(time.monotonic() - started, 3)
    if response.status_code != 200:
        return Attempt(False, latency, response.status_code, error=response.text[:300])

    if stream:
        # Tool calls arrive as deltas; presence anywhere in the stream counts.
        text = response.text
        return Attempt(
            True, latency, 200,
            tool_call="tool_calls" in text,
            finish_reason="tool_calls" if "tool_calls" in text else "stop",
        )

    try:
        payload = response.json()
    except ValueError:
        return Attempt(False, latency, 200, error="non-JSON body")
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return Attempt(
        True, latency, 200,
        tool_call=bool(message.get("tool_calls")),
        finish_reason=choice.get("finish_reason"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+", help="model ids to re-qualify")
    parser.add_argument("-n", "--repetitions", type=int, default=5)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "evidence" / "model-requalification.json")
    args = parser.parse_args()

    env: dict[str, str] = {}
    envfile = Path.home() / ".efah" / "env"
    if envfile.is_file():
        for line in envfile.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            env[k] = v
    key = env.get("LITELLM_EVAL_MASTER_KEY")
    if not key:
        print("MISSING_REQUIRED_CREDENTIAL: LITELLM_EVAL_MASTER_KEY", file=sys.stderr)
        return 2

    throttle = GlobalThrottle.from_policy()
    results: list[RouteResult] = []

    with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=180.0) as client:
        for model in args.models:
            for stream in (False, True):
                condition = "streaming" if stream else "non_streaming"
                result = RouteResult(model=model, condition=condition)
                for _ in range(args.repetitions):
                    result.attempts.append(probe_once(client, key, model, stream=stream, throttle=throttle))
                    time.sleep(SPACING_SECONDS)
                results.append(result)
                body = result.as_body()
                print(
                    f"  {model:28} {condition:14} "
                    f"{body['successes']}/{body['attempts']} ok  "
                    f"tool={body['tool_call_rate']}  "
                    f"p50={body['latency_p50']}s max={body['latency_max']}s"
                )

    report = {
        "check": "model_requalification",
        "contract_ref": "contract_7.2_live_empirical_fact_and_7.4_hypothesis_discipline",
        "oracle_type": "reproducible_empirical_benchmark",
        "model_judge_in_verdict_path": False,
        "evidence_tier": "INDEPENDENTLY_REPRODUCED",
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gateway": EVAL_BASE,
        "max_tokens": PROBE_MAX_TOKENS,
        "spacing_seconds": SPACING_SECONDS,
        "repetitions_per_condition": args.repetitions,
        "routes": [r.as_body() for r in results],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwritten: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
