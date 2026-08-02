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
MUTANT_AUTHOR_MODEL = "kimi-k2.7-code"

#: DEC-002: gate-bearing evidence never retries silently. A retry the recorded
#: configuration does not mention makes the evidence unprovable. Still zero.
MAX_RETRIES = 0

#: 300s, not the 120s recorded for ordinary eval-gateway calls. The holdout
#: author is now claude-opus-5-thinking, a reasoning model asked for up to 8000
#: tokens, and 120s cut it off mid-generation with a socket read timeout —
#: reported as TRANSIENT_PROVIDER_FAILURE, which is true and useless.
#:
#: This does not weaken DEC-002. That decision is about *retries* making the
#: recorded run differ from the run that happened; this is one attempt, still
#: zero retries, just allowed to finish. Recorded here and in model-policy.yaml
#: beside the role, so it is a stated exception rather than silent drift.
TIMEOUT_SECONDS = 300

#: The account-wide upstream ceiling is 100 rpm and the harness throttles to 90.
#: This process is serial and slow by construction, but it shares the account
#: with the builder, so it takes the same file lock.
#: The *shared* limiter, writable by both the builder and this identity. If it
#: is missing the generator refuses rather than pacing itself alone: an
#: unshared throttle on an account-wide limit is not a throttle.
THROTTLE_STATE = Path(os.environ.get("EFAH_THROTTLE_STATE", "/var/lib/efah-throttle/state.json"))
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

    if not THROTTLE_STATE.is_file():
        raise RuntimeError(
            f"shared throttle state {THROTTLE_STATE} is absent; refusing to generate "
            "unthrottled against an account-wide rate limit"
        )
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


