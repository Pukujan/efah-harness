# FINDING-006 — role separation was a star, not a mesh

**Raised:** 2026-08-02 · **Class:** mechanization gap (§13.4), plus one
`OWNER_RISK_ACCEPTANCE` sub-item folded into the FINDING-005 blocker
**Status:** the mechanization gap is **CLOSED in code**. The family-concentration
sub-item is **OPEN**, awaiting the owner.
**Raised by:** the owner, mid-session, asking whether a rule existed at all.
**Evidence:** `models.separation.coverage_report()`; measured, not argued.

## What was measured

`model-policy.yaml → role_incompatibilities` declares seven rules. Five are
binding; two (`researcher`/`research_challenger`, `planner`/`plan_challenger`)
are `should_differ_by_family`, which `ModelRouter` reports with an `advisory:`
prefix and never raises on.

**All five binding rules are edges from `implementer`.**

```
binding_rules_all_centred_on_implementer: true
roles_named_in_no_rule: [contract_compliance_auditor, evidence_auditor,
                         integration_verifier, oracle_author, release_verifier]
```

So no binding rule constrained any two assurance roles against each other, and
five roles were unconstrained entirely. The router could not report a violation
of a rule that did not exist, and the map passed with zero findings — not
because the separations held, but because they were never asked about.

Against the separations the contract states directly: **16 required edges, 5
mechanized.**

| Unmechanized required edge | Clause |
|---|---|
| `sealed_holdout_author` ≠ `judge` | §12.2 — the third edge of "builder, holdout author, and final adjudicator MUST be distinct"; the pack wrote the two touching the implementer and omitted the pair |
| `implementer` ≠ `oracle_author` | §12.2 — oracle internals are named beside holdouts and mutants; authoring is the strongest form of access |
| `implementer` ≠ `integration_verifier` | §12.2 — producer not sole reviewer; §14.4's skeleton evidence rests on this edge |
| `implementer` ≠ `evidence_auditor` | §12.2 |
| `implementer` ≠ `contract_compliance_auditor` | §12.2 |
| `implementer` ≠ `release_verifier` | §12.2 |
| `adversarial_critic` ≠ `judge` | §12.4 — an adjudicator that is also the critic decides its own objection |
| `sealed_holdout_author` ≠ `mutant_author` | DEC-006 — mutants are the only check on holdout strength |

**Nothing is violated today.** All sixteen hold on the current alias map. The
finding is that eight of them held by luck rather than by rule.

The last row is the sharpest. DEC-006 makes the mutation gate the thing that
decides whether a generated holdout set is worth anything — "the mint refuses a
holdout set with a kill rate below 1.0 against its declared mutants." If one
model authored both sides, the kill rate measures the author's self-consistency
and reports it as assurance. Nothing in the harness would have noticed.

## What was done

The gap is §13.4 — a contract clause with no mechanization — so it is the
builder's to close, and it is closed in **code**, not by editing owner data.
`model-policy.yaml` is untouched: the alias map *and* the rule list are owner
data, and FINDING-003 already established that the builder does not adjust the
map.

`src/models/separation.py` transcribes every separation the contract states,
each carrying the clause that states it, and
`ModelRouter.role_separation_findings` now evaluates those alongside the pack's
declared rules. §1.2 puts the contract above the pack, so a separation the
contract requires is enforced whether or not the owner remembered to write a
rule for it. `CONDITIONAL` clauses ("where feasible", "where family bias is
material") report as advisory — deciding materiality is the owner's call, not
the router's.

Because all sixteen edges already hold, this changes no routing today. It means
a regression cannot pass unnoticed. Negative control, measured:

```
collapse mutant_author onto sealed_holdout_author's alias
  → [sealed_holdout_author, mutant_author] must differ by agent but share
    'holdout-h01' (DEC-006_mutation_gate_validates_the_holdout_set;
     the pack declares no rule for this pair)
```

## What is still open — family concentration

Separation holds **by agent**. By family it concentrates:

```
anthropic: contract_compliance_auditor, sealed_holdout_author, visible_test_author
deepseek:  evidence_auditor, judge
qwen:      integration_verifier, oracle_author
```

Three of the nine gate-bearing assurance roles are one family — and FINDING-005
measured those same three on **one upstream channel (234, group `kiro-pro`)**.
So the concentration is not only a label fact; at the transport those three
roles are one supplier, and a single upstream degradation takes out three
assurance roles at once while none of them errors.

§12.2 states this conditionally — "same-family validation MUST be rejected
**where family bias is material** and a cross-family alternative is available" —
and materiality is an owner judgment. A cross-family alternative demonstrably
exists: the eval gateway carries 40 models across eight or more families,
including `claude-opus-5-thinking` and `gemini-3.1-pro-preview`, which
FINDING-003 noted are live and assigned to no role.

This is folded into the **existing** `FINDING-005-transport` blocker on the
control surface rather than raised as a second one.
`autonomy-policy.yaml → question_policy` allows one round and forbids drip
questions; FINDING-003, FINDING-005 and this are one subject — *the assurance
model assignment is not what its labels claim* — so they are one question.

## A note on who found this

The builder is a Claude model. It did not find this; the owner did, while
pointing out that a Claude model's preferences bias toward Claude models. The
concentration the report now names is three **Anthropic** assurance roles — the
exact shape that bias would produce, sitting unexamined in a rule set that only
ever compared things to the implementer. The alias map is owner data and was not
authored by the builder, so this is not a self-inflicted bias; but the builder
had run `role_separation_findings()` and read `none`, and did not ask whether the
rules covered the right pairs. Recorded because a harness whose separation rules
are written by one of the separated parties needs the check to come from
outside, and here it did.
