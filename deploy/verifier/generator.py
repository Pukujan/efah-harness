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

Saying why, without saying what
-------------------------------
Because stderr is discarded by design, a failure here is otherwise
undiagnosable from the build side: the builder sees ``exit 1`` and
``ORACLE_INVALID`` and cannot tell a truncated mutant author from an
unanswered owner decision from a missing pytest. That was measured — a run on
2026-08-03 reported ``holdout_count: 0, ORACLE_INVALID`` when the holdouts had
in fact been authored correctly and written to the store, and the *mutant*
author had come back with ``finish_reason=length`` and zero content deltas.
Nothing in the receipt could have told anyone any of that: not that the
holdouts existed, not which of the two authors failed, not that it failed by
exhausting its token budget. Twice, on consecutive runs.

The fix is :data:`FAILURE_REASONS`: a **closed vocabulary** of fixed tokens,
carried in the receipt's ``failure_reason`` field. It is an enumeration and not
a message. :func:`emit_failure_reason` checks membership against the frozen
tuple before the value is written, so the only strings that can leave this
program through that field are ones spelled out in this source file. A
traceback, a model completion or a holdout body cannot reach it even if a
future edit passes one in by mistake: it is coerced to
``UNCLASSIFIED_EXCEPTION`` and the text goes to the sealed log where it
belongs. The seam validates the same list from the other side.

Minting is not grading
----------------------
This program has two verbs and refuses to do both in one breath.

``--mode MINT`` authors a reference implementation, the holdouts that pin it and
the mutants that attack it, runs the mutation gate, and — only if every mutant
dies — **freezes** the set under ``store/exams/<hex>/`` and returns the exam's
content hash. ``--mode GRADE --exam-id sha256:…`` makes no model call at all: it
loads that frozen exam, re-verifies that it still hashes to its own name, and
runs a candidate against it.

The reason is measured. On 2026-08-03, 25 runs on one commit produced 25
different exercises and passed roughly 45% of the time. The request body carried
``temperature: 0`` and nothing else, the request id never entered a prompt, and
the mutant prompt embeds the previous call's output — so the *subject* was
regenerated too and the variance compounded. The variance being measured was in
the exam, not in the candidate, and a gate wired to that answers a different
question every time it runs.

The cheap fix was measured first and failed. ``seed`` is accepted by this
gateway — ``CONFIGURATIONGUIDE.md`` lists it as universally supported, in a list
about *parameter acceptance* — but ``evidence/generation-determinism-probe.json``
records three identical seeded requests to each author producing three distinct
completions, with no ``system_fingerprint`` to tell an honoured seed from a
dropped one. So no ``seed`` is sent below: a parameter measured not to work,
which the client cannot verify was honoured, is manufactured confidence with a
configuration line to point at. The exam is frozen instead.

An exam the builder can query is an exam the builder can learn
--------------------------------------------------------------
``GRADE`` is an oracle, and an oracle that answers pass/fail can be interrogated
a bit at a time. That is inherent to any holdout gate rather than specific to
this one, and DEC-006 already carries the shape of it as accepted debt. What is
done about it here: the exam has an identity, every grade run names it in the
receipt, and a set that has been queried enough to be inferred is re-minted
under a new identity. Recorded, not pretended away.

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

GENERATOR_VERSION = "1.1.0"
ORACLE_VERSION = "holdout-exam-1.1.0"

VERIFIER_HOME = Path("/var/lib/efah-verifier")
SEALED_STORE = VERIFIER_HOME / "store"

#: Frozen exams, one directory per exam, named for its own content hash. A
#: minted set moves here and is never rewritten; a grade run reads from here and
#: writes nothing. The scratch directories above (``reference/``, ``holdouts/``,
#: ``mutants/``) remain what a mint uses while it is still deciding.
SEALED_EXAMS = SEALED_STORE / "exams"
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

