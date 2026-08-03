#!/usr/bin/env python3
"""Does ``seed`` make this transport reproducible? Measure, do not assume.

The sealed holdout generator sends ``temperature: 0`` and no ``seed``, and 25
runs on one commit produced 25 different exercises. The cheap fix would be to
add ``seed`` to the request body and stop there — so before designing anything
architectural, the cheap fix is measured.

``project-pack/evidence/owner-documents/CONFIGURATIONGUIDE.md`` records ``seed``
as *"universally supported across 21 models tested"*, in a list that also
carries ``stop`` and JSON mode. That list measures **parameter acceptance** —
the gateway does not 400 — which is a different claim from **reproducibility**.
This probe measures the second one directly:

* three identical seeded requests per model, byte-compared;
* three identical *unseeded* requests per model, as the control, because a
  model that happens to be deterministic without a seed proves nothing about
  the seed;
* the presence of a ``system_fingerprint``, which is the only way a client can
  tell "the seed was honoured" from "the seed was dropped on the floor".

The prompt carries real sampling entropy on purpose. Asking for ``2+2`` would
produce identical answers from a fully non-deterministic sampler and would
measure nothing.

Read-only with respect to the sealed store; no holdout content is involved.
Uses the builder's own eval-gateway credential, not the verifier's.

Usage::

    python tools/probe_generation_determinism.py --repeats 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

EVAL_BASE_URL = "https://litellm-eval-production.up.railway.app"
BUILDER_ENV = Path.home() / ".efah" / "env"

#: The two roles whose non-determinism compounds: the holdout author writes the
#: reference implementation, and the mutant author's prompt embeds it.
MODELS = ("claude-opus-4-8", "kimi-k2.7-code")

#: Entropy on purpose. A deterministic answer to this prompt is a real result.
PROMPT = (
    "Invent a name for a fictional starship and give exactly one sentence of "
    "backstory. No preamble."
)

SEED = 20260803
MIN_INTERVAL_SECONDS = 0.9


def read_key() -> str:
    env = os.environ.get("LITELLM_EVAL_MASTER_KEY")
    if env:
        return env
    for line in BUILDER_ENV.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key == "LITELLM_EVAL_MASTER_KEY" and value:
            return value.strip().strip("'\"")
    raise SystemExit("no LITELLM_EVAL_MASTER_KEY available to the builder")


def call(api_key: str, model: str, *, seed: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 300,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if seed is not None:
        body["seed"] = seed
    request = urllib.request.Request(  # fixed https base url
        f"{EVAL_BASE_URL}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    chunks: list[str] = []
    fingerprint: str | None = None
    status = "ok"
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                fingerprint = fingerprint or event.get("system_fingerprint")
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        chunks.append(delta["content"])
    except Exception as exc:  # a transport failure is a result
        status = f"{type(exc).__name__}"
    text = "".join(chunks)
    return {
        "status": status,
        "seed_sent": seed,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode()).hexdigest()[:16],
        "system_fingerprint": fingerprint,
        "elapsed_s": round(time.time() - started, 2),
        "first_60": text.strip()[:60],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--out", default="evidence/generation-determinism-probe.json"
    )
    args = parser.parse_args()

    api_key = read_key()
    arms: list[dict[str, Any]] = []
    for model in MODELS:
        for label, seed in (("seeded", SEED), ("unseeded", None)):
            calls = []
            for _ in range(args.repeats):
                calls.append(call(api_key, model, seed=seed))
                time.sleep(MIN_INTERVAL_SECONDS)
            digests = {c["sha256"] for c in calls if c["status"] == "ok"}
            arms.append(
                {
                    "model": model,
                    "arm": label,
                    "repeats": args.repeats,
                    "distinct_outputs": len(digests),
                    "reproducible": len(digests) == 1 and len(calls) == args.repeats,
                    "system_fingerprint_present": any(
                        c["system_fingerprint"] for c in calls
                    ),
                    "calls": calls,
                }
            )
            print(
                f"{model:22s} {label:9s} distinct={len(digests)}/{args.repeats} "
                f"fingerprint={arms[-1]['system_fingerprint_present']}"
            )

    seeded = [a for a in arms if a["arm"] == "seeded"]
    verdict = (
        "SEED_MAKES_THIS_TRANSPORT_REPRODUCIBLE"
        if all(a["reproducible"] for a in seeded)
        else "SEED_DOES_NOT_MAKE_THIS_TRANSPORT_REPRODUCIBLE"
    )
    report = {
        "probe": "generation_determinism",
        "question": (
            "does sending `seed` with temperature 0 make this eval transport "
            "produce byte-identical completions for byte-identical prompts?"
        ),
        "why": (
            "the sealed holdout generator has no seed anywhere and 25 runs on one "
            "commit produced 25 different exercises; a working seed would be a "
            "cheaper fix than freezing the exam, so it is measured first"
        ),
        "prompt_entropy_note": (
            "the prompt asks for an invented name, so identical answers are a real "
            "result rather than an artefact of a question with one answer"
        ),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": EVAL_BASE_URL,
        "seed": SEED,
        "verdict": verdict,
        "arms": arms,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n{verdict}\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
