# DEC-502 — GATE-D1-08 A1 is not executed on the build side

- **Status**: decided
- **Date**: 2026-08-02
- **Workstream**: WS-E (Eval Lab)
- **Contract**: `EFAH-CONTRACT-001 v1.1` §12.2, §17.2 · `repositories.yaml`
  `isolation_assertions` · GATE-D1-08
- **Type**: assurance-scope decision

## Context

GATE-D1-08 A1 reads:

```yaml
- id: A1
  claim: Builder identity receives 401, 403, or 404 for the sealed repository.
  method: authenticated_request_under_builder_identity
  expected: http_status in [401, 403, 404]
  failure_state: PROTECTED_ASSET_ACCESS
  note: A 200 here is a hard failure, not a convenience.
```

A2 of the same gate reads:

```yaml
- id: A2
  claim: No build-side file contains the sealed repository URL, token, or DB credential.
  method: static_scan
  scope: ["project-pack/", "src/", "tests/", ".github/", "docker/", "*.env.example"]
  forbidden_patterns: [...]
  expected: zero_matches_outside_declared_sealed_repos_block
```

`src/` and `tests/` are in A2's scope. A build-side implementation of A1 must
name the sealed repository in order to request it. That name would then be a
match in `src/` or `tests/`, and A2 would fail.

The two assertions are not in conflict. They are addressed to different actors.

## Decision

`evaluation/checks.py` registers **no** check for `("GATE-D1-08", "A1")`. The
gate runner reports it as `NOT_IMPLEMENTED` with this reason recorded in
`NOT_EXECUTABLE_REASONS`, which appears in the gate result and its evidence:

> an authenticated probe of the sealed repository would have to name it on the
> build side, which A2 of this same gate forbids; the probe belongs to the
> verifier service identity. Implementing it here would break A2 to satisfy A1.

Consequently GATE-D1-08 is `PARTIALLY_EXECUTABLE` and its verdict is
`UNVERIFIABLE`, not `PASS`. A gate the build side cannot fully decide does not
report a green.

A3 (`token_scope_introspection`) is likewise unregistered: it needs a builder
credential and a GitHub API call that this runner does not have.

## What *is* executed, and it is not nothing

- **A2** — real static scan of all five scope entries. The forbidden patterns
  are read from the gate YAML at run time rather than hardcoded, precisely so
  the scanner itself does not create the match it searches for. It additionally
  asserts, more strictly than the pattern heuristic, that no sealed repository
  declares a resolvable URL anywhere including in the owner's own pack.
- **A4** — identity comparison from `repositories.yaml` and `model-policy.yaml`:
  separate service identity, `builder_access: forbidden`, holdout author running
  under `verifier_service_identity`.
- **A5** — the verdict-payload schema assertion, with live probes for all four
  `forbidden_content` classes, including content smuggled through
  `oracle_health` (the only open-shaped field in the approved response).
- **A6** — implementer vs `sealed_holdout_author` and `judge`, compared by both
  alias and vendor family.

## What this decision explicitly does not authorize

The gate's own `on_fail` block:

```yaml
remediation_must_not_include: granting_builder_access_to_sealed_side
```

Nothing here is remediated by obtaining access, a token, a URL, or a mirror.
The correct closure of A1 is for the **verifier service identity** to run the
probe from its side and publish the transcript as
`request_transcript_with_actor_identity`. That side can name its own repository
without violating anything.

## Owner action required

A1 and A3 close when the sealed side exists and runs its half of the probe.
Both are blocked on open owner question Q1 (github issue #1), not on build-side
work.