def call_model(api_key: str, model: str, prompt: str, log: Log, *, max_tokens: int = 8000) -> str:
    """One completion, zero retries, **streamed**.

    Streaming is not a latency optimisation here, it is a reliability fix. The
    owner's cortex research (2026-07-19, re-scoping an earlier conclusion)
    established it directly:

        "HTTP 524 on long gens (~120s/~16k) was a NON-STREAM artifact …
         streaming keeps the connection alive -> long gens COMPLETE
         (verified 139.5s/~3.2k tok)."

    This generator reproduced the same artifact with different status codes:
    HTTP 408 at 8000 and 4000 max_tokens, HTTP 502 at 16000, all non-streaming,
    all after our own client timeout had been raised to 300s and was proven not
    to be the binding constraint. Three models were nearly written off for it.

    A non-streaming request holds a silent connection while the model works, and
    something between here and the upstream closes it. A streamed request emits
    chunks the whole way and nothing decides it is dead.

    DEC-002 is untouched: still one attempt, still zero retries. The empty and
    truncated checks below stay, because streaming makes a long generation
    *finish*, not *correct*.
    """
    throttle()
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed https base url
        f"{EVAL_BASE_URL}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    chunks: list[str] = []
    finish: str | None = None
    echoed: str | None = None
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
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
            echoed = echoed or event.get("model")
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    chunks.append(delta["content"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]

    text = "".join(chunks)
    log.write(f"model={model} finish_reason={finish} chars={len(text)} streamed=True")

    if not text.strip():
        raise RuntimeError(f"{model} returned an empty generation")
    if finish == "length":
        raise RuntimeError(f"{model} generation was truncated (finish_reason=length)")
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
        subject, holdouts = generate_subject_and_holdouts(api_key, args, log)
        mutants = generate_mutants(api_key, args, subject, log)
        killed, gate_problems = run_mutation_gate(subject, holdouts, mutants, log)
    except Exception as exc:  # noqa: BLE001 - the class is the channel, not the text
        # The message may quote generated content, so it goes to the sealed log
        # and the builder receives a typed class only.
        log.write(f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return failure(request_id, 1, _classify(exc), store_content_hash())

    kill_rate = (len(killed) / len(mutants)) if mutants else 0.0
    # A gate problem voids the score outright. DEC-006's mint rule is that a
    # holdout set below 1.0 is refused; a set whose *gate* could not decide is
    # not at 1.0 either, and reporting it as such would be the manufactured
    # confidence the rule exists to prevent.
    accepted = (
        bool(holdouts) and bool(mutants) and kill_rate >= 1.0 and not gate_problems
    )

    if gate_problems:
        for problem in gate_problems:
            log.write(f"gate problem: {problem}")

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


def generate_subject_and_holdouts(api_key: str, args: argparse.Namespace, log: Log) -> tuple[Path, list[Path]]:
    """Author a reference implementation and the holdouts that pin it.

    The reference exists so the mutation gate has a **baseline**. Without one,
    a holdout that fails unconditionally kills every mutant and reports a
    perfect score — which is precisely the manufactured confidence DEC-006 says
    is worse than having no holdouts at all. The first run of this generator did
    exactly that and reported ``kill_rate: 1.0`` on a set that tested nothing.
    """
    prompt = (
        "Author a SEALED RELEASE HOLDOUT exercise in Python. Output exactly two files, "
        "each fenced and preceded by its filename on its own line as `# FILE: name.py`.\n\n"
        "1. `subject.py` — a small, correct module for a control-plane concern where a "
        "plausible-but-wrong implementation is easy to write. Prefer behaviours that are "
        "easy to fake: a state machine that must REFUSE certain transitions, an exit code "
        "that must be non-zero mid-run, a check that must report absent rather than empty. "
        "Pure functions and simple classes only; no I/O, no network, no clock.\n\n"
        f"2. `test_holdout.py` — {args.target_count} pytest tests importing `subject`. They must "
        "all PASS against the correct implementation, and each must FAIL loudly if the "
        "behaviour is subtly wrong. Assert on specific values, not on truthiness.\n\n"
        "No prose outside the two fenced blocks.\n"
        f"Candidate commit: {args.candidate_commit}\nContract version: {args.contract_version}"
    )
    text = call_model(api_key, HOLDOUT_AUTHOR_MODEL, prompt, log, max_tokens=8000)
    files = _split_files(text)
    log.write(f"holdout author produced files: {sorted(files)}")
    if "subject.py" not in files or "test_holdout.py" not in files:
        raise RuntimeError(f"holdout author returned {sorted(files)}, expected subject.py and test_holdout.py")

    subject = _write("reference", "subject.py", files["subject.py"])
    holdout = _write("holdouts", "test_holdout.py", files["test_holdout.py"])
    log.write(f"reference {len(files['subject.py'])} chars; holdouts {len(files['test_holdout.py'])} chars")
    return subject, [holdout]


def generate_mutants(api_key: str, args: argparse.Namespace, subject: Path, log: Log) -> list[Path]:
    """Seed known-bad variants of the *same* module the holdouts import.

    Coupling matters: the first version of this generator produced free-standing
    "mutant" files that nothing imported, so running the holdouts near them
    measured nothing. A mutant must be a drop-in replacement for the subject or
    it is not a mutant.
    """
    count = max(int(args.target_count), 3)
    prompt = (
        "Here is a correct Python module.\n\n```python\n" + subject.read_text() + "\n```\n\n"
        f"Produce {count} MUTANTS: complete copies of this module, each with exactly ONE "
        "small seeded defect that a weak test suite would miss. Keep every public name and "
        "signature identical — a mutant must be a drop-in replacement. Prefer off-by-one, "
        "inverted conditions, a swallowed error, a wrong default, a state transition that "
        "should be refused but is allowed.\n\n"
        "Output each as a fenced block preceded by `# FILE: mutant_<n>.py`. No prose."
    )
    text = call_model(api_key, MUTANT_AUTHOR_MODEL, prompt, log, max_tokens=8000)
    files = _split_files(text, fallback_stem="mutant_")
    # Filenames only -- metadata, not content. Enough to diagnose a format
    # mismatch without the builder ever seeing a mutant.
    log.write(f"mutant author produced files: {sorted(files)}")
    mutants = [
        _write("mutants", name, body)
        for name, body in sorted(files.items())
        if name.startswith("mutant")
    ]
    if not mutants:
        raise RuntimeError(
            f"mutant author returned no usable modules; parsed {sorted(files)} "
            f"from {len(text)} chars"
        )
    log.write(f"wrote {len(mutants)} mutants")
    return mutants


#: Filename markers seen in practice. The holdout author honoured
#: ``# FILE: name.py`` exactly; the mutant author did not, and a run died on the
#: parser rather than on anything about the mutants. Models are asked for one
#: shape and produce several, so the parser accepts the common ones instead of
#: making instruction-following a precondition for assurance.
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)
_NAME_BEFORE = re.compile(
    r"(?:#\s*FILE:\s*|#{1,6}\s*|\*\*)([A-Za-z0-9_./-]+\.py)\b[^\n]*\n+\s*$"
)
_NAME_INSIDE = re.compile(r"^\s*#\s*(?:FILE:\s*)?([A-Za-z0-9_./-]+\.py)\s*$")


def _split_files(text: str, *, fallback_stem: str | None = None) -> dict[str, str]:
    """Pull ``name.py -> source`` pairs out of a completion.

    Three shapes are accepted: a filename on the line before a fence (with or
    without ``# FILE:``, with or without markdown heading or bold markers), a
    filename as the first comment *inside* the fence, or — when a fallback stem
    is supplied and neither marker is present — positional naming, because a
    model that emitted six unlabelled code blocks when asked for six mutants has
    still done the work.
    """
    files: dict[str, str] = {}
    for index, match in enumerate(_FENCE.finditer(text), start=1):
        body = match.group(1)
        before = text[: match.start()]
        name = None

        head = _NAME_BEFORE.search(before[-200:])
        if head:
            name = head.group(1)
        else:
            first_line = body.splitlines()[0] if body.splitlines() else ""
            inside = _NAME_INSIDE.match(first_line)
            if inside:
                name = inside.group(1)
        if name is None and fallback_stem:
            name = f"{fallback_stem}{index}.py"
        if name:
            files[Path(name).name] = body
    return files


#: A root-owned interpreter with pytest installed. Not ``sys.executable``: the
#: system python this program runs under has no pytest, and ``python -m pytest``
#: without it exits **1** -- indistinguishable from "tests failed". That single
#: collision produced a fabricated ``kill_rate: 1.0`` on a set that ran no tests
#: at all, because every mutant "died" of the runner being absent.
TEST_RUNNER = Path("/opt/efah-verifier/venv/bin/python")


def assert_runner_available() -> None:
    """Prove the test runner exists before any verdict depends on it."""
    import subprocess

    if not TEST_RUNNER.is_file():
        raise RuntimeError(f"test runner {TEST_RUNNER} is absent; no verdict is possible")
    probe = subprocess.run(  # noqa: S603 - fixed argv
        [str(TEST_RUNNER), "-m", "pytest", "--version"],
        capture_output=True, text=True, timeout=60,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            f"{TEST_RUNNER} cannot run pytest (exit {probe.returncode}); a missing runner "
            "exits 1 exactly like a failing test, so every mutant would score as killed"
        )


def _run_pytest(directory: Path, holdouts: list[Path]) -> int:
    import subprocess

    result = subprocess.run(  # noqa: S603 - fixed argv
        [str(TEST_RUNNER), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         *[h.name for h in holdouts]],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=600,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return result.returncode


def run_mutation_gate(
    subject: Path, holdouts: list[Path], mutants: list[Path], log: Log
) -> tuple[list[Path], list[str]]:
    """Deterministic. No model participates in this verdict path (§17.4).

    Sound version. Three things the first one got wrong:

    * **A baseline is run first.** The holdouts must all pass against the correct
      subject. A suite that fails on correct code kills every mutant and means
      nothing, and the mint refuses the whole set rather than scoring it.
    * **Exit codes are read, not merely compared to zero.** pytest returns 1 for
      test failures and 2-5 for collection errors, internal errors, usage errors
      and "no tests collected". Only **1** is a kill; anything else is a broken
      run, and counting it would let a holdout with a syntax error score 1.0.
    * **The mutant is actually installed.** Each mutant is copied over
      ``subject.py`` in an isolated directory that the holdouts import, so a
      failure is caused by the seeded defect rather than by proximity.
    """
    import shutil
    import tempfile

    problems: list[str] = []
    assert_runner_available()

    with tempfile.TemporaryDirectory(dir=str(SEALED_STORE)) as tmp:
        base = Path(tmp) / "baseline"
        base.mkdir()
        shutil.copy(subject, base / "subject.py")
        for h in holdouts:
            shutil.copy(h, base / h.name)
        baseline_code = _run_pytest(base, holdouts)
        log.write(f"baseline (correct subject): pytest exit {baseline_code}")
        if baseline_code != 0:
            problems.append(
                f"holdouts do not pass against the correct implementation (pytest exit "
                f"{baseline_code}); a suite that fails on correct code kills every mutant "
                "and tests nothing"
            )
            return [], problems

        killed: list[Path] = []
        for mutant in mutants:
            work = Path(tmp) / f"m-{mutant.stem}"
            work.mkdir()
            shutil.copy(mutant, work / "subject.py")   # the mutant IS the subject
            for h in holdouts:
                shutil.copy(h, work / h.name)
            code = _run_pytest(work, holdouts)
            if code == 1:
                killed.append(mutant)
                verdict = "KILLED"
            elif code == 0:
                verdict = "SURVIVED"
            else:
                verdict = f"BROKEN_RUN(exit {code})"
                problems.append(f"{mutant.name}: pytest exit {code} is not a verdict")
            log.write(f"mutant {mutant.name}: {verdict}")

    return killed, problems


if __name__ == "__main__":
    raise SystemExit(main())
