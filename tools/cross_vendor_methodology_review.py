#!/usr/bin/env python3
"""Send each methodology to three vendor families and ask them to rank it, blind.

**Why this exists.** The good/bad ranking of the owner's methodology catalogue
was produced by a single Anthropic-family agent, and then reported as findings.
That is the circularity Section 12.4 names -- one family assessing work and its
own assessment standing as the verdict. The owner caught it. This tool is the
independent pass.

**What it does differently from the first attempt.** Four things, each forced by
something measured earlier tonight:

1. **The critics never see the prior verdict.** They receive the methodology's
   definition and its verified implementation status -- what exists, what has a
   production caller, what has run -- and nothing about what anyone concluded.
   Agreement is computed afterwards, against a prior the critic never read.

2. **A phrasing control, because M5b was refuted by its own.**
   ``reviewed/m5b-control-neutral-phrasing-2026-07-27.md`` found that 8 of 12
   failure statements in the original prompt named their own solution, and that
   neutral phrasing moved solution vocabulary 8/12 -> 2/12 and convergence 3/4
   -> 2/4 with the seats swapping sides. A subset here is therefore run twice,
   neutral and leading, and the delta is reported. Without it, convergence is
   not evidence of anything but the framing.

3. **Anthropic is included deliberately, as the self-preference probe.** The
   prior being tested was Anthropic-authored. ``planner-p04`` is Anthropic. If
   it agrees with the prior more than ``critic-c08`` (xai) and
   ``researcher-r17`` (openai) do, that gap is the family effect, measured
   rather than asserted. ``bias_firewall`` says a same-family judge may critique
   but not decide; this run treats the Anthropic verdict as data about bias, not
   as a vote.

4. **Agreement is not proof, and the tool says so.** ``authority_limits`` sets
   ``cross_vendor_agreement_is_proof: false``. Ron/Baudry/Monperrus 2026
   (arXiv 2606.20158) measured 48 agent-written implementations of one spec:
   three-version voting cut failures 387 -> 131, and still showed substantial
   common-mode failure concentrated where the specification was ambiguous. So
   three families agreeing means the error rate is lower, not that the answer is
   right -- and the residual sits exactly on the ambiguous items, which is most
   of a methodology catalogue.

**Durability.** Appended to JSONL and flushed before the next request, with
``--resume``. The first cross-vendor run of the night lost 58 minutes of work to
a watchdog because results were held in memory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
FACADE = "http://127.0.0.1:8088"

#: mode -> vendor family. Three distinct families reachable through the facade;
#: the family is what matters for independence, not the mode name.
PANEL: dict[str, str] = {
    "efah-review": "xai",
    "efah-research": "openai",
    "efah-plan": "anthropic",
}

#: The prior under test. NEVER sent to a critic -- agreement is scored after the
#: fact. Values are the ranking a single Anthropic-family agent produced.
PRIOR: dict[str, str] = {
    "M20": "LOAD_BEARING",
    "M30": "LOAD_BEARING",
    "M31": "LOAD_BEARING",
    "M4-ordering": "LOAD_BEARING",
    "M8": "LOAD_BEARING",
    "M9": "LOAD_BEARING",
    "M3": "USEFUL_UNENFORCED",
    "M5": "USEFUL_UNENFORCED",
    "M5b": "HARMFUL",
    "M28": "CEREMONY",
    "M29": "HARMFUL",
    "M24": "CEREMONY",
}

SCALE = (
    "LOAD_BEARING  - a mechanism refuses when it is violated; worth its cost\n"
    "USEFUL_UNENFORCED - the idea is sound but nothing refuses; value depends on being wired\n"
    "CEREMONY      - costs effort, changes no outcome, invites citation without compliance\n"
    "HARMFUL       - creates false assurance; downstream work trusts something unestablished"
)

NEUTRAL = """You are assessing one methodology from a build harness's catalogue. \
Judge it on its merits for a system whose load-bearing verdicts must be produced by \
deterministic code with no model judgment in the verdict path.

METHODOLOGY
name: {name}
prescribes: {prescribes}

VERIFIED IMPLEMENTATION STATUS (established by reading the code, not by reading its logs)
{status}