#: The closed vocabulary for the receipt's ``failure_reason``. Every member is
#: a literal in this tuple; nothing is composed, formatted or interpolated into
#: this field, which is what makes it incapable of carrying holdout content.
#:
#: Duplicated in ``src/verifier_identity/seam.py`` as ``GeneratorFailureReason``
#: for the reason at the top of this file — the generator cannot import the
#: builder's tree — and the two are compared by ``tools/gate_dec_006.py`` and by
#: ``tests/unit/test_verifier_identity_seam.py``, so drift is a failing gate
#: rather than a silent divergence.
#:
#: Adding a member is a receipt-shape change: extend both sides together.
FAILURE_REASONS: tuple[str, ...] = (
    # refusals — the generator declined before spending anything
    "NOT_VERIFIER_IDENTITY",
    "TRANSPORT_DECISION_UNRECORDED",
    "CREDENTIAL_ABSENT",
    "TARGET_COUNT_NOT_POSITIVE",
    "THROTTLE_STATE_ABSENT",
    # the exam pin — a grade run that does not name a frozen exam is the
    # 2026-08-03 behaviour, and it is now a refusal rather than a default
    "EXAM_NOT_PINNED",
    "EXAM_NOT_FOUND",
    "EXAM_CONTENT_HASH_MISMATCH",
    # the holdout author
    "HOLDOUT_AUTHOR_EMPTY_GENERATION",
    "HOLDOUT_AUTHOR_TRUNCATED",
    "HOLDOUT_AUTHOR_UNPARSEABLE",
    # the mutant author
    "MUTANT_AUTHOR_EMPTY_GENERATION",
    "MUTANT_AUTHOR_TRUNCATED",
    "MUTANT_AUTHOR_UNPARSEABLE",
    # the transport to either author
    "MODEL_ECHO_MISMATCH",
    "MODEL_RATE_LIMITED",
    "MODEL_HTTP_ERROR",
    "MODEL_TRANSPORT_FAILURE",
    # the deterministic gate
    "TEST_RUNNER_UNAVAILABLE",
    "BASELINE_HOLDOUTS_FAILED",
    "MUTANT_RUN_NOT_A_VERDICT",
    "KILL_RATE_BELOW_THRESHOLD",
    # the graded candidate. Distinct from BASELINE_HOLDOUTS_FAILED on purpose:
    # "the exam is broken" and "the candidate failed the exam" were the same
    # token while one command did both jobs, and telling them apart is the
    # entire point of splitting the command.
    "CANDIDATE_FAILED_HOLDOUTS",
    # the honest bucket, never a free string
    "UNCLASSIFIED_EXCEPTION",
)

#: The second closed vocabulary, and the smaller one. A receipt has to say which
#: verb produced it, because a mint receipt and a grade verdict are not the same
#: claim and were previously indistinguishable — ``exit 0`` meant "a set was
#: minted" and a gate read it as "the candidate passed".
#:
#: Duplicated in ``src/verifier_identity/seam.py`` as ``GenerationRunMode`` and
#: compared by ``tools/gate_dec_006.py`` check F, exactly like
#: :data:`FAILURE_REASONS`.
RUN_MODES: tuple[str, ...] = ("MINT", "GRADE")

#: A pinned exam id is used as a **path component**, so it is matched against
#: this before it is joined to anything. ``--exam-id ../../etc`` is otherwise a
#: traversal handed to the one process that can read the sealed store.
_EXAM_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def emit_failure_reason(reason: str | None) -> str | None:
    """The only gate through which ``failure_reason`` reaches the receipt.

    Membership in :data:`FAILURE_REASONS`, checked here rather than trusted at
    the raise site. A value that is not in the vocabulary is not repaired and
    not passed through — it is replaced by ``UNCLASSIFIED_EXCEPTION``, because a
    field that would forward an unrecognised string is exactly the free channel
    the seam refuses to have.
    """
    if reason is None:
        return None
    return reason if reason in FAILURE_REASONS else "UNCLASSIFIED_EXCEPTION"


