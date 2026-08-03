#!/usr/bin/env python3
"""Find a configuration that works, instead of concluding the model does not.

DEC-008, owner rule 2026-08-02:

    "do not assume ckff model fail based on few failures always try to debug
     them through multiple ways multiple hypothesis and make sure they work…
     they mostly always work u just have to keep trying more fuzzy
     configuration till they work right… take positive result more than
     negative as most are false negative and jumped conclusion"

This tool exists because the builder did exactly what the rule forbids: declared
``kimi-k2.7-code`` unusable for long-form generation after **one** empty
response, having already rediscovered the "apparent failure is a measurement
artifact" lesson twice the same day.

The asymmetry is deliberate and is the whole design. **One success proves the
model can do the task; one failure proves nothing.** A configuration that works
even once is a configuration, and the job is to find it — so this sweeps a grid
and reports the first working cell per model rather than averaging failures into
a verdict.

Dimensions swept, each chosen because it has already caused a false negative on
this transport at least once:

``max_tokens``
    4000 truncated the mutant author (``finish_reason=length``); 16000 returned
    502 Bad Gateway. The usable band is narrow and per-model, not global.
``stream``
    ``minimax-m3`` emits tool calls only when not streaming. Streaming also
    changes how the upstream buffers a long response, which is exactly what a
    408 is about.
``task_size``
    Asking for six mutants at once is a different request from asking for two.
    A model that cannot do the large one may do the small one repeatedly.
``repetition``
    An empty generation is frequently transient. The rule says try again.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from models.throttle import GlobalThrottle  # noqa: E402

EVAL_BASE = "https://litellm-eval-production.up.railway.app"

#: A realistic long-form generation task: produce N complete modules. Short
#: probes have repeatedly passed while the real task failed, so the sweep uses
#: the shape of the real work.
TASK = (
    "Here is a correct Python module.\n\n```python\n"
    "class Lease:\n"
    "    def __init__(self, work_unit_id, generation=1):\n"
    "        self.work_unit_id = work_unit_id\n"
    "        self.generation = generation\n"
    "        self.released = False\n\n"
    "    def renew(self):\n"
    "        if self.released:\n"
    "            raise RuntimeError('cannot renew a released lease')\n"
    "        self.generation += 1\n"
    "        return self.generation\n\n"
    "    def release(self):\n"
    "        self.released = True\n"
    "```\n\n"
    "Produce {n} MUTANTS: complete copies of this module, each with exactly ONE "
    "small seeded defect a weak test suite would miss. Keep every public name and "
    "signature identical. Output each as a fenced python block preceded by "
    "`# FILE: mutant_<n>.py`. No prose."
)


@dataclass
class Cell:
    model: str
    max_tokens: int
    stream: bool
    task_size: int
    attempt: int
    ok: bool
    chars: int = 0
    finish_reason: str | None = None
    blocks: int = 0
    latency: float = 0.0
    error: str | None = None

    def as_body(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
            "task_size": self.task_size,
            "attempt": self.attempt,
            "ok": self.ok,
            "chars": self.chars,
            "finish_reason": self.finish_reason,
            "fenced_blocks": self.blocks,
            "latency_seconds": round(self.latency, 2),
            "error": (self.error or "")[:200] or None,
        }


def _decode_stream(raw: str) -> tuple[str, str | None]:
    """Reassemble an SSE stream into the message it carries.

    Returns the concatenated content and the last non-null ``finish_reason``.
    A streamed response is only comparable to a non-streamed one after this
    step; comparing decoded content to raw wire bytes is what made every
    streaming cell unfalsifiable.
    """
    parts: list[str] = []
    finish: str | None = None
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
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    return "".join(parts), finish


def run_cell(client, key, model, max_tokens, stream, size, attempt, throttle) -> Cell:
    throttle.acquire()
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": TASK.format(n=size)}],
        "max_tokens": max_tokens,
    }
    if stream:
        body["stream"] = True
    started = time.monotonic()
    try:
        r = client.post(
            f"{EVAL_BASE}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    except httpx.HTTPError as exc:
        return Cell(model, max_tokens, stream, size, attempt, False,
                    latency=time.monotonic() - started, error=f"{type(exc).__name__}: {exc}")

    latency = time.monotonic() - started
    if r.status_code != 200:
        return Cell(model, max_tokens, stream, size, attempt, False,
                    latency=latency, error=f"HTTP {r.status_code}: {r.text[:160]}")

    if stream:
        # This branch used to read `r.text` directly and count "# FILE:" in the
        # RAW SSE. It could not work: token streaming splits that marker across
        # deltas ("#", " FILE", ":"), each in its own JSON envelope, so the
        # substring never appeared and `blocks` was always 0. With
        # `ok = chars > 200 and blocks >= 1 and ...`, EVERY streaming cell failed
        # for EVERY model, always — a systematic false negative on the transport
        # DEC-008 makes the default, inside the tool built to prevent false
        # negatives. It recorded kimi-k3 as having no working configuration while
        # the model was returning complete generations.
        #
        # The stream is now decoded into the message it actually carries, and the
        # markers are counted in that.
        content, finish = _decode_stream(r.text)
        chars = len(content)
        blocks = content.count("# FILE:")
    else:
        try:
            payload = r.json()
        except ValueError:
            return Cell(model, max_tokens, stream, size, attempt, False,
                        latency=latency, error="non-JSON body")
        choice = (payload.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        chars = len(content)
        finish = choice.get("finish_reason")
        blocks = content.count("# FILE:")

    # Success means usable output, not merely HTTP 200. An empty body with a
    # 200 is the false-positive twin of the false negatives this tool exists
    # to prevent.
    ok = chars > 200 and blocks >= 1 and finish != "length"
    return Cell(model, max_tokens, stream, size, attempt, ok, chars, finish, blocks, latency,
                None if ok else f"chars={chars} blocks={blocks} finish={finish}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("models", nargs="+")
    ap.add_argument("--attempts", type=int, default=2, help="repetitions per cell")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "evidence" / "generation-config-sweep.json")
    args = ap.parse_args()

    env = {}
    f = Path.home() / ".efah" / "env"
    if f.is_file():
        for line in f.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            env[k] = v
    key = env.get("LITELLM_EVAL_MASTER_KEY")
    if not key:
        print("MISSING_REQUIRED_CREDENTIAL", file=sys.stderr)
        return 2

    throttle = GlobalThrottle.from_policy()
    cells: list[Cell] = []
    working: dict[str, dict[str, Any]] = {}

    grid = list(product([6000, 8000, 10000], [False, True], [3, 5]))
    with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=300.0) as client:
        for model in args.models:
            for max_tokens, stream, size in grid:
                if model in working:
                    break  # a working configuration was found; stop sweeping
                for attempt in range(1, args.attempts + 1):
                    cell = run_cell(client, key, model, max_tokens, stream, size, attempt, throttle)
                    cells.append(cell)
                    mark = "OK  " if cell.ok else "    "
                    print(f"  {mark}{model:18} tok={max_tokens:<6} stream={stream!s:5} "
                          f"n={size} try{attempt}  chars={cell.chars:<6} blocks={cell.blocks} "
                          f"{cell.error or ''}"[:150])
                    if cell.ok:
                        working[model] = cell.as_body()
                        break
                    time.sleep(1.5)

    report = {
        "check": "generation_config_sweep",
        "rule_ref": "DEC-008 — a failure is a configuration finding, not a verdict",
        "oracle_type": "reproducible_empirical_benchmark",
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "working_configuration_per_model": working,
        "models_with_no_working_configuration": [m for m in args.models if m not in working],
        "cells": [c.as_body() for c in cells],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print("\nworking configurations:")
    for m, c in working.items():
        print(f"  {m}: max_tokens={c['max_tokens']} stream={c['stream']} task_size={c['task_size']}")
    for m in report["models_with_no_working_configuration"]:
        print(f"  {m}: no cell succeeded in this grid — widen it before concluding anything")
    print(f"\nwritten: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
