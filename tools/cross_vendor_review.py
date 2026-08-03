#!/usr/bin/env python3
"""Send every registered gate check to a cross-family critic and ask it to refute.

The checks wired on 2026-08-02/03 were written by one vendor's agents and
accepted by the same vendor's orchestrator. That is the circularity Section 12.4
names: *a producing model must not be the sole reviewer of its own output.* This
tool is the independent pass -- it routes through the harness's ``efah-review``
mode, which the pack maps to ``adversarial_critic``, deliberately cross-family
from the ``implementer`` seat.

**What this tool is not.** It is not a gate and its output is not a verdict.
``authority_limits`` forbids a model judge in a deterministic verdict path and
records ``cross_vendor_agreement_is_proof: false``. The critic here is a
*finder*: it proposes ways a check could pass while its assertion is false, and
a human decides whether each proposal is real. A refutation this tool reports is
a hypothesis to test, never a failure to record.

**The question it asks.** Not "is this check good" -- a model asked to review
will find something to say regardless, and the resulting prose is unfalsifiable.
It asks for a *concrete scenario in which the check passes while the property it
names is false*. That has a truth value: either the scenario survives contact
with the code or it does not.

Serial by construction. ``request_policy`` records an account-wide 100 req/min
ceiling with ``unthrottled_fanout: forbidden``, so concurrency buys nothing here
and only manufactures 429s indistinguishable from real failures.

**Durability.** The first full run of this tool was killed at ~58 minutes by a
watchdog restarting its parent session, and produced *nothing* -- results were
held in memory and written once at the end. Each review is now appended to a
JSONL sidecar and flushed the moment it lands, and ``--resume`` skips pairs
already recorded there. The rolled-up JSON is derived from that sidecar, so the
sidecar is the durable artifact and the JSON is a convenience.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = REPO_ROOT.parent / "project-pack"
FACADE = "http://127.0.0.1:8088"
CRITIC_MODE = "efah-review"

#: Which module holds each gate's checks. Read from the registry rather than
#: hardcoded so a gate wired later is not silently skipped.
CHECK_MODULES = [
    "checks_d1_03", "checks_d1_04", "checks_d1_05", "checks_d1_06",
    "checks_d2_11", "checks_d2_12", "checks_d2_21", "checks_d2_22",
    "checks_audit_followup",
]

PROMPT = """You are reviewing a verification check written to prove one acceptance \
assertion in a build harness. Your job is to REFUTE it, not to approve it.

THE ASSERTION THE CHECK MUST PROVE
gate: {gate_id}
assertion: {assertion_id}
statement: {statement}
method: {method}
expected: {expected}

THE CHECK AS WRITTEN
```python
{source}
```

Answer this one question: is there a CONCRETE scenario in which this check \
returns PASS while the property named in the assertion is actually FALSE?

Rules for your answer:
- A scenario must be specific enough to test: name the code path, the input, or \
the substitution that would slip past.
- "It could be more thorough" is not a refutation. Vague criticism is worthless \
here.
- If the check genuinely pins the property, say NOT_REFUTED and stop. Saying so \
is a real answer, not a failure to find something.
- Judge only what the check proves versus what the assertion claims. Style, \
naming and performance are out of scope.