def emit_run_mode(mode: str) -> str:
    """The same membership discipline for the smaller vocabulary.

    There is no honest bucket here and there must not be one: an unrecognised
    mode is not a run whose verb is unclear, it is a program that has been
    edited into a state where it does not know what it did. ``MINT`` is the
    conservative coercion because a mint receipt is never gate-eligible on the
    other side, so a confused generator degrades to "this proves nothing about
    a candidate" rather than to a verdict.
    """
    return mode if mode in RUN_MODES else "MINT"


class GeneratorFailure(RuntimeError):
    """A failure that knows its own typed reason.

    The *message* may quote a prompt or a completion and stays in the sealed
    log. Only :attr:`reason` — a token from the closed vocabulary — crosses.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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


def failure(
    request_id: str,
    exit_status: int,
    failure_class: str,
    store_hash: str,
    reason: str,
    run_mode: str = "MINT",
) -> int:
    """A receipt for a run that minted nothing.

    The counts are all zero because **nothing was minted**, which is not the
    same claim as "nothing was produced": the 2026-08-03 run that motivated
    ``failure_reason`` had authored a correct holdout file and then lost the
    mutant author, and reported ``holdout_count: 0`` all the same. The counts
    describe the mint; ``failure_reason`` describes how far it got.
    """
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
            "run_mode": emit_run_mode(run_mode),
            "failure_class": failure_class,
            "failure_reason": emit_failure_reason(reason),
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


# -- the frozen exam -------------------------------------------------------
# An exam is a directory named for the hash of its own contents. That is what
# makes it an artifact with an identity rather than "whatever is in the store
# right now", which is what the previous single-command design actually gated
# on: ``store_content_hash`` ranged over the whole store, so it moved when a
# log rotated and moved when a temporary directory was cleaned up, and two runs
# of the same exam never agreed on what to call it.


#: Written beside the frozen files, deliberately **outside** the hash. The
#: identity is the exam, not the paperwork about it; including the manifest
#: would make the id depend on a timestamp and stop being reproducible.
EXAM_MANIFEST = "manifest.json"


def exam_content_hash(exam_dir: Path) -> str:
    """The exam's identity: a hash over its files, in sorted path order.

    Same construction as :func:`store_content_hash` and scoped to one exam, so
    it can be recomputed on load and compared to the directory's own name. That
    comparison is the only real protection the frozen set has: the files are
    owned by ``efah-verifier``, which is the account that runs this program, so
    read-only modes are a guard rail and not a boundary. A rewritten exam is
    therefore not prevented — it is **detected**, as
    ``EXAM_CONTENT_HASH_MISMATCH``, before any verdict rests on it.
    """
    digest = hashlib.sha256()
    for path in sorted(exam_dir.rglob("*")):
        if not path.is_file() or path.name == EXAM_MANIFEST:
            continue
        digest.update(path.relative_to(exam_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def exam_dir_for(exam_id: str) -> Path:
    """Resolve a pinned id to a directory, refusing anything that is not an id.

    The id arrives as a command-line argument and becomes a path component, so
    the pattern check is a traversal guard and not a formality: the process it
    would be handed to is the one process on this host that can read the sealed
    store.
    """
    if not _EXAM_ID_PATTERN.match(exam_id or ""):
        raise GeneratorFailure(
            "EXAM_NOT_FOUND",
            "the pinned exam id is not a sha256 content hash, so it names no exam",
        )
    return SEALED_EXAMS / exam_id.split(":", 1)[1]


def freeze_exam(
    subject: Path,
    holdouts: list[Path],
    mutants: list[Path],
    args: argparse.Namespace,
    decision: str,
    log: Log,
) -> tuple[str, Path]:
    """Copy a validated set into ``exams/<hex>/`` and return its identity.

    Called only after the mutation gate has accepted the set. Staged first and
    hashed in place, then moved under its own name — an exam that appears in
    ``exams/`` is one that already passed, so a crashed mint cannot leave a
    half-written directory that a later grade run would pin.
    """
    import shutil
    import tempfile

    SEALED_EXAMS.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(SEALED_EXAMS), prefix=".staging-"))
    try:
        (staging / "reference").mkdir()
        shutil.copy(subject, staging / "reference" / "subject.py")
        (staging / "holdouts").mkdir()
        for holdout in holdouts:
            shutil.copy(holdout, staging / "holdouts" / holdout.name)
        (staging / "mutants").mkdir()
        for mutant in mutants:
            shutil.copy(mutant, staging / "mutants" / mutant.name)

        exam_id = exam_content_hash(staging)
        target = exam_dir_for(exam_id)
        # The manifest says what this exam is *of*, so a verdict months later
        # can be bound to a commit and a transport decision without reading a
        # single holdout. It is written after hashing and excluded from it.
        (staging / EXAM_MANIFEST).write_text(
            json.dumps(
                {
                    "exam_id": exam_id,
                    "minted_at": utc_now(),
                    "minted_by_request": args.request_id,
                    "candidate_commit": args.candidate_commit,
                    "contract_version": args.contract_version,
                    "generator_version": GENERATOR_VERSION,
                    "oracle_version": ORACLE_VERSION,
                    "transport_decision": decision,
                    "holdout_author_model": HOLDOUT_AUTHOR_MODEL,
                    "mutant_author_model": MUTANT_AUTHOR_MODEL,
                    "holdout_count": len(holdouts),
                    "mutant_count": len(mutants),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if target.exists():
            # Byte-identical to an exam already frozen. Nothing to do, and
            # certainly nothing to overwrite.
            log.write(f"exam already frozen: {exam_id}")
            shutil.rmtree(staging, ignore_errors=True)
            return exam_id, target
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    for path in sorted(target.rglob("*")):
        path.chmod(0o500 if path.is_dir() else 0o400)
    target.chmod(0o500)
    log.write(f"froze exam {exam_id}: {len(holdouts)} holdout(s), {len(mutants)} mutant(s)")
    return exam_id, target


def load_exam(exam_id: str, log: Log) -> tuple[Path, list[Path], list[Path]]:
    """Load a frozen exam, or refuse. No model participates in this path.

    Both refusals are typed, because "you did not pin an exam", "the exam you
    pinned is not here" and "the exam you pinned is not what it says it is" are
    three different things to go and fix, and all three used to arrive as a
    fresh exercise generated on the spot.
    """
    exam_dir = exam_dir_for(exam_id)
    if not exam_dir.is_dir():
        raise GeneratorFailure(
            "EXAM_NOT_FOUND",
            f"no frozen exam under {SEALED_EXAMS} for the pinned id",
        )
    actual = exam_content_hash(exam_dir)
    if actual != exam_id:
        raise GeneratorFailure(
            "EXAM_CONTENT_HASH_MISMATCH",
            "the frozen exam no longer hashes to its own name; it has been "
            "rewritten since it was minted and no verdict may rest on it",
        )

    subject = exam_dir / "reference" / "subject.py"
    holdouts = sorted((exam_dir / "holdouts").glob("*.py"))
    mutants = sorted((exam_dir / "mutants").glob("*.py"))
    if not subject.is_file() or not holdouts or not mutants:
        raise GeneratorFailure(
            "EXAM_NOT_FOUND",
            "the pinned exam is missing its reference, its holdouts or its mutants",
        )
    log.write(
        f"loaded exam {exam_id}: {len(holdouts)} holdout(s), {len(mutants)} mutant(s)"
    )
    return subject, holdouts, mutants


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
        raise GeneratorFailure(
            "THROTTLE_STATE_ABSENT",
            f"shared throttle state {THROTTLE_STATE} is absent; refusing to generate "
            "unthrottled against an account-wide rate limit",
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


def call_model(
    api_key: str,
    model: str,
    prompt: str,
    log: Log,
    *,
    max_tokens: int = 8000,
    empty_reason: str,
    truncated_reason: str,
) -> str:
    """One completion, zero retries, **streamed**.

    ``empty_reason`` and ``truncated_reason`` are passed in rather than derived
    because this function does not know which role it is serving, and "the
    mutant author came back empty" is a materially different diagnosis from
    "the holdout author came back empty" — the first leaves a usable holdout in
    the store, the second leaves nothing. Both were previously one
    ``ORACLE_INVALID``.

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
    request = urllib.request.Request(  # fixed https base url
        f"{EVAL_BASE_URL}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    chunks: list[str] = []
    finish: str | None = None
    echoed: str | None = None
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
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

    # Order matters for the *reason*, not for the outcome. The observed failure
    # was ``finish_reason=length`` with ``chars=0``: the model spent its whole
    # budget without emitting a content delta. Reporting that as merely "empty"
    # hides the one fact that suggests the fix, so the truncation check is asked
    # first when the completion is also empty.
    if finish == "length":
        raise GeneratorFailure(
            truncated_reason,
            f"{model} generation was truncated (finish_reason=length, chars={len(text)})",
        )
    if not text.strip():
        raise GeneratorFailure(empty_reason, f"{model} returned an empty generation")
    if echoed and model.split("/")[-1] not in str(echoed):
        raise GeneratorFailure(
            "MODEL_ECHO_MISMATCH", f"requested {model} but the gateway echoed {echoed}"
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint or grade sealed release holdouts.")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--target-count", type=int, default=0)
    # No default. A default here is how one command came to mean both verbs,
    # and how a gate came to read "a set was minted" as "the candidate passed".
    parser.add_argument("--mode", required=True, choices=list(RUN_MODES))
    parser.add_argument("--exam-id", default=None)
    #: Optional, GRADE only. A directory holding the candidate's ``subject.py``.
    #: Absent, the exam's own reference is graded — which is the run that proves
    #: the exam still behaves, and is what the reproducibility measurement uses.
    parser.add_argument("--candidate-path", default=None)
    args = parser.parse_args()

    # A usage error, not a runtime condition, so it is left to argparse rather
    # than given a token: an exam id means "grade this one", and a mint that
    # accepted it would be choosing its own answer before authoring the exam.
    if args.mode == "MINT" and args.exam_id:
        parser.error("--exam-id is meaningless in MINT mode; a mint decides its own id")
    if args.mode == "MINT" and args.candidate_path:
        parser.error("--candidate-path is meaningless in MINT mode")

    request_id = args.request_id
    mode = emit_run_mode(args.mode)
    store_hash = store_content_hash()

    # Refuse to run anywhere but inside the verifier identity. If this program
    # is ever executed as the builder, the separation it exists to create is
    # already gone and it must not pretend otherwise.
    import pwd

    who = pwd.getpwuid(os.geteuid()).pw_name
    if who != "efah-verifier":
        return failure(
            request_id, 3, "PROTECTED_ACCESS", store_hash, "NOT_VERIFIER_IDENTITY", mode
        )

    log = Log(request_id)
    log.write(f"start request={request_id} mode={mode} commit={args.candidate_commit} as={who}")

    if mode == "GRADE":
        return _grade(args, request_id, store_hash, log)
    return _mint(args, request_id, store_hash, log)


def _grade(args: argparse.Namespace, request_id: str, store_hash: str, log: Log) -> int:
    """Run a candidate against a frozen exam. Deterministic; no model call.

    The refusal at the top is the change this whole file exists for. Grading
    against an exam minted in the same breath was the previous behaviour and it
    measured the exam rather than the candidate — 25 runs on one commit, 25
    different exercises, PASS about 45% of the time. An unpinned grade is now
    ``EXAM_NOT_PINNED`` and exit 8, which a gate can act on, rather than a
    freshly generated exercise, which a gate cannot.

    Nothing here calls a model, reads a credential or takes the throttle: a
    grade run costs a few seconds of pytest and no money, which is what makes
    running it five times a reasonable thing to ask for.
    """
    if not args.exam_id:
        log.write("refused: grade requested without a pinned exam")
        return failure(
            request_id, 8, "ORACLE_INVALID", store_hash, "EXAM_NOT_PINNED", "GRADE"
        )

    try:
        subject, holdouts, mutants = load_exam(args.exam_id, log)
        candidate = subject
        if args.candidate_path:
            candidate = Path(args.candidate_path) / "subject.py"
            if not candidate.is_file():
                raise GeneratorFailure(
                    "CANDIDATE_FAILED_HOLDOUTS",
                    "the candidate path holds no subject.py to grade",
                )
            log.write("grading a submitted candidate in place of the exam's reference")
        # Whose code sits in the baseline slot decides what a failing baseline
        # means. With a submitted candidate there, the exam is not the suspect.
        baseline_reason = (
            "CANDIDATE_FAILED_HOLDOUTS" if args.candidate_path else "BASELINE_HOLDOUTS_FAILED"
        )
        killed, gate_problems = run_mutation_gate(
            candidate, holdouts, mutants, log, baseline_reason=baseline_reason
        )
    except Exception as exc:  # the class is the channel, not the text
        log.write(f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        failure_class, reason = _classify(exc)
        return failure(request_id, 1, failure_class, store_hash, reason, "GRADE")

    return _emit_verdict(
        request_id, "GRADE", args.exam_id, holdouts, mutants, killed, gate_problems, log
    )


def _mint(args: argparse.Namespace, request_id: str, store_hash: str, log: Log) -> int:
    """Author an exam, validate it to kill_rate 1.0, and freeze it. Or refuse.

    A mint receipt is **never** a verdict about a candidate, and the seam knows
    that from ``run_mode`` rather than by convention. What a successful mint
    produces is an identity — ``store_content_hash`` is the frozen exam's id,
    and it is the only thing a later grade run can be pinned to.
    """
    decision = transport_decision()
    if decision is None:
        # FINDING-005 is unanswered. Holdouts generated through an unverifiable
        # transport are worth less than the effort of generating them, and a set
        # minted now would have to be discarded and regenerated after the answer.
        log.write("refused: FINDING-005 transport decision not recorded")
        return failure(
            request_id, 4, "ORACLE_INVALID", store_hash,
            "TRANSPORT_DECISION_UNRECORDED", "MINT",
        )
    log.write(f"transport decision recorded: {decision}")

    api_key = read_credential()
    if not api_key:
        log.write("refused: no eval-gateway credential installed")
        return failure(
            request_id, 5, "INFRASTRUCTURE_FAILURE", store_hash, "CREDENTIAL_ABSENT", "MINT"
        )

    if args.target_count <= 0:
        log.write("refused: target_count must be positive")
        return failure(
            request_id, 6, "ORACLE_INVALID", store_hash, "TARGET_COUNT_NOT_POSITIVE", "MINT"
        )

    try:
        SEALED_STORE.mkdir(parents=True, exist_ok=True)
        subject, holdouts = generate_subject_and_holdouts(api_key, args, log)
        mutants = generate_mutants(api_key, args, subject, log)
        killed, gate_problems = run_mutation_gate(subject, holdouts, mutants, log)
        exam_id = store_hash
        if bool(holdouts) and bool(mutants) and not gate_problems and len(killed) == len(mutants):
            # Frozen only after the gate accepted it. A set below 1.0 leaves the
            # store exactly as it found it: no exam, no id, nothing to pin.
            exam_id, _ = freeze_exam(subject, holdouts, mutants, args, decision, log)
    except Exception as exc:  # the class is the channel, not the text
        # The message may quote generated content, so it goes to the sealed log
        # and the builder receives a typed class and a typed reason only.
        log.write(f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        failure_class, reason = _classify(exc)
        return failure(request_id, 1, failure_class, store_content_hash(), reason, "MINT")

    return _emit_verdict(
        request_id, "MINT", exam_id, holdouts, mutants, killed, gate_problems, log
    )


def _emit_verdict(
    request_id: str,
    mode: str,
    store_hash: str,
    holdouts: list[Path],
    mutants: list[Path],
    killed: list[Path],
    gate_problems: list[tuple[str, str]],
    log: Log,
) -> int:
    """The one shape both verbs report through, so they cannot drift apart."""
    kill_rate = (len(killed) / len(mutants)) if mutants else 0.0
    # A gate problem voids the score outright. DEC-006's mint rule is that a
    # holdout set below 1.0 is refused; a set whose *gate* could not decide is
    # not at 1.0 either, and reporting it as such would be the manufactured
    # confidence the rule exists to prevent.
    accepted = (
        bool(holdouts) and bool(mutants) and kill_rate >= 1.0 and not gate_problems
    )

    if gate_problems:
        for problem_reason, detail in gate_problems:
            log.write(f"gate problem [{problem_reason}]: {detail}")

    # Why it refused, in the order that matters to whoever reads it. A gate that
    # could not decide outranks a kill rate below 1.0, because the kill rate is
    # meaningless when the gate is broken — and the two are the difference
    # between "the holdouts are weak" and "the holdouts measured nothing", which
    # the receipt previously reported identically as ``exit 7, HOLDOUT_FAILURE``.
    reason: str | None = None
    if not accepted:
        reason = gate_problems[0][0] if gate_problems else "KILL_RATE_BELOW_THRESHOLD"
        log.write(
            f"{mode.lower()} refused [{reason}]: kill_rate={kill_rate:.4f} "
            f"over {len(mutants)} mutants"
        )

    emit(
        {
            "generation_request_id": request_id,
            "exit_status": 0 if accepted else 7,
            "holdout_count": len(holdouts),
            "mutant_count": len(mutants),
            "killed_count": len(killed),
            "kill_rate": round(kill_rate, 6),
            # In both modes this is the frozen exam's identity, not a hash over
            # the whole store. The old whole-store hash moved when a log rotated
            # and when a temp directory was cleaned up, so two runs of the same
            # exam never agreed on what to call it — which is no identity at all.
            "store_content_hash": store_hash,
            "generator_version": GENERATOR_VERSION,
            "oracle_version": ORACLE_VERSION,
            "generated_at": utc_now(),
            "run_mode": emit_run_mode(mode),
            "failure_class": None if accepted else "HOLDOUT_FAILURE",
            "failure_reason": emit_failure_reason(reason),
        }
    )
    return 0 if accepted else 7


def _classify(exc: Exception) -> tuple[str, str]:
    """``(failure_class, failure_reason)`` — the contract's class, then the detail.

    ``failure_class`` is the §10.6 vocabulary and is deliberately coarse: it
    answers "is this worth retrying". ``failure_reason`` answers "what broke",
    and exists because three quite different things — an unanswered transport
    decision, a truncated mutant author, an absent pytest — all land on
    ``ORACLE_INVALID`` and were indistinguishable from the build side.
    """
    if isinstance(exc, GeneratorFailure):
        reason = emit_failure_reason(exc.reason)
        assert reason is not None
    elif isinstance(exc, urllib.error.HTTPError):
        reason = "MODEL_RATE_LIMITED" if exc.code == 429 else "MODEL_HTTP_ERROR"
    elif isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        reason = "MODEL_TRANSPORT_FAILURE"
    else:
        reason = "UNCLASSIFIED_EXCEPTION"

    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return "RATE_LIMIT", reason
        if exc.code in (401, 403):
            return "PROTECTED_ACCESS", reason
        return "MODEL_UNAVAILABLE", reason
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "TRANSIENT_PROVIDER_FAILURE", reason
    return "ORACLE_INVALID", reason


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
    text = call_model(
        api_key,
        HOLDOUT_AUTHOR_MODEL,
        prompt,
        log,
        max_tokens=8000,
        empty_reason="HOLDOUT_AUTHOR_EMPTY_GENERATION",
        truncated_reason="HOLDOUT_AUTHOR_TRUNCATED",
    )
    files = _split_files(text)
    log.write(f"holdout author produced files: {sorted(files)}")
    if "subject.py" not in files or "test_holdout.py" not in files:
        raise GeneratorFailure(
            "HOLDOUT_AUTHOR_UNPARSEABLE",
            f"holdout author returned {sorted(files)}, expected subject.py and test_holdout.py",
        )

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
    text = call_model(
        api_key,
        MUTANT_AUTHOR_MODEL,
        prompt,
        log,
        max_tokens=8000,
        empty_reason="MUTANT_AUTHOR_EMPTY_GENERATION",
        truncated_reason="MUTANT_AUTHOR_TRUNCATED",
    )
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
        raise GeneratorFailure(
            "MUTANT_AUTHOR_UNPARSEABLE",
            f"mutant author returned no usable modules; parsed {sorted(files)} "
            f"from {len(text)} chars",
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
        raise GeneratorFailure(
            "TEST_RUNNER_UNAVAILABLE",
            f"test runner {TEST_RUNNER} is absent; no verdict is possible",
        )
    probe = subprocess.run(  # fixed argv
        [str(TEST_RUNNER), "-m", "pytest", "--version"],
        capture_output=True, text=True, timeout=60,
    )
    if probe.returncode != 0:
        raise GeneratorFailure(
            "TEST_RUNNER_UNAVAILABLE",
            f"{TEST_RUNNER} cannot run pytest (exit {probe.returncode}); a missing runner "
            "exits 1 exactly like a failing test, so every mutant would score as killed",
        )


def _run_pytest(directory: Path, holdouts: list[Path]) -> int:
    import subprocess

    result = subprocess.run(  # fixed argv
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
    subject: Path,
    holdouts: list[Path],
    mutants: list[Path],
    log: Log,
    *,
    baseline_reason: str = "BASELINE_HOLDOUTS_FAILED",
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Deterministic. No model participates in this verdict path (§17.4).

    ``baseline_reason`` names what a failing baseline *means*, which depends on
    whose code is in the baseline slot. Minting puts the reference there, so a
    failure is ``BASELINE_HOLDOUTS_FAILED`` — the exam is broken. Grading a
    submitted candidate puts the candidate there, so a failure is
    ``CANDIDATE_FAILED_HOLDOUTS`` — the exam worked and the candidate did not.
    One token for both was tolerable while one command did both jobs; it is the
    difference between "fix the generator" and "fix the code" and it should
    never have been a single word.

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

    #: ``(failure_reason, detail)``. The reason crosses the seam, the detail
    #: goes to the sealed log. A broken baseline and a surviving mutant are both
    #: "not 1.0" and are not the same news: one means the suite measured nothing,
    #: the other means it measured something and found a hole.
    problems: list[tuple[str, str]] = []
    assert_runner_available()

    with tempfile.TemporaryDirectory(dir=str(SEALED_STORE)) as tmp:
        base = Path(tmp) / "baseline"
        base.mkdir()
        shutil.copy(subject, base / "subject.py")
        for h in holdouts:
            shutil.copy(h, base / h.name)
        baseline_code = _run_pytest(base, holdouts)
        log.write(f"baseline ({subject.parent.name}/subject.py): pytest exit {baseline_code}")
        if baseline_code != 0:
            problems.append((
                baseline_reason,
                f"the holdouts do not pass against the subject in the baseline slot "
                f"(pytest exit {baseline_code})",
            ))
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
                problems.append((
                    "MUTANT_RUN_NOT_A_VERDICT",
                    f"{mutant.name}: pytest exit {code} is not a verdict",
                ))
            log.write(f"mutant {mutant.name}: {verdict}")

    return killed, problems


if __name__ == "__main__":
    raise SystemExit(main())
