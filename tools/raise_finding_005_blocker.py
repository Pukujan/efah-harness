#!/usr/bin/env python3
"""Raise FINDING-005's remaining question to the owner through the control surface.

Why the surface and not a GitHub issue: ``autonomy-policy.yaml ->
question_policy`` allows **one** initial owner question round and declares
``drip_questions_across_phases: forbidden``. That round was spent on Q1 (issue
#1), answered by DEC-006. A second issue round would be exactly the drip the
policy forbids. AMENDMENT-001 §11.7 gives the owner ``ANSWER_BLOCKER`` on the
control surface, which is the channel a *typed* blocker belongs on.

The blocker carries the five fields §20.2 requires of any well-formed question:
what it blocks, 2-4 concrete options, the consequence of each, the evidence, and
the recommendation stated **after** the neutral options.

Raising it does not idle the project. §20.3 and
``on_timeout: continue_unblocked_work_and_hold_blocked_tasks`` mean only the
tasks that genuinely depend on the answer are held.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from governance.states import OwnerInterrupt  # noqa: E402
from owner_surface.domain import OpenBlocker  # noqa: E402
from owner_surface.gateway import TerminusControlPlaneGateway  # noqa: E402

BLOCKER_ID = "FINDING-005-transport"

QUESTION = """\
ONE QUESTION, THREE PARTS - all of them "the assurance model assignment is not \
what its labels claim". FINDING-003, FINDING-005 and FINDING-006 are batched here \
rather than asked separately, because question_policy forbids drip questions. \
(1) TRANSPORT: the nine gate-bearing assurance roles are served from resold \
subscription pools, measured from your own ckff account log and reconfirmed \
2026-08-02 on the eval gateway path (claude-opus-4-8 -> channel 234, group \
kiro-pro). One channel serves several differently-named models, so the \
cross-family separation the harness enforces by alias may not exist at the \
transport. A degraded assurance model does not error; it emits plausible tests \
that pass. Option B (pin ckff's official channels) has been probed and \
eliminated: 0 of 9 official routes cover a gate-bearing role's model, there is \
no official Google channel at all, and the DB-less eval gateway rejects an \
unconfigured pinned name with HTTP 400. \
(2) CONCENTRATION (FINDING-006): separation holds by agent, but three of the \
nine assurance roles - visible_test_author, sealed_holdout_author, \
contract_compliance_auditor - are all family anthropic, and part (1) measured \
those same three on one upstream channel. One supplier degrading takes out three \
assurance roles at once, silently. A cross-family alternative demonstrably \
exists: claude-opus-5-thinking and gemini-3.1-pro-preview are live on the eval \
gateway and assigned to no role. Section 12.2 makes this your call because it \
says "where family bias is material". \
(3) TIER LABELS (FINDING-003): gemini-3.5-flash, glm-5-turbo and claude-sonnet-5 \
are labelled tier: frontier and are not. Second-order - a better label does not \
help while three labels resolve to one account pool. \
ALREADY FIXED, NO ANSWER NEEDED: the missing separation RULES. All five binding \
rules were edges from implementer, leaving every assurance-to-assurance pair \
unchecked - including sealed_holdout_author vs mutant_author, which DEC-006's \
mutation gate depends on. Sixteen contract-required edges are now enforced in \
code from the contract text; all sixteen hold on your current map, so no routing \
changed. model-policy.yaml was not edited - the alias map is yours. \
WHAT THIS BLOCKS: generation of sealed release holdout content only. The verifier \
identity, the sealed store, the generator, and the mutation gate proceed regardless.\
"""

OPTIONS = [
    (
        "A - official credentials on the eval gateway. "
        "CONSEQUENCE: you supply an official Anthropic/Google key and add the routes to the "
        "DB-less eval LiteLLM config; nine low-volume roles move to verifiable transport; "
        "candidate work stays on ckff where cheapness is the point. Costs a credential and "
        "one gateway redeploy. Holdout generation resumes on a transport whose provenance "
        "is checkable."
    ),
    (
        "C - keep this transport and instrument detection. "
        "CONSEQUENCE: per-request upstream attribution recorded with every gate-bearing run, "
        "hard-fail on empty or truncated generation, and a private mutant corpus with known "
        "kill difficulty the assurance model must keep killing. Measures capability instead "
        "of trusting a label. No credential needed, buildable now - but it audits a "
        "transport it cannot pin, so substitution is detected after the fact, not prevented."
    ),
    (
        "D - accept and record. "
        "CONSEQUENCE: nothing is built; the honest-debt ledger must state that the assurance "
        "path's model provenance is unverified and that the 25/25 mutation kill rate is "
        "weaker evidence than it appears. Cheapest, and the evidence package says so plainly."
    ),
]

RECOMMENDATION = (
    "RECOMMENDATION (confidence: high on the diagnosis, medium on the remedy): A for the nine "
    "gate-bearing roles, with C's private mutant corpus built regardless of your choice. The "
    "corpus is the only check that catches both model substitution and the weak-oracle failure, "
    "and it measures the capability that actually matters rather than a leaderboard label. C "
    "alone leaves the kill rate resting on mutants an unknown model wrote. D is defensible only "
    "if you accept that the sealed-holdout guarantee is currently unproven at the transport. "
    "ON PART (2): recommend moving at least one of the three anthropic assurance roles to "
    "another family - sealed_holdout_author is the one to move, because it is the role whose "
    "output nothing downstream checks. Stated with a caveat you should weigh: the builder is a "
    "Claude model, so its preference for keeping Claude models in assurance roles is exactly the "
    "bias this part is about, and you flagged it. The measurement is mechanical; the preference "
    "is not, so treat the recommendation as weaker than the finding."
)


async def main() -> int:
    replace = "--replace" in sys.argv
    gateway = TerminusControlPlaneGateway()
    existing = {b.blocker_id for b in await gateway.open_blockers()}
    if BLOCKER_ID in existing and not replace:
        print(f"already open: {BLOCKER_ID} (pass --replace to restate it)")
        return 0

    blocker = OpenBlocker(
        blocker_id=BLOCKER_ID,
        interrupt_type=OwnerInterrupt.OWNER_RISK_ACCEPTANCE,
        task_id="WU-HOLDOUT-GEN",
        question=QUESTION,
        options=[*OPTIONS, RECOMMENDATION],
    )
    await gateway.upsert_blocker(blocker)
    print(json.dumps(blocker.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
