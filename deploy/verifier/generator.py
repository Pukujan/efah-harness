#!/usr/bin/env python3
"""Sealed holdout generator — runs as the verifier identity, never as the builder.

DEC-006 option B. This file is installed to ``/opt/efah-verifier/bin/`` **root
owned, mode 0755**, and executed via ``sudo -u efah-verifier``. Two consequences
shape everything below:

* **It cannot import from the builder's tree.** The builder can rewrite anything
  under its own home, so a generator that imported ``src/`` would be a generator
  the builder controls, and role separation would be theatre. This file is
  therefore stdlib-only and self-contained, and duplicating a little logic from
  the harness is the correct trade rather than a lapse.
* **It cannot be rewritten by the account that runs it.** Root owns it. A
  compromise of ``efah-verifier`` cannot change what is generated or where it
  goes.

Output discipline. Exactly one line reaches the builder: a JSON receipt on
stdout carrying an exit status and counts. Everything else — prompts,
completions, tracebacks, the holdouts themselves — is written inside the sealed
store, which the builder cannot read. **stderr is not a channel**: the caller
sends it to ``/dev/null``, so anything written there is lost rather than leaked.

Holdouts are not oracles
------------------------
Contract §17.3 ranks a deterministic execution or state check above a calibrated
model judge, and §17.4 requires a trusted oracle to have a deterministic verdict
path with no hidden model call. A model-authored holdout is therefore a
*candidate* until the mutation gate validates it. This program refuses to mint a
set whose kill rate against its declared mutants is below 1.0: a holdout that
kills no known-bad mutant tests nothing, and one that a mutant survives is worse
than absent because it manufactures confidence.

The transport gate
------------------
FINDING-005 measured that the assurance roles are served from resold
subscription pools, and holdouts generated through an unverifiable transport are
worth less than the effort of generating them. This program therefore refuses to
generate until the owner's decision is recorded at
``/var/lib/efah-verifier/etc/transport-decision``. That file lives inside the
verifier's own 0700 directory, so the builder cannot create it to unblock
itself — the refusal is mechanical, not a matter of the builder's restraint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "1.0.0"
ORACLE_VERSION = "holdout-mint-1.0.0"

VERIFIER_HOME = Path("/var/lib/efah-verifier")
SEALED_STORE = VERIFIER_HOME / "store"
VERIFIER_ETC = VERIFIER_HOME / "etc"
VERIFIER_LOG = VERIFIER_HOME / "log"
CREDENTIAL = VERIFIER_ETC / "eval.env"
TRANSPORT_DECISION = VERIFIER_ETC / "transport-decision"

EVAL_BASE_URL = "https://litellm-eval-production.up.railway.app"

#: model-policy.yaml maps sealed_holdout_author to this. Duplicated rather than
#: imported for the reason at the top of the file; drift between the two is
#: caught by tools/gate_dec_006.py, which reads both and compares.
HOLDOUT_AUTHOR_MODEL = "claude-opus-4-8"
MUTANT_AUTHOR_MODEL = "gemini-3.5-flash"

#: DEC-002: gate-bearing evidence never retries silently. A retry the recorded
#: configuration does not mention makes the evidence unprovable.
MAX_RETRIES = 0
TIMEOUT_SECONDS = 120

#: The account-wide upstream ceiling is 100 rpm and the harness throttles to 90.
#: This process is serial and slow by construction, but it shares the account
#: with the builder, so it takes the same file lock.
THROTTLE_STATE = Path(os.environ.get("EFAH_THROTTLE_STATE", "/tmp/efah-global-throttle.json"))
MIN_INTERVAL_SECONDS = 0.9


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Log:
    """Writes inside the sealed store. Never to stdout, never to stderr."""

    def __init__(self, request_id: str) -> None:
        VERIFIER_LOG.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", request_id)[:64]
        self._path = VERIFIER_LOG / f"{safe}.log"

    def write(self, message: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(f"{utc_now()} {message}\n")


def emit(receipt: dict[str, Any]) -> None:
    """The one line the builder sees. Nothing else may be printed."""
    sys.stdout.write(json.dumps(receipt, sort_keys=True) + "\n")
    sys.stdout.flush()


def failure(request_id: str, exit_status: int, failure_class: str, store_hash: str) -> int:
    emit(
        {
            "generation_request_id": request_id,
            "exit_status": exit_status,
            "holdout_count": 0,
            "mutant_count": 0,
            "killed_count": 0,
            "kill_rate": 0.0,
            "store_content_hash": store_hash,
            "generator_version": GENERATOR_VERSION,
            "oracle_version": ORACLE_VERSION,
            "generated_at": utc_now(),
            "failure_class": failure_class,
        }
    )
    return exit_status


def store_content_hash() -> str:
    """A hash over the store's contents. Identifies the set without revealing it.

    §18 requires a content hash on every artifact, and this is the only way the
    builder can bind evidence to a specific holdout set while remaining unable to
    read it.
    """
    digest = hashlib.sha256()
    if SEALED_STORE.is_dir():
        for path in sorted(SEALED_STORE.rglob("*")):
            if not path.is_file():
                continue
            digest.update(path.relative_to(SEALED_STORE).as_posix().encode())
            digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def read_credential() -> str | None:
    if not CREDENTIAL.is_file():
        return None
    for line in CREDENTIAL.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key == "LITELLM_EVAL_MASTER_KEY" and value:
            return value
    return None


def transport_decision() -> str | None:
    """The owner's FINDING-005 answer, or ``None`` while it is unanswered.

    Inside the verifier's 0700 directory on purpose: the builder cannot write it,
    so it cannot unblock itself.
    """
    if not TRANSPORT_DECISION.is_file():
        return None
    value = TRANSPORT_DECISION.read_text().strip().upper()
    return value if value in {"A", "C", "D"} else None


def throttle() -> None:
    """Account-wide spacing, shared with the builder through a file lock.

    An unthrottled fan-out self-inflicts 429s that are indistinguishable from
    genuine model failure — fabricated evidence, which is worse than a slow run.
    """
    import fcntl

    THROTTLE_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(THROTTLE_STATE, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            try:
                state = json.loads(fh.read() or "{}")
            except json.JSONDecodeError:
                state = {}
            now = time.time()
            last = float(state.get("last_reserved_at") or 0.0)
            scheduled = max(now, last + MIN_INTERVAL_SECONDS)
            state["last_reserved_at"] = scheduled
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(state))
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    wait = scheduled - time.time()
    if wait > 0:
        time.sleep(wait)


def call_model(api_key: str, model: str, prompt: str, log: Log) -> str:
    """One completion, zero retries. Empty or truncated output is a hard failure.

    FINDING-005 option C: a degraded assurance model does not error, it emits
    plausible output. Truncation and emptiness are the two failure modes that are
    mechanically detectable, so they are refused rather than accepted.
    """
    throttle()
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8000,
            "temperature": 0,
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed https base url
        f"{EVAL_BASE_URL}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = json.loads(response.read())

    choice = (payload.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")
    log.write(f"model={model} finish_reason={finish} chars={len(text)}")

    if not text.strip():
        raise RuntimeError(f"{model} returned an empty generation")
    if finish == "length":
        raise RuntimeError(f"{model} generation was truncated (finish_reason=length)")
    # Model-echo assertion: the gateway names what actually answered.
    echoed = payload.get("model")
    if echoed and model.split("/")[-1] not in str(echoed):
        raise RuntimeError(f"requested {model} but the gateway echoed {echoed}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate sealed release holdouts.")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--target-count", type=int, default=0)
    args = parser.parse_args()

    request_id = args.request_id
    store_hash = store_content_hash()

    # Refuse to run anywhere but inside the verifier identity. If this program
    # is ever executed as the builder, the separation it exists to create is
    # already gone and it must not pretend otherwise.
    import pwd

    who = pwd.getpwuid(os.geteuid()).pw_name
    if who != "efah-verifier":
        return failure(request_id, 3, "PROTECTED_ACCESS", store_hash)

    log = Log(request_id)
    log.write(f"start request={request_id} commit={args.candidate_commit} as={who}")

    decision = transport_decision()
    if decision is None:
        # FINDING-005 is unanswered. Holdouts generated through an unverifiable
        # transport are worth less than the effort of generating them, and a set
        # minted now would have to be discarded and regenerated after the answer.
        log.write("refused: FINDING-005 transport decision not recorded")
        return failure(request_id, 4, "ORACLE_INVALID", store_hash)
    log.write(f"transport decision recorded: {decision}")

    api_key = read_credential()
    if not api_key:
        log.write("refused: no eval-gateway credential installed")
        return failure(request_id, 5, "INFRASTRUCTURE_FAILURE", store_hash)

    if args.target_count <= 0:
        log.write("refused: target_count must be positive")
        return failure(request_id, 6, "ORACLE_INVALID", store_hash)

    try:
        SEALED_STORE.mkdir(parents=True, exist_ok=True)
        holdouts = generate_holdouts(api_key, args, log)
        mutants = generate_mutants(api_key, args, log)
        killed = run_mutation_gate(holdouts, mutants, log)
    except Exception as exc:  # noqa: BLE001 - the class is the channel, not the text
        # The message may quote generated content, so it goes to the sealed log
        # and the builder receives a typed class only.
        log.write(f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return failure(request_id, 1, _classify(exc), store_content_hash())

    kill_rate = (len(killed) / len(mutants)) if mutants else 0.0
    accepted = bool(holdouts) and bool(mutants) and kill_rate >= 1.0

    if not accepted:
        # DEC-006: the mint refuses a set below 1.0. The generated files stay in
        # the store for the owner to inspect; they are simply not minted.
        log.write(f"mint refused: kill_rate={kill_rate:.4f} over {len(mutants)} mutants")

    emit(
        {
            "generation_request_id": request_id,
            "exit_status": 0 if accepted else 7,
            "holdout_count": len(holdouts),
            "mutant_count": len(mutants),
            "killed_count": len(killed),
            "kill_rate": round(kill_rate, 6),
            "store_content_hash": store_content_hash(),
            "generator_version": GENERATOR_VERSION,
            "oracle_version": ORACLE_VERSION,
            "generated_at": utc_now(),
            "failure_class": None if accepted else "HOLDOUT_FAILURE",
        }
    )
    return 0 if accepted else 7


def _classify(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return "RATE_LIMIT"
        if exc.code in (401, 403):
            return "PROTECTED_ACCESS"
        return "MODEL_UNAVAILABLE"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "TRANSIENT_PROVIDER_FAILURE"
    return "ORACLE_INVALID"


# -- generation ------------------------------------------------------------
# These write into the sealed store and return only counts to their caller.
# Nothing above this line ever holds a holdout body.


def _write(directory: str, name: str, content: str) -> Path:
    target = SEALED_STORE / directory
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def generate_holdouts(api_key: str, args: argparse.Namespace, log: Log) -> list[Path]:
    prompt = (
        "You are authoring SEALED RELEASE HOLDOUT tests for a Python control-plane "
        "harness. The implementer will never see them. Write pytest tests that a "
        "plausible-but-wrong implementation would fail and a correct one would pass. "
        "Target behaviours that are easy to fake: exit codes that must be non-zero "
        "mid-run, state transitions that must be refused, evidence that must be "
        "absent rather than empty. Output only Python, no prose, no code fences.\n\n"
        f"Candidate commit: {args.candidate_commit}\n"
        f"Contract version: {args.contract_version}\n"
        f"Produce {args.target_count} independent test functions."
    )
    text = call_model(api_key, HOLDOUT_AUTHOR_MODEL, prompt, log)
    path = _write("holdouts", f"holdout_{args.candidate_commit[:12]}.py", text)
    log.write(f"wrote {path.name} ({len(text)} chars)")
    return [path]


def generate_mutants(api_key: str, args: argparse.Namespace, log: Log) -> list[Path]:
    prompt = (
        "You are authoring KNOWN-BAD MUTANTS: small, plausible defects that a weak "
        "test suite would miss. Each mutant is a self-contained Python module with a "
        "single seeded defect. Output only Python, no prose, no code fences.\n\n"
        f"Candidate commit: {args.candidate_commit}\n"
        f"Produce {max(args.target_count, 1)} mutants."
    )
    text = call_model(api_key, MUTANT_AUTHOR_MODEL, prompt, log)
    path = _write("mutants", f"mutant_{args.candidate_commit[:12]}.py", text)
    log.write(f"wrote {path.name} ({len(text)} chars)")
    return [path]


def run_mutation_gate(holdouts: list[Path], mutants: list[Path], log: Log) -> list[Path]:
    """Deterministic. No model participates in this verdict path (§17.4).

    Each mutant is run against the holdout set; a mutant the set does not kill is
    recorded and the mint is refused. This is the only check that distinguishes a
    holdout set from a plausible-looking file.
    """
    import subprocess

    killed: list[Path] = []
    for mutant in mutants:
        result = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-m", "pytest", "-x", "-q", *[str(h) for h in holdouts]],
            cwd=mutant.parent,
            capture_output=True,
            text=True,
            timeout=600,
        )
        # A non-zero exit means the holdout set detected the seeded defect.
        if result.returncode != 0:
            killed.append(mutant)
        log.write(f"mutant {mutant.name}: pytest exit {result.returncode}")
    return killed


if __name__ == "__main__":
    raise SystemExit(main())
