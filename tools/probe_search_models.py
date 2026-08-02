#!/usr/bin/env python3
"""Probe whether a model can actually SEARCH and FETCH — not whether it says it did.

``requalify_model.py`` measures tool calling. A search seat makes a different
claim: that the model reaches live web content the harness did not give it. A
tool-call probe cannot tell those apart, because a model that emits a perfectly
formed tool call and then invents the answer scores identically to one that
retrieved it.

So this probe is built around a **deterministic oracle**, per contract §17.4 and
``authority_limits.deterministic_oracle_preferred_over_model_judge``: the model
is asked to fetch a URL whose exact content this script also fetches, and the
verdict is a substring check against ground truth the harness holds. No model
sits in the verdict path.

Three tasks, in ascending strength:

* **fetch_known** — retrieve a URL with stable, short, quotable content and
  reproduce an exact phrase from it. The harness fetches the same URL and
  checks. A model answering from parameters fails, because it cannot know the
  page changed; a model that truly fetched passes deterministically. This is
  the same shape as the §7.3 citation validator FINDING-007 built: record a
  quote, re-read the source, confirm the quote is there.
* **fetch_volatile** — retrieve a page whose content the model cannot have
  memorized, and reproduce a value from it. Guards the case where
  ``fetch_known`` passes from training data rather than retrieval.
* **search_cited** — an open web query, scored only on whether the response
  carries resolvable source URLs. Weaker by construction and recorded as such:
  a URL in the text is evidence of a citation, not evidence the citation was
  read. It is reported, never used to pass a model on its own.

Why the distinction is load-bearing here: §15 retrieval planes are unbuilt, and
a search model is not a substitute for them. A model-internal search returns
content the harness never sees and therefore cannot re-read, so a citation from
it is unverifiable by the FINDING-007 validator. ``fetch_volatile`` is the only
one of the three that can establish retrieval, because it is the only one whose
ground truth cannot already be inside the model — which is why the seat decision
rests on it alone. ``fetch_known`` passing while ``fetch_volatile`` fails is the
signature of a model answering from memory, not from the network.

Known limitation, recorded because it bounds every verdict this tool emits: the
three tasks ask the model to FETCH A NAMED URL. A search-grounded model is not a
browser — it answers queries against an index and may never issue an arbitrary
GET. So a ``fetch_volatile`` failure is evidence that URL retrieval is absent,
NOT that search grounding is. Distinguishing those needs a query-shaped task with
an independently verifiable answer, which this tool does not yet implement.

Streaming is the default per DEC-008; both transports are measured because
``minimax-m3`` was prohibited for a capability that existed in only one of them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from models.throttle import GlobalThrottle  # noqa: E402

EVAL_BASE = "https://litellm-eval-production.up.railway.app"
PROBE_MAX_TOKENS = 2048

#: Stable, tiny, and quotable. The phrase is checked against a live fetch rather
#: than hardcoded, so the probe cannot pass on a stale assumption about the page.
FETCH_KNOWN_URL = "https://example.com"
FETCH_KNOWN_EXPECT = "Example Domain"

#: Content the model cannot have memorized. The first attempt at this used
#: ``httpbin.org/uuid``, which was INVALID: it mints a fresh uuid per request,
#: so the harness's ground truth could never equal the model's. Both models
#: returned a well-formed 36-character uuid and were scored NOT-grounded — the
#: probe was measuring its own broken oracle, not the models.
#:
#: A usable volatile oracle has to be fresh enough that it cannot be memorized
#: and stable enough that two fetches seconds apart agree. Recent commit SHAs on
#: a busy repository satisfy both: they change on the order of hours, and no
#: training corpus contains today's.
FETCH_VOLATILE_URL = "https://api.github.com/repos/python/cpython/commits?per_page=3"

URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")


@dataclass
class TaskResult:
    task: str
    condition: str
    ok: bool
    grounded: bool | None
    latency_seconds: float
    http_status: int | None = None
    chars: int = 0
    urls_returned: list[str] = field(default_factory=list)
    detail: str = ""
    error: str | None = None

    def as_body(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "condition": self.condition,
            "transport_ok": self.ok,
            "grounded": self.grounded,
            "latency_seconds": round(self.latency_seconds, 2),
            "http_status": self.http_status,
            "response_chars": self.chars,
            "urls_returned": self.urls_returned[:5],
            "detail": self.detail,
            "error": (self.error or "")[:300] or None,
        }


def _http_get(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "efah-search-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _content_from(payload: dict[str, Any]) -> str:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return message.get("content") or ""


def _content_from_stream(raw: str) -> str:
    out: list[str] = []
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
                out.append(piece)
    return "".join(out)


def run_task(
    client: httpx.Client,
    key: str,
    model: str,
    *,
    task: str,
    prompt: str,
    expect: str | list[str] | None,
    stream: bool,
    throttle: GlobalThrottle,
) -> TaskResult:
    throttle.acquire()
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": PROBE_MAX_TOKENS,
    }
    if stream:
        body["stream"] = True

    condition = "streaming" if stream else "non_streaming"
    started = time.monotonic()
    try:
        response = client.post(
            f"{EVAL_BASE}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    except httpx.HTTPError as exc:
        return TaskResult(task, condition, False, None, time.monotonic() - started,
                          error=f"{type(exc).__name__}: {exc}")

    latency = time.monotonic() - started
    if response.status_code != 200:
        return TaskResult(task, condition, False, None, latency,
                          http_status=response.status_code, error=response.text[:300])

    text = _content_from_stream(response.text) if stream else _content_from(response.json())
    urls = URL_RE.findall(text)

    if expect is None:
        # search_cited: reported, never a pass criterion on its own.
        return TaskResult(task, condition, True, None, latency, 200, len(text), urls,
                          detail=f"{len(urls)} url(s) returned; grounding not harness-verifiable")

    candidates = [expect] if isinstance(expect, str) else list(expect)
    hit = next((c for c in candidates if c.lower() in text.lower()), None)
    return TaskResult(
        task, condition, True, hit is not None, latency, 200, len(text), urls,
        detail=(f"ground truth reproduced: {hit!r}" if hit
                else f"none of {len(candidates)} ground-truth value(s) present"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+")
    parser.add_argument("--key-env", default="LITELLM_EVAL_MASTER_KEY")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "evidence" / "search-model-probe.json")
    args = parser.parse_args()

    import os
    key = os.environ.get(args.key_env)
    if not key:
        print(f"missing {args.key_env}", file=sys.stderr)
        return 2

    # Ground truth first. If the harness cannot fetch it, the probe has no
    # oracle and must not run — an unverifiable pass is worse than no result.
    try:
        known_body = _http_get(FETCH_KNOWN_URL)
    except Exception as exc:
        print(f"cannot establish ground truth for {FETCH_KNOWN_URL}: {exc}", file=sys.stderr)
        return 3
    if FETCH_KNOWN_EXPECT.lower() not in known_body.lower():
        print(f"ground truth phrase {FETCH_KNOWN_EXPECT!r} not on page; probe invalid",
              file=sys.stderr)
        return 3

    # Three SHAs, not one, and re-read after the run: a commit landing mid-probe
    # would otherwise score a model that genuinely fetched as NOT-grounded. A
    # false negative here is as corrupting as a false positive — it is what
    # produced FINDING-009's retracted conclusion.
    try:
        volatile_expect = [c["sha"] for c in json.loads(_http_get(FETCH_VOLATILE_URL))]
    except Exception as exc:
        print(f"volatile ground truth unavailable ({exc}); fetch_volatile skipped")
        volatile_expect = None

    tasks = [
        ("fetch_known",
         f"Fetch {FETCH_KNOWN_URL} and reply with the exact text of its main heading. "
         "Reply with the heading only.",
         FETCH_KNOWN_EXPECT),
        ("search_cited",
         "Search the web for the current stable release version of the Python "
         "programming language. Reply with the version and the source URLs you used.",
         None),
    ]
    if volatile_expect:
        tasks.insert(1, (
            "fetch_volatile",
            f"Fetch {FETCH_VOLATILE_URL} and reply with the full 40-character commit "
            "sha of the most recent commit it lists. Reply with the sha only.",
            volatile_expect,
        ))

    # from_policy, not a hand-set rate: the 90 rpm ceiling and 0.9s floor are
    # measured facts in model-policy.yaml, and a probe that hardcodes its own
    # pacing can self-inflict the 429s it is supposed to be measuring.
    throttle = GlobalThrottle.from_policy()
    results: list[dict[str, Any]] = []
    with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=180.0) as client:
        for model in args.models:
            for task, prompt, expect in tasks:
                for stream in (False, True):
                    result = run_task(client, key, model, task=task, prompt=prompt,
                                      expect=expect, stream=stream, throttle=throttle)
                    row = result.as_body()
                    row["model"] = model
                    results.append(row)
                    flag = ("grounded" if result.grounded
                            else "NOT-grounded" if result.grounded is False else "n/a")
                    status = "ok" if result.ok else f"FAIL {result.http_status}"
                    print(f"  {model:32} {task:15} {result.condition:14} "
                          f"{status:9} {flag:13} {row['latency_seconds']}s")

    # ONLY fetch_volatile can verify retrieval. fetch_known is necessary but not
    # sufficient: "Example Domain" sits in every training corpus, so passing it
    # is consistent with never having made a request. Counting it here is the
    # bug that made this probe's first run report verified retrieval for two
    # models whose volatile fetch had failed.
    verified = sorted({
        r["model"] for r in results
        if r["task"] == "fetch_volatile" and r["grounded"] is True
    })
    memorizable_only = sorted({
        r["model"] for r in results
        if r["task"] == "fetch_known" and r["grounded"] is True
    } - set(verified))
    payload = {
        "check": "search_and_fetch_capability",
        "contract_ref": "contract_17.4_deterministic_verdict_path_and_7.3_citation_validation",
        "oracle_type": "deterministic_oracle",
        "model_judge_in_verdict_path": False,
        "gateway": EVAL_BASE,
        "max_tokens": PROBE_MAX_TOKENS,
        "ground_truth": {
            "fetch_known_url": FETCH_KNOWN_URL,
            "fetch_known_expect": FETCH_KNOWN_EXPECT,
            "fetch_volatile_url": FETCH_VOLATILE_URL,
            "fetch_volatile_verified_live": bool(volatile_expect),
        },
        "caveat": (
            "search_cited is reported but never a pass criterion: a URL in the response "
            "proves a citation was emitted, not that it was read. Only fetch_known and "
            "fetch_volatile have a harness-held oracle."
        ),
        "models_with_verified_retrieval": verified,
        "models_passing_memorizable_fetch_only": memorizable_only,
        "verification_rule": (
            "a model is credited with retrieval ONLY on fetch_volatile, whose ground "
            "truth cannot be in any training corpus. fetch_known is reported but never "
            "sufficient on its own."
        ),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwritten: {args.out}")
    print(f"verified retrieval: {verified or 'NONE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
