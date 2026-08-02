#!/usr/bin/env python3
"""FINDING-005 transport probe — is per-request channel pinning possible?

FINDING-005 measured that the gate-bearing assurance roles are served from
resold subscription pools (``kiro-pro``, ``gemini-cli``), and that one upstream
channel serves several differently-named models. The finding lists four owner
options. Option **B** — "select ckff's ``官转`` (official-relay) channels
explicitly for gate-bearing roles, **if the platform allows pinning a channel
per request**" — was recorded as *unverified, needs a probe*.

``autonomy-policy.yaml -> question_policy.must_not_ask_about`` includes
``anything_safely_measurable_by_probe``. Whether pinning works is measurable, so
it must be measured before the owner is asked anything. This tool measures it.

Three things are established here, in increasing cost:

1. **Inventory** (0 model requests) — ``GET /api/pricing`` returns ckff's full
   catalogue with the channel prefix carried *in the model name itself*
   (``[官转1] claude-sonnet-4-5``). That is the pinning mechanism, and the pack
   already uses it: ``model-policy.yaml`` routes ``research_challenger`` to
   ``[grok] grok-4.5`` and ``plan_challenger`` to ``[ds2] deepseek-v4-pro``.
   So the question is not *whether* names can pin — it is **which official
   channels exist, and what they carry**.

2. **Reachability** (N model requests, serial, throttled) — a pinned name is
   only useful if the *eval gateway* forwards it. The eval deployment is DB-less
   (``environments.yaml -> litellm_eval.must_remain_dbless``), so its model list
   is static config the builder cannot edit. If a pinned name is not configured
   there, option B costs an owner gateway change, which is exactly what option A
   costs.

3. **Attribution** (0 extra model requests) — ``GET /api/log/self`` reports the
   channel and group that actually served each request. The probe correlates its
   own requests back to that log, so the conclusion rests on the upstream's own
   accounting rather than on the gateway's echo.

Everything here is read-only against the upstream except the minimal completions
in step 2, which are capped by ``--max-requests`` and dispatched one at a time
through :class:`models.throttle.GlobalThrottle`. An unthrottled fan-out would
self-inflict 429s indistinguishable from genuine model failure — fabricated
evidence, per DEC-301.

Usage::

    PYTHONPATH=src python tools/probe_finding_005.py            # inventory only
    PYTHONPATH=src python tools/probe_finding_005.py --live     # + reachability
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from models.throttle import GlobalThrottle  # noqa: E402

CKFF_BASE = "https://ckff.dev"
EVAL_BASE = "https://litellm-eval-production.up.railway.app"

#: Prefixes the relay operator uses for an official upstream. 官转 = "official
#: relay"; 官 alone prefixes single-vendor official channels (官3, 官4). Matched
#: as a prefix token, not a substring, so ``[三方4]`` (third-party) cannot pass
#: by accident.
OFFICIAL_PREFIX_PATTERN = re.compile(r"^官(转)?\d*$")

#: The roles whose output nothing downstream checks. FINDING-005: a degraded
#: assurance model does not error, it emits plausible tests that pass. These are
#: the roles the transport question is actually about.
GATE_BEARING_ROLES = (
    "visible_test_author",
    "sealed_holdout_author",
    "mutant_author",
    "oracle_author",
    "release_verifier",
    "adversarial_critic",
    "judge",
    "contract_compliance_auditor",
    "integration_verifier",
)


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _secrets() -> dict[str, str]:
    home = Path.home() / ".efah"
    merged = {**_load_env_file(home / "env"), **_load_env_file(home / "ckff.env")}
    merged.update({k: v for k, v in os.environ.items() if k in ("CKFF_TOKEN", "LITELLM_EVAL_MASTER_KEY")})
    return merged


# --------------------------------------------------------------------------
# 1. Inventory
# --------------------------------------------------------------------------


def channel_prefix(model_name: str) -> str | None:
    """The bracketed channel tag a ckff model name carries, if any."""
    match = re.match(r"^\[([^\]]+)\]", model_name.strip())
    return match.group(1).strip() if match else None


def is_official(prefix: str | None) -> bool:
    return bool(prefix) and bool(OFFICIAL_PREFIX_PATTERN.match(prefix or ""))


def fetch_pricing(token: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{CKFF_BASE}/api/pricing",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    response.raise_for_status()
    return list(response.json().get("data") or [])


def inventory(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Which channels exist, which are official, and what the official ones carry."""
    by_prefix: dict[str, list[str]] = {}
    for row in rows:
        name = str(row.get("model_name") or "")
        if not name:
            continue
        by_prefix.setdefault(channel_prefix(name) or "", []).append(name)

    official = {p: sorted(v) for p, v in by_prefix.items() if is_official(p)}
    official_models = sorted(n for names in official.values() for n in names)

    return {
        "catalogue_size": sum(len(v) for v in by_prefix.values()),
        "distinct_channel_prefixes": len([p for p in by_prefix if p]),
        "unprefixed_models": len(by_prefix.get("", [])),
        "official_channel_prefixes": sorted(official),
        "official_models": official_models,
        "official_model_count": len(official_models),
        "pinning_mechanism": "channel_tag_is_part_of_the_model_name",
        "pinning_mechanically_possible": bool(official_models),
    }


