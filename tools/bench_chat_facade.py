#!/usr/bin/env python3
"""Live end-to-end sample of the chat façade — the half a stub cannot measure.

``tests/contract/test_chat_facade_contract.py`` proves the façade routes,
blinds, bounds and refuses correctly, with no model in the loop. It says nothing
about whether a turn actually completes against real infrastructure, how long it
takes, or whether a mode returns something usable. This tool measures that, and
only that.

**Why this is a small sample and not five hundred calls.** Every scenario here is
a real generation through the harness. ``request_policy`` records a measured
account-wide limit of 100 requests/minute with ``unthrottled_fanout: forbidden``,
so concurrency does not buy throughput — the throttle serialises regardless, and
a fan-out only manufactures 429s that are indistinguishable from real failure.
The bulk of coverage therefore belongs in the deterministic suite, which runs
hundreds of cases in under two seconds for free. Scale this with ``--repeat``
deliberately, knowing each unit is a paid generation.

**Usage accounting is reported as missing, not silently omitted.** The façade
returns no ``usage`` block, so an OpenAI client has no token counts to display.
That is recorded per-run as ``usage_reported: false`` rather than left for a
reader to assume, because a benchmark that quietly drops a field it could not
find is how an absence becomes an unstated assumption.

The verdict path is deterministic: latency, HTTP status, byte counts and
structural checks. No model judges another model's output — ``authority_limits``
forbids exactly that.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:8088"

#: One scenario per mode, plus the refusal path. Each names what it is checking
#: so a failure reads as a finding rather than a number.
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "auto_short_answer",
        "model": "efah-auto",
        "prompt": "Reply with exactly the word: ok",
        "expect_min_chars": 1,
    },
    {
        "id": "plan_decomposition",
        "model": "efah-plan",
        "prompt": "Plan the work to add a /metrics endpoint to an existing FastAPI service.",
        "expect_min_chars": 400,
    },
    {
        "id": "research_evidence",
        "model": "efah-research",
        "prompt": "What are the trade-offs between server-sent events and websockets?",
        "expect_min_chars": 300,
    },
    {
        "id": "review_refutation",
        "model": "efah-review",
        "prompt": "Review this claim: 'the tests pass, so the feature is correct'.",
        "expect_min_chars": 300,
    },
    {
        "id": "build_candidate",
        "model": "efah-build",
        "prompt": "Add a docstring to a function called healthz. Do not change behaviour.",
        "expect_min_chars": 100,
    },
    {
        "id": "multi_turn_continuity",
        "model": "efah-auto",
        "messages": [
            {"role": "user", "content": "My project is called Bluefin."},
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": "What is my project called? One word."},
        ],
        "expect_min_chars": 1,
        "expect_substring": "luefin",
    },
    {
        "id": "foreign_model_refused",
        "model": "gpt-4o",
        "prompt": "hello",
        "expect_status": 400,
    },
]


@dataclass
class RunResult:
    scenario: str
    model: str
    stream: bool
    attempt: int
    ok: bool
    http_status: int | None
    latency_seconds: float
    chars: int = 0
    usage_reported: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


def _decode_stream(raw: str) -> str:
    parts: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        blob = line[5:].strip()
        if not blob or blob == "[DONE]":
            continue
        try:
            event = json.loads(blob)
        except ValueError:
            continue
        for choice in event.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
    return "".join(parts)


def run_one(client: httpx.Client, base: str, sc: dict[str, Any], *, stream: bool,
            attempt: int) -> RunResult:
    messages = sc.get("messages") or [{"role": "user", "content": sc["prompt"]}]
    body: dict[str, Any] = {"model": sc["model"], "messages": messages}
    if stream:
        body["stream"] = True

    started = time.monotonic()
    try:
        response = client.post(f"{base}/v1/chat/completions", json=body)
    except httpx.HTTPError as exc:
        return RunResult(sc["id"], sc["model"], stream, attempt, False, None,
                         time.monotonic() - started, error=f"{type(exc).__name__}: {exc}")
    latency = time.monotonic() - started

    expected_status = sc.get("expect_status", 200)
    if response.status_code != expected_status:
        return RunResult(sc["id"], sc["model"], stream, attempt, False, response.status_code,
                         latency, error=response.text[:300])
    if expected_status != 200:
        # A refusal scenario: the correct status IS the result.
        return RunResult(sc["id"], sc["model"], stream, attempt, True, response.status_code,
                         latency, checks={"refused_as_specified": True})

    usage_reported = False
    if stream:
        text = _decode_stream(response.text)
        checks = {
            "terminates_with_done": response.text.rstrip().endswith("data: [DONE]"),
            "content_type_is_sse": response.headers
            .get("content-type", "").startswith("text/event-stream"),
        }
    else:
        payload = response.json()
        text = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        usage_reported = bool(payload.get("usage"))
        checks = {
            "echoes_mode_id": payload.get("model") == sc["model"],
            "finish_reason_stop": (payload.get("choices") or [{}])[0].get("finish_reason")
            == "stop",
        }

    checks["meets_min_length"] = len(text) >= sc.get("expect_min_chars", 1)
    if "expect_substring" in sc:
        checks["carries_expected_substring"] = sc["expect_substring"].lower() in text.lower()
    # A dispatch failure is surfaced as readable text by design, so a 200 is not
    # proof of success. Check the body for the harness's own failure prefixes.
    checks["not_a_surfaced_failure"] = not text.startswith(
        ("Dispatch failed", "Routing failed", "No content returned")
    )

    return RunResult(sc["id"], sc["model"], stream, attempt, all(checks.values()),
                     response.status_code, latency, len(text), usage_reported, checks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--repeat", type=int, default=1,
                        help="repetitions per scenario per transport; each is a paid generation")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1] / "evidence"
                        / "chat-facade-live-bench.json")
    args = parser.parse_args()

    results: list[RunResult] = []
    with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=args.timeout) as client:
        for sc in SCENARIOS:
            transports = (False,) if sc.get("expect_status") else (False, True)
            for stream in transports:
                for attempt in range(1, args.repeat + 1):
                    result = run_one(client, args.base, sc, stream=stream, attempt=attempt)
                    results.append(result)
                    failed = [k for k, v in result.checks.items() if not v]
                    print(f"  {'PASS' if result.ok else 'FAIL'}  {result.scenario:24} "
                          f"{'stream' if stream else 'plain ':6} "
                          f"{result.latency_seconds:6.1f}s  chars={result.chars:<6} "
                          f"{('failed: ' + ','.join(failed)) if failed else ''}"
                          f"{(' ' + result.error[:60]) if result.error else ''}")

    passed = [r for r in results if r.ok]
    latencies = sorted(r.latency_seconds for r in passed) or [0.0]
    payload = {
        "check": "chat_facade_live_bench",
        "oracle_type": "deterministic_structural_checks",
        "model_judge_in_verdict_path": False,
        "base_url": args.base,
        "scenarios": len(SCENARIOS),
        "runs": len(results),
        "passed": len(passed),
        "failed": len(results) - len(passed),
        "latency_p50": round(statistics.median(latencies), 2),
        "latency_max": round(max(latencies), 2),
        "usage_block_returned_by_facade": any(r.usage_reported for r in results),
        "note": (
            "Sample size is deliberately small: every run is a real generation and the "
            "account-wide throttle makes concurrency useless. Structural coverage lives in "
            "tests/contract/test_chat_facade_contract.py."
        ),
        "results": [asdict(r) for r in results],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"\n{len(passed)}/{len(results)} passed · p50 {payload['latency_p50']}s "
          f"· usage block returned: {payload['usage_block_returned_by_facade']}")
    print(f"written: {args.out}")
    return 0 if len(passed) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
