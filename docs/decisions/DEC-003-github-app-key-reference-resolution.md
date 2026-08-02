# DEC-003 — GitHub App private-key reference resolved by inspection

**Bound to:** EFAH-CONTRACT-001 v1.1
**Class:** low-consequence, reversible derivation with disclosure (contract §7.1 step 4)
**Date:** 2026-08-02 · **Decided by:** builder

## Context

`secrets.refs.yaml → refs.github_app_or_pat` declares `ref: env:GITHUB_APP_PRIVATE_KEY`
with `blocker_if_missing: MISSING_REQUIRED_CREDENTIAL`.

Measured live: `GITHUB_APP_PRIVATE_KEY` is **unset**. The environment instead
supplies `GITHUB_APP_PRIVATE_KEY_PATH=/home/yoav/.efah/efah-harness-builder.private-key.pem`
(1675 bytes, valid PEM). That key mints an installation token for App 4460605 on
`Pukujan/efah-harness`.

## Decision

The credential is **present**, under a path-valued reference rather than a
value-valued one. `MISSING_REQUIRED_CREDENTIAL` does **not** fire.

Contract §7.1 forbids raising an owner question where a fact is discoverable, and
§29 states existing configuration "must be inspected rather than asked about".
The secret adapter therefore resolves `env:NAME` and `env:NAME_PATH` (file
contents) interchangeably, preferring an explicit value when both exist.

## Consequences

- The single permitted question round is not spent on a discoverable fact.
- No secret value is written to the pack, to git, or to a TerminusDB commit —
  only the reference form changed.
- Reversible: if the owner later exports `GITHUB_APP_PRIVATE_KEY` directly, the
  adapter prefers it with no code change.

## Measured App scope (2026-08-02)

`contents:write · issues:write · pull_requests:write · actions:write ·
checks:write · metadata:read`, `repository_selection: selected`, accessible
repositories: `Pukujan/efah-harness` **only**.

`administration` is **absent**, so branch protection and required status checks
cannot be set by the builder. Recorded separately as **BEA-01**
(`BLOCKED_EXTERNAL_ACCESS`, owner action) rather than as a question, per
`repositories.yaml` lines 30–33.

Sealed repositories are absent from the App's scope, satisfying
`isolation_assertions.builder_token_scope_excludes_sealed_repos`.