def role_coverage(policy: dict[str, Any], official_models: list[str]) -> dict[str, Any]:
    """For each gate-bearing role, is there an official route to *its* model?

    Matched on the bare model id inside the official name, so
    ``[官转1] claude-sonnet-4-5`` covers ``claude-sonnet-4-5`` and nothing else.
    A role whose model has no official variant cannot be pinned without also
    changing which model does the work — which is a capability decision, not a
    transport one, and therefore the owner's.
    """
    aliases = policy.get("aliases") or {}
    official_bare = {
        re.sub(r"^\[[^\]]+\]\s*", "", n).split("[")[0].strip(): n for n in official_models
    }

    rows = []
    for role in GATE_BEARING_ROLES:
        entry = aliases.get(role) or {}
        model = str(entry.get("litellm_model") or "")
        bare = re.sub(r"^\[[^\]]+\]\s*", "", model).strip()
        official_equivalent = official_bare.get(bare)
        rows.append(
            {
                "role": role,
                "configured_model": model,
                "configured_family": entry.get("family"),
                "currently_pinned": channel_prefix(model) is not None,
                "official_route_to_same_model": official_equivalent,
                "coverable_by_option_b_without_changing_model": official_equivalent is not None,
            }
        )
    covered = [r for r in rows if r["coverable_by_option_b_without_changing_model"]]
    return {
        "roles": rows,
        "covered": len(covered),
        "total": len(rows),
        "option_b_fully_covers_gate_bearing_roles": len(covered) == len(rows),
    }


# --------------------------------------------------------------------------
# 2. Reachability
# --------------------------------------------------------------------------


def eval_gateway_models(key: str) -> list[str]:
    response = httpx.get(
        f"{EVAL_BASE}/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=60.0,
    )
    response.raise_for_status()
    return sorted(str(m.get("id")) for m in response.json().get("data") or [])


