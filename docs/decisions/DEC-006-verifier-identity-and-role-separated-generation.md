# DEC-006 — Option B: locally isolated verifier identity, role-separated generation

**Bound to:** EFAH-CONTRACT-001 v1.1
**Class:** `OWNER_RISK_ACCEPTANCE` + `IRRESOLVABLE_EVIDENCE_CONFLICT` resolution
**Answers:** blocker Q1 (issue #1)
**Decided by:** Kujan (owner), 2026-08-02 · **Status:** DECIDED

## Decision

**Option B.** The protected verifier runs under a **separate service identity on
this host**, with its own credential and its own store that the builder process
cannot read. Sealed holdouts, mutants, and oracle internals are generated *by
frontier models through the eval LiteLLM gateway*, under that identity.

## Why this is compliant, not a shortcut

Contract §17.2: "The protected verifier MUST be in a separate repository
**and/or** service identity that implementation workers cannot read, list,
clone, query, or modify."

A separate service identity alone satisfies the clause. The same pattern is
already proven live for the protected model-identity store: `environments.yaml →
terminusdb_protected` is a second container with its own volume, its own
credential, and its own port, and the main admin credential receives **HTTP 401**
against it. That measurement is encoded as a test.

No contract amendment is required.

## What the owner corrected

The initial framing of Q1 treated the missing piece as a missing *author*. It was
not. `repositories.yaml → sealed_repos[0].bootstrap_mode` already reads
`role_separated_generation_authorized`, and `model-policy.yaml` already maps
`sealed_holdout_author` to `holdout-h01` (anthropic/claude-opus-4-8) and
`mutant_author` to `mutant-m03` (google/gemini-3.5-flash) — both in different
families from `implementer` (openai/gpt-5.6-luna), both on the eval gateway.

What was missing was the **identity boundary and the store**. B supplies both.

## The constraint that decides whether this works

**Generation runs inside the verifier identity's process, never the builder's.**

If holdout text is returned into the builder process — even transiently, even
only to be written out again — the builder has read it, and role separation is
broken regardless of which vendor authored the text. A different model writing
into the implementer's memory is theatre, not separation.

Therefore:

- the generator is executed by the verifier service identity as a separate OS
  user, with its own eval-gateway credential;
- the builder invokes it as an opaque subprocess and receives an **exit status
  and a count**, never content;
- the store is mode `0700` under the verifier user, unreadable by the builder;
- the builder's submission path carries only the §17.2 four fields and receives
  only the five-field verdict shape.

## Holdouts are not oracles

Model-authored holdouts are **not** a deterministic oracle. §17.3 ranks a
deterministic execution or state check above a calibrated model judge, and
§17.4 requires a trusted oracle to have a deterministic verdict path with no
hidden model call.

So a generated holdout is a *candidate* until the mutation gate validates it: a
holdout that fails to kill any known-bad mutant tests nothing, and one that
"passes" a mutant is worse than absent because it manufactures confidence. The
mint refuses a holdout set with a kill rate below 1.0 against its declared
mutants.

`judge_calibration.minimum_agreement_to_gate` remains `null`, so every model
judge stays advisory and the deterministic oracles carry the gates.

## Implemented 2026-08-02 — measured, 6/6

`evidence/DEC-006-verifier-identity.json`, produced by `tools/gate_dec_006.py`
from the **builder** identity. The tool refuses to run as root, because as root
the store is readable regardless and check B would pass for the wrong reason.

| | Check | Measured |
|---|---|---|
| A | verifier identity exists and differs | builder uid 1000 (`yoav`) vs verifier uid 995 (`efah-verifier`) |
| B | builder's read of the store is refused | `os.listdir` → `PermissionError: Permission denied` |
| C | generator is root-owned | `owner=root mode=0o755` — the account that runs it cannot rewrite it |
| D | sudo grant is scoped | `yoav ALL=(efah-verifier) NOPASSWD: /opt/efah-verifier/bin/generate-holdouts` |
| E | seam returns a receipt and no content | invoked for real; `exit_status=4 failure_class=ORACLE_INVALID` |
| F | generator's models match the pack | `claude-opus-4-8` / `gemini-3.5-flash`, compared not assumed |

The boundary turned out tighter than designed: the builder cannot even `stat`
the store, because `/var/lib/efah-verifier` denies traversal. That is recorded
as `stat_denied`, **not** as `exists: false` — a denial reported as an absence
would repeat FINDING-004's error of counting a missing signal as success.

**GATE-D1-08 A4 was corrected in the same change.** Its method is
`compare_actor_identifiers` and it had been reporting PASS from
`repositories.yaml` and `model-policy.yaml` — declarations *that* the identities
differ, not the identifiers themselves. It now compares real uids and is
`UNVERIFIABLE` on a host with no verifier identity, rather than PASS. Gate totals
are unchanged (PASS=5, FAIL=0); one assertion is now genuinely verified instead
of declaration-verified.

Check F exists because the generator deliberately cannot import the harness — a
generator importing `src/` would depend on code the builder can rewrite, which
would make the separation theatre. The price of that independence is a
duplicated constant; the mitigation is a check that compares the two.

## Generation has not run, and the refusal is mechanical

FINDING-005 measured that the assurance roles are served from resold
subscription pools, so holdouts minted now would have to be discarded once the
owner answers. The generator therefore refuses until a transport decision is
recorded at `/var/lib/efah-verifier/etc/transport-decision` — **inside the
verifier's own 0700 directory, which the builder cannot write**. The builder
cannot unblock itself; the refusal does not depend on the builder's restraint.

`HoldoutLane` continues to return `UNVERIFIABLE` / `BLOCKED_EXTERNAL_ACCESS`, so
`hidden_holdout` does not report PASS and auto-merge stays blocked. That is the
correct state, not a gap.

## Accepted consequences (honest debt)

- **Same host.** Isolation rests on OS user separation and filesystem
  permissions, not on GitHub's boundary. A root compromise of `gravebuster`
  defeats it. Option A remains the durable path and this is reversible: the
  submission interface does not change, so regenerating under an owner-held
  identity later costs nothing already built.
- **The builder authored the generator.** It cannot read the output, but it
  wrote the code that produces it. Mitigated by the mutation gate — a generator
  that produced weak holdouts would show up as an unkilled mutant — and recorded
  here rather than left implicit.
- **One eval credential.** DEC-002 already records that the DB-less eval gateway
  supports exactly one master key, so per-role spend attribution is lost on that
  path. Unchanged by this decision.
