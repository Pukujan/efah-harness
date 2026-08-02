# Context7 snapshots

Contract: EFAH-CONTRACT-001 v1.0 · Section 16

Version-pinned documentation snapshots land here, one file per retrieval, named
`C7-<library>-<version>-<short_hash>.json`.

## The rule that is easy to get wrong

You have two Context7 credentials. Contract Section 16 is explicit:

> The two Context7 credentials are operational capacity/failover credentials,
> **not independent evidence sources**.

So two retrievals — one per credential — that agree are **one source agreeing
with itself**, not corroboration. Source diversity for the evidence rules in
Section 7.3 has to come from genuinely different origins: the library's own
repository, a standards document, a reproducible benchmark you ran. `GATE-D2-15`
asserts this directly (`credential_alias_does_not_increase_source_diversity`).

## Required fields per snapshot

Every snapshot records all eleven, or the provenance gate fails:

```yaml
snapshot_id: C7-...
credential_alias: primary | secondary
library_id: "..."
library_version_or_branch: "..."     # never unpinned
query: "..."
retrieved_at: "..."
raw_response_hash: "sha256:..."
normalized_response_hash: "sha256:..."
source_locations: []
affected_dependencies: []
affected_decisions: []
```

`raw` and `normalized` hashes are both required: the raw hash proves what came
back over the wire, the normalized hash lets you diff two versions without
whitespace and ordering noise producing a false "changed" signal.

## Why snapshots are cached rather than re-fetched

Contract Section 16.2's version-diff loop needs a *previous* state to diff
against. A cache that is only ever overwritten cannot answer "what changed in
this library's API between the version we integrated against and the version we
are now pinning?" — which is the question that produces the impact map and the
revalidation task. Keep old snapshots.

## Suggested libraries to snapshot before Day 1

Every entry in `dependency-policy.yaml` under `selected_stack`, at the exact
version you pin there. Doing this before launch means the builder starts with a
warm cache and does not spend Day-1 time on documentation retrieval it could
have had for free.