def probe_one(key: str, model: str, throttle: GlobalThrottle) -> dict[str, Any]:
    """One minimal completion. Zero client retries: DEC-002 makes a retry the
    recorded configuration does not mention a provenance failure, and this probe
    is evidence like any other run."""
    reservation = throttle.acquire()
    started = time.monotonic()
    record: dict[str, Any] = {
        "model_requested": model,
        "throttle_wait_seconds": round(reservation.waited_seconds, 3),
    }
    try:
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=120.0) as client:
            response = client.post(
                f"{EVAL_BASE}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
            )
        record["http_status"] = response.status_code
        record["latency_seconds"] = round(time.monotonic() - started, 3)
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.status_code == 200:
            record["model_echoed"] = body.get("model")
            record["forwarded_by_eval_gateway"] = True
            choices = body.get("choices") or [{}]
            record["text"] = (choices[0].get("message") or {}).get("content", "")[:80]
        else:
            record["forwarded_by_eval_gateway"] = False
            record["error"] = json.dumps(body, ensure_ascii=False)[:400] or response.text[:400]
    except httpx.HTTPError as exc:  # transport failure is a measurement too
        record["http_status"] = None
        record["latency_seconds"] = round(time.monotonic() - started, 3)
        record["forwarded_by_eval_gateway"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


# --------------------------------------------------------------------------
# 3. Attribution
# --------------------------------------------------------------------------


def consumption_log(token: str, pages: int = 2, page_size: int = 100) -> Iterator[dict[str, Any]]:
    for page in range(pages):
        response = httpx.get(
            f"{CKFF_BASE}/api/log/self",
            params={"p": page, "page_size": page_size, "type": 0},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
        response.raise_for_status()
        items = (response.json().get("data") or {}).get("items") or []
        if not items:
            return
        yield from items


def attribute(records: list[dict[str, Any]], token: str, since: int) -> list[dict[str, Any]]:
    """Ask the upstream which channel served each probe request.

    The gateway's echoed ``model`` is the gateway's claim; the account log is the
    operator's own accounting. Where they disagree, the log wins — that
    disagreement is precisely what FINDING-005 is about.
    """
    log = [entry for entry in consumption_log(token) if int(entry.get("created_at") or 0) >= since - 5]
    for record in records:
        requested = record.get("model_requested")
        match = next((e for e in log if str(e.get("model_name")) == requested), None)
        if match is None:
            record["upstream_attribution"] = None
            continue
        record["upstream_attribution"] = {
            "channel": match.get("channel"),
            "channel_name": match.get("channel_name"),
            "group": match.get("group"),
            "model_name_logged": match.get("model_name"),
            "request_id": match.get("request_id"),
        }
    return records


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="spend model requests on the reachability probe")
    parser.add_argument("--max-requests", type=int, default=4, help="hard cap on completions dispatched")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "evidence" / "FINDING-005-transport-probe.json",
    )
    args = parser.parse_args()

    secrets = _secrets()
    ckff_token = secrets.get("CKFF_TOKEN")
    eval_key = secrets.get("LITELLM_EVAL_MASTER_KEY")
    if not ckff_token:
        print("MISSING_REQUIRED_CREDENTIAL: CKFF_TOKEN", file=sys.stderr)
        return 2

    import yaml

    policy = yaml.safe_load((REPO_ROOT / "project-pack" / "model-policy.yaml").read_text())

    rows = fetch_pricing(ckff_token)
    inv = inventory(rows)
    coverage = role_coverage(policy, inv["official_models"])

    result: dict[str, Any] = {
        "finding": "FINDING-005",
        "probe": "transport_channel_pinning",
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream": CKFF_BASE,
        "eval_gateway": EVAL_BASE,
        "inventory": inv,
        "gate_bearing_role_coverage": coverage,
        "evidence_tier": "DETERMINISTIC_ORACLE",
    }

    if eval_key:
        gateway_models = eval_gateway_models(eval_key)
        result["eval_gateway_models"] = gateway_models
        result["official_models_configured_on_eval_gateway"] = sorted(
            set(gateway_models) & set(inv["official_models"])
        )
    else:
        result["eval_gateway_models"] = None
        result["eval_gateway_note"] = "LITELLM_EVAL_MASTER_KEY absent; gateway list not measured"

    if args.live:
        if not eval_key:
            print("MISSING_REQUIRED_CREDENTIAL: LITELLM_EVAL_MASTER_KEY", file=sys.stderr)
            return 2
        # One official name and one currently-configured assurance name. The
        # pair is the whole experiment: does a pinned name pass through, and
        # where does the unpinned one actually land?
        # An official route in the *same family* as the sealed-holdout author,
        # so the comparison isolates the transport rather than the vendor.
        candidates = [m for m in inv["official_models"] if "claude" in m.lower()][:1]
        if not candidates:
            candidates = inv["official_models"][:1]
        aliases = policy.get("aliases") or {}
        configured = str((aliases.get("sealed_holdout_author") or {}).get("litellm_model") or "")
        if configured:
            candidates.append(configured)
        candidates = candidates[: args.max_requests]

        throttle = GlobalThrottle.from_policy()
        since = int(time.time())
        records = [probe_one(eval_key, model, throttle) for model in candidates]
        time.sleep(3)  # the operator's log is written asynchronously
        result["reachability"] = attribute(records, ckff_token, since)
        result["requests_dispatched"] = len(records)
    else:
        result["reachability"] = None
        result["requests_dispatched"] = 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "eval_gateway_models"}, ensure_ascii=False, indent=2))
    print(f"\nwritten: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
