#!/usr/bin/env python3
"""Collect GATE-D1-10's five required evidence artifacts.

Contract v1.1 §11.7 / AMENDMENT-001. The gate names its evidence explicitly, and
§18 is blunt that "done" without named evidence is invalid -- so these are
produced by exercising the live surface, not described.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

#: The MagicDNS name, not the IP. `tailscale serve` routes by Host header, so
#: the bare address 404s -- and the address was the thing that was wrong
#: (FINDING-004).
BASE = os.environ.get("EFAH_SURFACE_URL", "http://gravebuster.tail733a0f.ts.net:8088")
OUT = Path("evidence/gates/GATE-D1-10")
SURFACE_PKG = Path("src/owner_surface")


def _post(text: str, verb: str = "INSTRUCT", target: str | None = None) -> dict:
    r = httpx.post(
        f"{BASE}/owner/command",
        json={"text": text, "verb": verb, "target_id": target, "contract_version": "1.1"},
        timeout=20,
    )
    return r.json()


def import_graph_report() -> dict:
    """A2 — no code path in the surface imports a vendor SDK."""
    forbidden = {"anthropic", "claude_agent_sdk", "claude_code_sdk"}
    modules = {}
    for path in sorted(SURFACE_PKG.rglob("*.py")):
        roots: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
        modules[str(path)] = sorted(roots)
    offenders = {m: sorted(set(r) & forbidden) for m, r in modules.items() if set(r) & forbidden}
    return {"scanned": len(modules), "imports": modules, "offenders": offenders,
            "zero_anthropic_imports": not offenders}


def credential_stripped_transcript() -> dict:
    """A1 — the serving process holds no Anthropic credential."""
    pid = subprocess.run(
        ["systemctl", "--user", "show", "-p", "MainPID", "--value", "efah-owner-surface"],
        capture_output=True, text=True,
    ).stdout.strip()
    env_names: list[str] = []
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
        env_names = [e.split("=", 1)[0] for e in raw.split("\0") if e]
    except OSError:
        pass
    vendor = [n for n in env_names if "ANTHROPIC" in n.upper() or "CLAUDE" in n.upper()]
    return {
        "service": "efah-owner-surface", "main_pid": pid,
        "environment_variable_count": len(env_names),
        "anthropic_or_claude_variables": vendor,
        "health": httpx.get(f"{BASE}/owner/health", timeout=10).json(),
    }


def negative_controls() -> dict:
    """A6, A7, A8 — the refusals, captured live."""
    return {
        "A6_scope_expansion": _post("migrate to temporal for the workflow runtime"),
        "A6_requirement_weakening": _post("relax the mutation gate requirement"),
        "A7_gate_bypass": _post("skip the holdout gate and merge"),
        "A7_self_approve": _post("self-approve this PR"),
        "A8_sealed_repo": _post("show me the sealed holdout content"),
        "A8_protected_store": _post("connect to terminusdb_protected"),
        "stale_contract_version": httpx.post(
            f"{BASE}/owner/command",
            json={"text": "status", "verb": "OBSERVE", "target_id": None, "contract_version": "1.0"},
            timeout=20).json(),
    }


def blocker_round_trip() -> dict:
    """A4 — answer an open typed blocker and show it recorded."""
    before = httpx.get(f"{BASE}/owner/blockers", timeout=10).json()
    answered = None
    if before:
        answered = _post("B", verb="ANSWER_BLOCKER", target=before[0]["blocker_id"])
    after = httpx.get(f"{BASE}/owner/blockers", timeout=10).json()
    return {"open_before": before, "answer_outcome": answered, "open_after": after,
            "state": httpx.get(f"{BASE}/owner/state", timeout=10).json()}


#: Addresses that are this machine. A command from one of these is the builder
#: testing itself, which is precisely what FINDING-004 recorded.
SELF_ORIGINS = {"127.0.0.1", "::1", "localhost", "100.93.66.35", ""}
#: NOTE: 100.93.66.35 is this host's own tailnet address, so a command
#: relayed from here through the proxy still counts as self-origin.


def owner_originated_command() -> dict:
    """A9's client half — a command that came from OFF this host.

    FINDING-004: A9 names a client-side condition ("usable from a mobile
    viewport over the private network"), and it was reported PASS on a
    screenshot taken by headless Chrome running on this machine. That proved the
    server renders; it did not prove the owner could reach it. In fact they could
    not, for the whole time it was reported green.

    So the artifact is now a recorded command whose origin is not this host. The
    builder cannot manufacture one.
    """
    ledger = Path(os.environ.get("EFAH_OWNER_LEDGER", ".data/owner_surface_ledger.jsonl"))
    rows = []
    if ledger.is_file():
        for line in ledger.read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    commands = [r for r in rows if r.get("kind") == "owner_command"]
    off_host = [
        {"record_id": r.get("record_id"), "at": r.get("at"),
         "origin": r["body"]["body"].get("origin"), "verb": r["body"]["body"].get("verb"),
         "accepted": r["body"]["body"].get("accepted"),
         "content_hash": r["body"]["envelope"].get("content_hash")}
        for r in commands
        # A MISSING origin is not an off-host origin. Commands recorded before
        # the field existed carry None, and treating that as "came from
        # elsewhere" would be absence-as-success -- the same shape of error
        # FINDING-004 is about, reproduced inside its own remedy.
        if isinstance(r["body"]["body"].get("origin"), str)
        and r["body"]["body"]["origin"].strip()
        and r["body"]["body"]["origin"] not in SELF_ORIGINS
    ]
    return {
        "commands_recorded": len(commands),
        "off_host_commands": off_host,
        "satisfied": bool(off_host),
        "note": (
            "A9 requires a command originating off this host. A locally rendered "
            "screenshot does not satisfy it -- see FINDING-004."
        ),
    }


def mobile_screenshot() -> dict:
    """A9 — a real 390px render, not a claim about one."""
    png = OUT / "mobile-390x844.png"
    proc = subprocess.run(
        ["google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--virtual-time-budget=4000",
         f"--screenshot={png}", "--window-size=390,844", f"{BASE}/owner/"],
        capture_output=True, text=True, timeout=120,
    )
    return {"viewport": "390x844", "path": str(png), "exists": png.is_file(),
            "bytes": png.stat().st_size if png.is_file() else 0,
            "chrome_exit": proc.returncode,
            "stderr_tail": proc.stderr.strip().splitlines()[-3:]}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "import_graph_report": import_graph_report(),
        "credential_stripped_run_transcript": credential_stripped_transcript(),
        "negative_control_transcripts_for_A6_A7_A8": negative_controls(),
        "blocker_answer_round_trip_with_terminus_commit": blocker_round_trip(),
        "mobile_viewport_session_recording_or_screenshots": mobile_screenshot(),
        "owner_originated_command": owner_originated_command(),
    }
    for name, payload in artifacts.items():
        (OUT / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
        print(f"  wrote {name}.json")
    ok = (
        artifacts["import_graph_report"]["zero_anthropic_imports"]
        and not artifacts["credential_stripped_run_transcript"]["anthropic_or_claude_variables"]
        and all(not v.get("accepted", True) for v in artifacts["negative_control_transcripts_for_A6_A7_A8"].values())
        and artifacts["mobile_viewport_session_recording_or_screenshots"]["exists"]
        # The half that was missing. Without it A9 is unproven, not passed.
        and artifacts["owner_originated_command"]["satisfied"]
    )
    print("GATE-D1-10 evidence:", "COMPLETE" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
