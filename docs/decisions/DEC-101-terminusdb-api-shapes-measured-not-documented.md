# DEC-101 — TerminusDB API shapes come from the running server, not the docs

- **Status:** accepted
- **Date:** 2026-08-02
- **Workstream:** WS-B (TerminusDB authoritative graph and provenance)
- **Contract:** EFAH-CONTRACT-001 v1.1 · Sections 1.2, 7.1, 7.2, 16.1
- **Evidence tier:** DETERMINISTIC_ORACLE (live probe, reproducible)
- **Context7 snapshot:** `project-pack/evidence/context7-snapshots/C7-terminusdb-main-bcc5b287.json`

## Question

What are the correct endpoints and payload shapes for the operations the control
plane needs: ensure database, create branch, list branches, insert documents,
query documents, read commit id and log?

## Resolver

Contract Section 7.1 orders resolvers: recorded decision, then **objectively
measurable**, then external research. This is measurable — the server is running
and is the authority on its own API — so it was probed before any documentation
was consulted. Section 7.2 classes this as a *live empirical fact*, whose primary
resolver is a fresh probe.

Measured server, `GET http://localhost:6363/api/info`, 2026-08-02:

```
terminusdb 12.0.6 · git_hash 54661f4d9a9a049c56f07167426fc1f2e7fe4fe1
terminusdb_store 0.19.8 · storage 2
```

## Findings that a documentation-only implementation would have got wrong

| # | Documented / assumed | Measured on 12.0.6 | Consequence if unmeasured |
|---|---|---|---|
| 1 | `GET /api/branch/:path` lists branches (Context7 snapshot, `_autodocs/INDEX.md`) | **HTTP 405** `Method not allowed: GET`. Branches are read as `Branch` documents from `GET /api/document/{org}/{db}/local/_commits?type=Branch` | GATE-D1-01 A1's before/after branch listing could not be produced at all |
| 2 | `@key` is inherited from a parent class | **Not inherited.** A subclass of an abstract parent with no `@key` of its own gets a *random* id | Every entity id would be random, so every `Dependency` link built from `entity_id` would dangle. Silent, not loud |
| 3 | A batch insert can reference a document created in the same batch | **True**, because one request is one transaction — but a reference to a document in a *different* request fails with `references_untyped_object` | Import order matters; the pack import submits its entity graph as one batch |
| 4 | `list[...]` maps naturally to a `Set` | A `Set` is returned in the server's order, not the write order | The entity body changes between write and read, so `content_hash` stops verifying and GATE-D1-02 A4 fails. `List` is used instead; measured to preserve order, accept `[]`, and work for links |
| 5 | Branch names are free-form | A `/` is parsed as a path separator: `api:BadTargetAbsoluteDescriptor` | The conventional `import/pack-...` branch name is impossible; `import-pack-<hash8>-<stamp>` is used |
| 6 | `count=true` is a valid document query parameter | `count` must be a non-negative integer | A malformed count silently 400s a read |

Negative controls were run for 1, 2, 4, 5 and 6 rather than assumed.

## Decision

1. The adapter (`src/integrations/terminusdb.py`) implements **only** measured
   routes. Its module docstring carries the measured route table.
2. Where the Context7 snapshot and the running server disagree, the server wins
   (Section 1.2: measured live state outranks pinned docs). The conflict is
   recorded inside the snapshot under `documentation_vs_measured_conflict` rather
   than quietly dropped — Section 16.2's version-diff loop needs the previous
   claim to diff against.
3. The Context7 snapshot is **branch-pinned, not version-pinned**: Context7
   serves `/terminusdb/terminusdb` from the repository's `main` branch and offers
   no version-tagged variant. `dependency-policy.yaml` requires a pin, so the
   authoritative pin for this dependency is the measured server build recorded in
   `measured_server`, and the snapshot says so in `pinning_note`.

## Consequence for other lanes

Any lane that talks to TerminusDB should go through
`integrations.terminusdb.TerminusClient` rather than re-deriving routes. In
particular `GET /api/branch/...` looks correct in the vendor docs and is not.
