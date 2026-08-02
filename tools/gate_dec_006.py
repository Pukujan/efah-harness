#!/usr/bin/env python3
"""DEC-006 option B — measured evidence that the verifier boundary exists and holds.

Contract §17.2 · GATE-D1-08 A4 · DEC-006. `oracle_type: deterministic_execution_or_state`.
No model participates in this verdict path.

Six checks, each of which fails loudly rather than degrading quietly:

A  the verifier service identity exists and is a different uid from the builder
B  the builder's attempt to read the sealed store is refused by the kernel
C  the generator is root-owned, so the account that runs it cannot rewrite it
D  the sudoers grant is scoped to the generator and grants no unrestricted command
E  the seam returns a valid receipt shape and no content, invoked for real
F  the generator's model assignments match model-policy.yaml

F exists because the generator is deliberately unable to import the harness — it
would otherwise depend on code the builder can rewrite, which would make the
separation theatre. The cost of that independence is a duplicated constant, and
the mitigation for a duplicated constant is a check that compares them. Drift
here would mean the sealed side is quietly using a different model from the one
the pack records, which is a §18 provenance failure.

Run from the **builder** identity. Run as root and the store becomes readable,
B passes for the wrong reason, and the evidence says something untrue — so a
root invocation is refused.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import yaml  # noqa: E402

from verifier_identity.identity import measure  # noqa: E402
from verifier_identity.seam import (  # noqa: E402
    PERMITTED_RECEIPT_FIELDS,
    GenerationRequest,
    GenerationSeam,
)

GENERATOR_SOURCE = REPO_ROOT / "deploy" / "verifier" / "generator.py"


def _result(check: str, claim: str, passed: bool, detail: str, **extra: Any) -> dict[str, Any]:
    return {"check": check, "claim": claim, "passed": passed, "detail": detail, **extra}


def check_a(m) -> dict[str, Any]:
    return _result(
        "A",
        "the verifier service identity exists and differs from the builder's",
        bool(m.identities_distinct and m.verifier_uid is not None),
        f"builder uid={m.builder_uid} ({m.builder_user}), verifier uid={m.verifier_uid} ({m.identity.user})",
        builder_uid=m.builder_uid,
        verifier_uid=m.verifier_uid,
    )


def check_b(m) -> dict[str, Any]:
    can_read, detail = m.builder_read_attempt
    # An absent store is not a boundary. Only an actual refusal counts.
    refused = (not can_read) and "does not exist" not in detail
    return _result(
        "B",
        "the builder's read of the sealed store is refused by the kernel",
        refused and not m.measured_as_root,
        detail,
        method="os.listdir under the builder's effective identity",
        measured_as_root=m.measured_as_root,
    )


def check_c(m) -> dict[str, Any]:
    facts = m.generator
    root_owned = facts.owner_uid == 0
    not_group_writable = facts.mode in {"0o755", "0o555", "0o750", "0o550"}
    return _result(
        "C",
        "the generator is root-owned, so the verifier identity cannot rewrite it",
        bool(facts.exists and root_owned and not_group_writable),
        f"owner={facts.owner_name} mode={facts.mode}",
    )


def check_d(m) -> dict[str, Any]:
    sudoers = m.sudoers or {}
    if not sudoers.get("readable"):
        return _result("D", "the sudoers grant is scoped to the generator", False,
                       f"could not read the drop-in: {sudoers.get('reason')}")
    rules = sudoers.get("rules") or []
    grants = [r for r in rules if "NOPASSWD" in r or ("ALL=" in r and not r.startswith("Defaults"))]
    scoped = bool(grants) and not sudoers.get("grants_unrestricted_command")
    return _result(
        "D",
        "the sudoers grant names the generator and no unrestricted command",
        scoped,
        "; ".join(rules),
        # Stated in the evidence, not only in the doc: this is an audit boundary.
        honest_note=(
            "the builder holds blanket passwordless sudo independently of this rule, "
            "so a narrow grant restricts the sanctioned path and makes out-of-band "
            "access visible in the audit log; it does not prevent it (DEC-006)"
        ),
    )


def check_e(m) -> dict[str, Any]:
    """Invoke the seam for real and inspect what came back.

    The generator refuses while FINDING-005 is unanswered, which is the point:
    the refusal is a valid receipt with a typed failure class, so the seam is
    exercised end to end without generating anything.
    """
    if not m.provisioned:
        return _result("E", "the seam returns a receipt shape and no content", False,
                       "not provisioned; the seam was not invoked")
    seam = GenerationSeam()
    outcome = seam.generate(
        GenerationRequest(
            generation_request_id=f"GEN-PROBE-{int(time.time())}",
            candidate_commit="0" * 40,
            contract_version="1.1",
            target_count=1,
        )
    )
    evidence = outcome.as_evidence()
    receipt = evidence.get("receipt") or {}
    shape_ok = bool(receipt) and set(receipt) <= set(PERMITTED_RECEIPT_FIELDS) | {"mint_accepted"}
    return _result(
        "E",
        "the seam returns a valid receipt shape and no content",
        bool(shape_ok and not evidence["rejected_because"]),
        f"state={evidence['state']} exit_status={receipt.get('exit_status')} "
        f"failure_class={receipt.get('failure_class')}",
        receipt=receipt,
        stderr_read_by_builder=evidence["stderr_read_by_builder"],
        stdout_bytes_discarded=evidence["stdout_bytes_discarded"],
    )


def check_f() -> dict[str, Any]:
    """The generator cannot import the pack, so its constants are compared instead."""
    policy = yaml.safe_load((REPO_ROOT / "project-pack" / "model-policy.yaml").read_text())
    aliases = policy.get("aliases") or {}
    expected = {
        "HOLDOUT_AUTHOR_MODEL": str((aliases.get("sealed_holdout_author") or {}).get("litellm_model")),
        "MUTANT_AUTHOR_MODEL": str((aliases.get("mutant_author") or {}).get("litellm_model")),
    }
    source = GENERATOR_SOURCE.read_text()
    drift: list[str] = []
    found: dict[str, str | None] = {}
    for name, want in expected.items():
        match = re.search(rf'^{name}\s*=\s*"([^"]+)"', source, re.M)
        actual = match.group(1) if match else None
        found[name] = actual
        if actual != want:
            drift.append(f"{name}: generator has {actual!r}, pack has {want!r}")
    return _result(
        "F",
        "the generator's model assignments match model-policy.yaml",
        not drift,
        "; ".join(drift) or "generator constants match the pack",
        expected=expected,
        found=found,
    )


def main() -> int:
    m = measure()
    if m.measured_as_root:
        print(
            "REFUSING: run this from the builder identity. As root the store is "
            "readable regardless, and check B would pass for the wrong reason.",
            file=sys.stderr,
        )
        return 2

    checks = [check_a(m), check_b(m), check_c(m), check_d(m), check_e(m), check_f()]
    passed = sum(1 for c in checks if c["passed"])

    report = {
        "gate": "DEC-006",
        "name": "verifier service identity and sealed store",
        "contract_ref": "contract_17.2_protected_verifier_architecture",
        "gate_ref": "GATE-D1-08 A4",
        "oracle_type": "deterministic_execution_or_state",
        "model_judge_in_verdict_path": False,
        "evidence_tier": "DETERMINISTIC_ORACLE",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": "PASS" if passed == len(checks) else "FAIL",
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "identity_measurement": m.as_body(),
        "honest_debt": m.as_body()["honest_debt"],
        "generation_status": (
            "BLOCKED_OWNER_DECISION: sealed holdout content is not generated while "
            "FINDING-005 is unanswered. The refusal is enforced by the generator, "
            "which requires a transport decision recorded inside the verifier's own "
            "0700 directory - the builder cannot write it to unblock itself."
        ),
    }

    out = REPO_ROOT / "evidence" / "DEC-006-verifier-identity.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")

    for check in checks:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  {check['check']}  {mark}  {check['claim']}")
        print(f"        {check['detail']}")
    print(f"\nDEC-006: {report['verdict']} ({passed}/{len(checks)})")
    print(f"written: {out}", file=sys.stderr)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