Begin your reply with exactly one of:
REFUTED: <one-line summary of the scenario>
NOT_REFUTED
"""


@dataclass
class Review:
    gate_id: str
    assertion_id: str
    verdict: str
    latency_seconds: float
    source_lines: int
    summary: str = ""
    body: str = ""
    error: str | None = None
    findings: list[str] = field(default_factory=list)


def _load_specs() -> dict[tuple[str, str], dict[str, Any]]:
    """Every assertion the visible acceptance specs declare, keyed by id pair."""
    specs: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted((PACK_ROOT / "acceptance" / "visible").glob("GATE-*.yaml")):
        doc = yaml.safe_load(path.read_text())
        gate_id = doc.get("gate_id")
        for assertion in doc.get("assertions") or []:
            aid = assertion.get("assertion_id") or assertion.get("id")
            if gate_id and aid:
                specs[(gate_id, aid)] = assertion
    return specs


def _function_sources(module_name: str) -> dict[str, str]:
    """Top-level function source, by name, without importing the module.

    Parsed rather than imported so this tool never pulls the check registry into
    its own process -- the checks touch live services, and reviewing them should
    not run them.
    """
    path = REPO_ROOT / "src" / "evaluation" / f"{module_name}.py"
    if not path.is_file():
        return {}
    text = path.read_text()
    lines = text.splitlines()
    out: dict[str, str] = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = "\n".join(lines[node.lineno - 1 : node.end_lineno])
    return out


def _registered_pairs() -> list[tuple[str, str, str]]:
    """(gate_id, assertion_id, function_name) for every registered check."""
    pairs: list[tuple[str, str, str]] = []
    entry = re.compile(r'\("(GATE-[A-Z0-9-]+)",\s*"(A\d+)"\):\s*([A-Za-z_][A-Za-z0-9_]*)')
    for module in CHECK_MODULES:
        path = REPO_ROOT / "src" / "evaluation" / f"{module}.py"
        if not path.is_file():
            continue
        for gate_id, aid, fn in entry.findall(path.read_text()):
            pairs.append((gate_id, aid, f"{module}:{fn}"))
    return sorted(set(pairs))


def _decode_stream(raw: str) -> str:
    """Reassemble the SSE body into the message it carries."""
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


def review_one(client: httpx.Client, gate_id: str, aid: str,
               spec: dict[str, Any], source: str) -> Review:
    prompt = PROMPT.format(
        gate_id=gate_id,
        assertion_id=aid,
        statement=spec.get("statement", "(not recorded)"),
        method=spec.get("method", "(not recorded)"),
        expected=spec.get("expected", "(not recorded)"),
        source=source,
    )
    started = time.monotonic()
    # Streamed, per DEC-008. These prompts carry a whole check function, and a
    # non-streamed request of this size dies at the edge proxy at ~100-120s --
    # which reads as the model failing rather than the transport. The first run
    # of this tool did exactly that.
    try:
        response = client.post(
            f"{FACADE}/v1/chat/completions",
            json={"model": CRITIC_MODE, "stream": True,
                  "messages": [{"role": "user", "content": prompt}]},
        )
    except httpx.HTTPError as exc:
        return Review(gate_id, aid, "ERROR", time.monotonic() - started,
                      source.count("\n") + 1, error=f"{type(exc).__name__}: {exc}")
    latency = time.monotonic() - started
    if response.status_code != 200:
        return Review(gate_id, aid, "ERROR", latency, source.count("\n") + 1,
                      error=f"HTTP {response.status_code}: {response.text[:200]}")

    text = _decode_stream(response.text)
    head = text.strip().splitlines()[0] if text.strip() else ""
    if head.upper().startswith("REFUTED"):
        verdict = "REFUTED"
    elif "NOT_REFUTED" in head.upper():
        verdict = "NOT_REFUTED"
    else:
        # An unparseable reply is recorded as its own class rather than being
        # rounded to NOT_REFUTED. A critic that did not answer the question is
        # not a critic that found nothing.
        verdict = "UNPARSED"
    return Review(gate_id, aid, verdict, latency, source.count("\n") + 1,
                  summary=head[:300], body=text)


def _load_completed(path: Path) -> dict[tuple[str, str], Review]:
    """Reviews already durably recorded, keyed by id pair.

    A truncated final line is expected -- the process this guards against is
    killed mid-write -- so an unparseable record is dropped rather than fatal.
    """
    done: dict[tuple[str, str], Review] = {}
    if not path.is_file():
        return done
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("verdict") in {"ERROR", "SKIPPED"}:
            continue  # a failure is worth retrying; a real verdict is not
        done[(row.get("gate_id"), row.get("assertion_id"))] = Review(**row)
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "evidence" / "cross-vendor-check-review.json")
    parser.add_argument("--jsonl", type=Path,
                        default=REPO_ROOT / "evidence" / "cross-vendor-check-review.jsonl",
                        help="durable append-only sidecar; the JSON is derived from it")
    parser.add_argument("--resume", action="store_true",
                        help="skip pairs already carrying a verdict in --jsonl")
    parser.add_argument("--only", nargs="*", help="limit to these gate ids")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    specs = _load_specs()
    pairs = _registered_pairs()
    if args.only:
        pairs = [p for p in pairs if p[0] in set(args.only)]
    if not pairs:
        print("no registered checks found", file=sys.stderr)
        return 2

    sources: dict[str, dict[str, str]] = {m: _function_sources(m) for m in CHECK_MODULES}
    completed = _load_completed(args.jsonl) if args.resume else {}
    if completed:
        print(f"resuming: {len(completed)} of {len(pairs)} already recorded in {args.jsonl}",
              flush=True)

    reviews: list[Review] = []
    args.jsonl.parent.mkdir(parents=True, exist_ok=True)
    # Line-buffered append. Every verdict is on disk before the next request
    # starts, so a kill costs at most the one review in flight.
    with args.jsonl.open("a", encoding="utf-8", buffering=1) as sidecar, \
            httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=args.timeout) as client:

        def record(review: Review) -> None:
            reviews.append(review)
            sidecar.write(json.dumps(asdict(review), ensure_ascii=False) + "\n")

        for gate_id, aid, ref in pairs:
            prior = completed.get((gate_id, aid))
            if prior is not None:
                reviews.append(prior)  # already durable; not re-appended
                print(f"  {prior.verdict:11} {gate_id} {aid:4}   (resumed)", flush=True)
                continue
            module, _, fn = ref.partition(":")
            source = sources.get(module, {}).get(fn)
            spec = specs.get((gate_id, aid))
            if source is None or spec is None:
                record(Review(gate_id, aid, "SKIPPED", 0.0, 0,
                              error="check source or acceptance spec not found"))
                print(f"  SKIP        {gate_id} {aid}", flush=True)
                continue
            review = review_one(client, gate_id, aid, spec, source)
            record(review)
            print(f"  {review.verdict:11} {gate_id} {aid:4} {review.latency_seconds:6.1f}s  "
                  f"{review.summary[:96]}", flush=True)

    counts: dict[str, int] = {}
    for r in reviews:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    payload = {
        "check": "cross_vendor_adversarial_review_of_gate_checks",
        "contract_ref": "contract_12.4_producer_not_sole_reviewer",
        "oracle_type": "model_finder_not_oracle",
        "model_judge_in_verdict_path": False,
        "authority": (
            "ADVISORY ONLY. authority_limits sets model_judge_in_deterministic_verdict_path "
            "to forbidden and cross_vendor_agreement_is_proof to false. A REFUTED verdict "
            "here is a hypothesis to test against the code, never a recorded gate failure."
        ),
        "critic_mode": CRITIC_MODE,
        "critic_role": "adversarial_critic (cross-family from implementer by 12.4)",
        "counts": counts,
        "reviews": [asdict(r) for r in reviews],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"\n{counts}")
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
