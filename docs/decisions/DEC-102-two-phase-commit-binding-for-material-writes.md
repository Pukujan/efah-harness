# DEC-102 — A material write costs two commits, and that is the honest price

- **Status:** accepted
- **Date:** 2026-08-02
- **Workstream:** WS-B (TerminusDB authoritative graph and provenance)
- **Contract:** EFAH-CONTRACT-001 v1.1 · Sections 8, 8.1, 15.2, 18
- **Gates:** GATE-D1-01 A3, GATE-D1-02 A1, GATE-D1-02 A4
- **Evidence tier:** DETERMINISTIC_ORACLE

## The conflict

Two contract requirements collide:

- **Section 8** — every compiled object carries `terminus_commit` and a
  `content_hash` covering the object.
- **Section 15.2** — every material write *creates* the commit.

The commit id does not exist until the write happens, so an object cannot carry
the id of the commit that created it at the moment it is written.

## Hypotheses considered

| ID | Approach | Verdict |
|---|---|---|
| H-1 | Store `terminus_commit: null` on write | **Refuted.** GATE-D1-02 A1 requires the field present; a null is the "silent default for a material field" Section 8.1 forbids |
| H-2 | Back-fill the commit id without re-sealing | **Refuted.** `content_hash` covers the envelope, so the stored hash would no longer verify. GATE-D1-02 A4 recomputes and compares |
| H-3 | Record the binding in a side table keyed by document id | **Refuted.** The object itself is what the mechanical verifier reads (Section 18); a side table can drift from it |
| H-4 | Two commits: materialise, then bind and re-seal | **Selected** |

## Decision

`provenance.writer.ProvenanceWriter.write` performs:

1. **materialise** — insert the entities with `terminus_database` and
   `terminus_branch` set and `terminus_commit` still `None`;
2. read the branch head, which is now the commit that materialised them;
3. **bind** — set `terminus_commit`, re-seal the envelope, and `PUT` the objects
   back in a second commit whose message is
   `bind provenance commit <id>: <original message>`.

The stored object therefore names the commit that first materialised it, and its
`content_hash` verifies against its own envelope and body.

## What is given up

- Two commits per material write instead of one. TerminusDB commits are cheap
  content-addressed layers, and the full history remains diffable, so this buys
  provenance completeness at a cost the deadline can absorb.
- The bind commit rewrites envelope metadata on a ledger event document. Ledger
  content is *not* rewritten: `TaskLedger.verify_append_only` recomputes each
  event's hash over its body, so a rewrite of any ledger field would be caught.

## Alternative left open

If commit volume becomes a problem, TerminusDB's data-version header
(`Requested_Data_Version` / `New_Data_Version`) may allow a single-commit
optimistic scheme. It was not measured under this contract and is not assumed to
work.