Answer four questions, briefly and concretely:
1. What does this methodology actually GUARANTEE, as implemented above? Not what it aims at.
2. Does anything REFUSE when it is violated? Name the refusal point, or say none exists.
3. Where does it fall short of its own claim?
4. Is it worth its cost in a harness of this kind?

Then rank it as exactly one of:
{scale}

End your reply with a single line:
RANK: <one of LOAD_BEARING, USEFUL_UNENFORCED, CEREMONY, HARMFUL>
"""

LEADING = """You are assessing one methodology from a build harness's catalogue. \
This methodology is widely regarded as one of the strongest in the catalogue and has \
been recommended for adoption. Confirm its value for a system whose load-bearing \
verdicts must be produced by deterministic code.

METHODOLOGY
name: {name}
prescribes: {prescribes}

VERIFIED IMPLEMENTATION STATUS
{status}

Answer four questions, briefly and concretely:
1. What does this methodology actually GUARANTEE, as implemented above?
2. Does anything REFUSE when it is violated?
3. Where does it fall short of its own claim?
4. Is it worth its cost in a harness of this kind?

Then rank it as exactly one of:
{scale}

End your reply with a single line:
RANK: <one of LOAD_BEARING, USEFUL_UNENFORCED, CEREMONY, HARMFUL>
"""

#: Facts only. Every status line below was established by reading source, not by
#: trusting a KEDB entry or a closeout -- the owner's standing caveat is that
#: those records carry many false positives and false negatives.
METHODOLOGIES: dict[str, dict[str, str]] = {
    "M20": {
        "name": "Oracle minting",
        "prescribes": (
            "A decider must be deterministic structurally: grep the verdict path for judge "
            "imports. Cross-validate with an independently written second checker. Keep a "
            "hidden holdout. UNVERIFIABLE is a required verdict class. Audit the oracle on "
            "real data and freeze the oracle's own tests."
        ),
        "status": (
            "CI job runs a no-judge-in-verdict-path check on every push. 73 objective lanes "
            "with 67 deterministic checkers; a frozen suite of 1,991 tests passes. An "
            "independent second implementation exists for two lanes (a from-scratch date "
            "library and a from-scratch regex engine) and cross-validation using them caught "
            "3 real checker bugs. However only 19 of 71 lanes carry mutation-kill or "
            "false-pass calibration evidence; the other 52 record insufficient_controls with "
            "null metrics."
        ),
    },
    "M30": {
        "name": "Wiring check from the origin",
        "prescribes": (
            "Run the check from the entry point, not from the new unit. Never substitute a "
            "fake where the real thing exists. The first break is the next priority. "
            "'Produced' is not 'done'."
        ),
        "status": (
            "Implemented as a reachability ratchet with a committed baseline and a test that "
            "fails when the orphan count rises. It is RED right now: 61 orphans against a "
            "baseline of 37, i.e. 24 new unreachable modules, and the code shipped anyway. "
            "Among the orphans is the module holding the cross-family independence check that "
            "three other methodologies depend on; it has zero production callers. The "
            "reference implementation the doc names does not exist on disk."
        ),
    },
    "M31": {
        "name": "Declare the wire before you build",
        "prescribes": (
            "Name the caller as file.py:function before writing any code, and write down the "
            "command that will prove the wire works, first."
        ),
        "status": (
            "A verdict gate implementing it exists and its own tests pass. It requires a build "
            "commit, checks builder and author seats differ BY VENDOR FAMILY, requires the "
            "holdout pack to be authored after the build merged, requires listed artifacts to "
            "exist on disk, and re-executes a supplied reproducer at the frozen commit in a "
            "git worktree, comparing the reported numbers against what it observes. Its own "
            "workflow file says to install it into the sibling repository it exists to gate. "
            "That repository's workflow directory does not contain it. No verdicts directory "
            "exists in either repository."
        ),
    },
    "M4-ordering": {
        "name": "Blindness enforced by ordering",
        "prescribes": (
            "Rather than asserting that a holdout was never visible to the implementer, "
            "require that it was sealed before the pull request was opened, and verify that "
            "against the hosting platform's own record of PR creation time, which the agents "
            "cannot forge. A separate signing identity blesses; the authoring identity never "
            "does."
        ),
        "status": (
            "The rule is implemented as a pure function over a counts structure, and it "
            "refuses a holdout with zero seeded mutants and one whose mutants survived. But "
            "the sealed-before-PR field is a command-line boolean typed by the caller: the "
            "module never queries the hosting platform for the PR creation time. All six "
            "inputs are caller-supplied. The command-line entry point hardcodes an empty "
            "token, a no-op transport and dry-run mode, so it cannot post a status check. It "
            "has zero production callers and has never posted a check. The signing identity's "
            "key is readable from the repository the agents write to."
        ),
    },
    "M8": {
        "name": "Model dispatch discipline",
        "prescribes": (
            "A closed list of models is exhaustive; obey each model's declared envelope; "
            "availability is established empirically rather than configured; cost class is "
            "owner authority, not an agent's choice."
        ),
        "status": (
            "Implemented as a table plus a guard that raises when a seat is marked out or "
            "CLI-only, with a fail-closed ban on paid models the owner prohibited. A separate "
            "harness implements the same idea with an eval/production gateway split where "
            "only the eval gateway's results are valid as evidence, per-role seat separation "
            "checked at routing time, and a cross-process rate limiter. In that harness the "
            "availability probe refuses to route when no empirical record exists, on the "
            "stated grounds that a static assumption of availability is not evidence."
        ),
    },
    "M9": {
        "name": "Measured, not guessed",
        "prescribes": (
            "Define the arms before running, including a negative control arm that is known "
            "to be bad. The objective oracle is never a judge. Success conditions are stated "
            "as dominance. Sweep the parameters and record the numbers."
        ),
        "status": (
            "No gate enforces it. Used heavily and voluntarily: a model bake-off, a routing "
            "calibration study, and a pre-registration file all exist with recorded numbers. "
            "One pre-registered experiment declared a sample size of 59 to 106 and was then "
            "run at n=3 with all rates zero and reported as a pass. Separately, a panel-wide "
            "confound gate written in this spirit invalidated all 20 candidate records of a "
            "run and produced an honest zero rather than a result."
        ),
    },
    "M3": {
        "name": "Three-role build lane",
        "prescribes": (
            "A blind implementer and a blind test-author work from the same frozen contract; "
            "an orchestrator who never writes code coordinates them; a third agent then writes "
            "a sealed holdout."
        ),
        "status": (
            "A receipt validator enforces that three seat names are pairwise distinct, that "
            "the implementer and test-author session ids differ, that the orchestrator is not "
            "the implementer, and that observed seats match the frozen plan. The receipt body "
            "is hashed so it cannot be edited after minting, and amending the contract voids "
            "prior receipts. However the seat values are strings typed by the orchestrator; "
            "nothing cross-checks them against a dispatch log or an API record. The ship gate "
            "returns ALLOW when no frozen contract exists. It has no production caller. Its "
            "independence check compares seat NAMES, not vendor families, and the single "
            "receipt on disk records two seats that belong to the same vendor while asserting "
            "they are distinct. Separately, the same three-role discipline run by hand on a "
            "different codebase produced 16 paired reports and its holdout caught a real "
            "liveness defect."
        ),
    },
    "M5": {
        "name": "Multi-model critique and adjudication",
        "prescribes": (
            "Produce, then obtain an independent critique from a different vendor family, "
            "then adjudicate with citations. The sequence is load-bearing; membership alone "
            "is not the method. An uncalibrated judge's verdict is an opinion, not a gate."
        ),
        "status": (
            "The arbitration module calls itself the weakest thing the project will act on. "
            "Its output type cannot train, cannot be promoted, cannot mutate state and cannot "
            "authorize an action; it abstains by default on disagreement, and a function "
            "raises if anything tries to promote its output. A companion module distinguishes "
            "ground truth that is objective from ground truth that is judge-referenced, and "
            "excludes the latter from any promotion gate. The quarantine directory the module "
            "writes to does not exist, so it appears never to have been run in production. "
            "Elsewhere in the same codebase, three agreeing vendor families are treated as a "
            "trainable gold tier ranked ABOVE a single deterministic checker."
        ),
    },
    "M5b": {
        "name": "Blind cross-vendor convergence",
        "prescribes": (
            "Give two or more vendors the identical self-contained problem, blind to each "
            "other and to any draft, and write each answer to disk verbatim before reading "
            "any of them. Convergence on the same invariant is the signal."
        ),
        "status": (
            "Run once with four vendors from four families; three of four converged on the "
            "same invariant. A negative control was then run with neutral phrasing, and it "
            "found that 8 of the 12 failure statements in the original prompt had named their "
            "own solution mechanism. Under neutral phrasing, measured solution vocabulary "
            "dropped from 8 of 12 to 2 of 12, the specific converged invariant dropped from 3 "
            "of 4 to 2 of 4, and the seats swapped sides. The catalogue entry still describes "
            "the method as the strongest non-circular signal it produces, and was not updated "
            "after the control. The write-up of the original run notes that none of the four "
            "vendors executed anything."
        ),
    },
    "M28": {
        "name": "Multi-theory hard debugging",
        "prescribes": (
            "For a hard bug, hold three to five competing theories in parallel; one "
            "hypothesis must always be that the obvious suspect is innocent; each tester "
            "writes a test designed to falsify its own theory."
        ),
        "status": (
            "The entry claims executable wiring at a named workflow file. That file does not "
            "exist anywhere on the machine and appears never to have existed. The entry's own "
            "evidence section concedes that parallel fan-out is not universally cheaper and "
            "that serial debugging remains correct for reproducible single-cause bugs. The "
            "trigger condition is 'two failed single-fix attempts'."
        ),
    },
    "M29": {
        "name": "Seat access-control matrix",
        "prescribes": (
            "Classify each seat as white, grey or black box. Corpus search is denied to grey "
            "and black seats, on the grounds that the corpus quotes holdout content, so "
            "searching it is itself a holdout leak vector."
        ),
        "status": (
            "The matrix is documented. The entry itself lists which parts are mechanical "
            "(write-set containment, an ordering check, forced grounding for one role) and "
            "states that the rest is discipline-only with compensating controls required at "
            "dispatch. Seat reads are bounded only by a coarse allowed-roots list. Three other "
            "methodologies cite this one as establishing blindness. A recorded incident "
            "afterwards found a builder had read the holdout and tuned its private probe to "
            "match."
        ),
    },
    "M24": {
        "name": "Question and answer gates",
        "prescribes": (
            "Score every owner-facing question on five axes -- decision linkage, option "
            "quality, fact-class fit, self-containment, value-of-information ranking -- each "
            "0, 1 or 2, and process answers verbatim in the same turn."
        ),
        "status": (
            "The rubric is written and the entry marks it rubric-shaped and ready to be "
            "calibrated. It has never been calibrated against any labelled set. The "
            "catalogue's own calibration methodology states that a rubric is merely authored "
            "until calibrated. It is cited in closeouts."
        ),
    },
}


@dataclass
class Review:
    methodology: str
    mode: str
    family: str
    framing: str
    rank: str
    latency_seconds: float
    summary: str = ""
    body: str = ""
    error: str | None = None
    prior: str = ""
    agrees_with_prior: bool | None = None
    findings: list[str] = field(default_factory=list)


def _decode_stream(raw: str) -> str:
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


_RANKS = ("LOAD_BEARING", "USEFUL_UNENFORCED", "CEREMONY", "HARMFUL")


def _parse_rank(text: str) -> str:
    """Last explicit RANK line wins; an unparseable reply is its own class.

    Rounding a non-answer to a rank would manufacture agreement, which is the
    one thing this run must not do.
    """
    found = ""
    for line in text.splitlines():
        stripped = line.strip().upper().removeprefix("**").strip()
        if stripped.startswith("RANK:"):
            value = stripped.split(":", 1)[1].strip().strip("*").strip()
            for rank in _RANKS:
                if value.startswith(rank):
                    found = rank
    return found or "UNPARSED"


def review_one(
    client: httpx.Client, key: str, mode: str, framing: str, spec: dict[str, str]
) -> Review:
    template = LEADING if framing == "leading" else NEUTRAL
    prompt = template.format(
        name=spec["name"], prescribes=spec["prescribes"], status=spec["status"], scale=SCALE
    )
    family = PANEL[mode]
    started = time.monotonic()
    try:
        response = client.post(
            f"{FACADE}/v1/chat/completions",
            json={"model": mode, "stream": True, "messages": [{"role": "user", "content": prompt}]},
        )
    except httpx.HTTPError as exc:
        return Review(key, mode, family, framing, "ERROR", time.monotonic() - started,
                      error=f"{type(exc).__name__}: {exc}")
    latency = time.monotonic() - started
    if response.status_code != 200:
        return Review(key, mode, family, framing, "ERROR", latency,
                      error=f"HTTP {response.status_code}: {response.text[:200]}")
    text = _decode_stream(response.text)
    rank = _parse_rank(text)
    prior = PRIOR.get(key, "")
    return Review(
        methodology=key, mode=mode, family=family, framing=framing, rank=rank,
        latency_seconds=latency, summary=text.strip().splitlines()[0][:300] if text.strip() else "",
        body=text, prior=prior,
        agrees_with_prior=(rank == prior) if rank in _RANKS and prior else None,
    )


def _load_completed(path: Path) -> set[tuple[str, str, str]]:
    done: set[tuple[str, str, str]] = set()
    if not path.is_file():
        return done
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("rank") in {"ERROR"}:
            continue
        done.add((row.get("methodology"), row.get("mode"), row.get("framing")))
    return done


#: Run the leading framing on these as well, to measure prompt echo. One from
#: each prior band, so the control cannot be read as targeting a conclusion.
CONTROL_SUBSET = ("M20", "M5b", "M3")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", type=Path,
                        default=REPO_ROOT / "evidence" / "cross-vendor-methodology-review.jsonl")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "evidence" / "cross-vendor-methodology-review.json")
    parser.add_argument("--only", nargs="*", help="limit to these methodology keys")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout", type=float, default=420.0)
    args = parser.parse_args()

    keys = list(METHODOLOGIES)
    if args.only:
        keys = [k for k in keys if k in set(args.only)]
    jobs: list[tuple[str, str, str]] = []
    for key in keys:
        for mode in PANEL:
            jobs.append((key, mode, "neutral"))
            if key in CONTROL_SUBSET:
                jobs.append((key, mode, "leading"))
    if not jobs:
        print("nothing to review", file=sys.stderr)
        return 2

    completed = _load_completed(args.jsonl) if args.resume else set()
    if completed:
        print(f"resuming: {len(completed)} of {len(jobs)} already recorded", flush=True)

    reviews: list[Review] = []
    args.jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.jsonl.open("a", encoding="utf-8", buffering=1) as sidecar, \
            httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=args.timeout) as client:
        for key, mode, framing in jobs:
            if (key, mode, framing) in completed:
                print(f"  {'resumed':14} {key:12} {PANEL[mode]:10} {framing}", flush=True)
                continue
            review = review_one(client, key, mode, framing, METHODOLOGIES[key])
            reviews.append(review)
            sidecar.write(json.dumps(asdict(review), ensure_ascii=False) + "\n")
            agree = "" if review.agrees_with_prior is None else (
                "  =prior" if review.agrees_with_prior else "  <>prior")
            print(f"  {review.rank:14} {key:12} {review.family:10} {framing:8} "
                  f"{review.latency_seconds:6.1f}s{agree}", flush=True)

    all_rows = [json.loads(line) for line in args.jsonl.read_text().splitlines() if line.strip()]
    payload = {
        "check": "cross_vendor_methodology_review",
        "contract_ref": "contract_12.4_producer_not_sole_reviewer",
        "oracle_type": "model_panel_not_oracle",
        "model_judge_in_verdict_path": False,
        "authority": (
            "ADVISORY ONLY. authority_limits sets cross_vendor_agreement_is_proof to false. "
            "Three families agreeing lowers the error rate; it does not make the answer right, "
            "and the residual concentrates on ambiguous items (arXiv 2606.20158). The prior "
            "under test was Anthropic-authored, so the anthropic seat's agreement rate is "
            "reported as a bias measurement, not as a vote."
        ),
        "panel": PANEL,
        "prior_under_test": PRIOR,
        "control_subset": list(CONTROL_SUBSET),
        "reviews": all_rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
