# DEC-501 — Oracle mint records live beside the pack, not inside it

- **Status**: decided
- **Date**: 2026-08-02
- **Workstream**: WS-E (Eval Lab)
- **Contract**: `EFAH-CONTRACT-001 v1.1` §14.3, §17.4, §20.2
- **Supersedes**: nothing
- **Type**: mechanism decision (not `BUILD_VS_INTEGRATE`)

## Context

`owner_todos.json` lists two fields as `TODO_computed_at_mint` for each of the
three oracles:

```json
{"file": "acceptance/oracle-definitions/ORACLE-001-composition-reachability.yaml",
 "field": "content_hash", "value": "TODO_computed_at_mint",
 "blocker_if_unset": "oracle not minted"}
```

Contract §17.4 requires every trusted oracle to carry a version, a content hash,
and a last audit date. So the fields must be filled. The obvious way to fill
them is to write them into the pack YAMLs where the `TODO` markers are.

That way is wrong, and the reason is §14.3.

## The conflict

`project-pack/acceptance/visible/ASSERTION_HASHES.txt` pins the sha256 of every
gate file, and `tools/check_assertion_hashes.py` fails the build on any drift.
The oracle definitions are not in that manifest today — but they are pack
files, the pack is owner authority (§1.2 priority 1), and `integrations/pack.py`
loads them read-only by design with the comment *"the pack is the owner's
artifact and the harness's authority"*.

More to the point: a builder that edits pack files to satisfy a gate the pack
itself declares has no gates. The mechanism does not become safe because this
particular edit is benign. `FAILED_SCOPE` exists for exactly this move.

There is also a second, quieter problem. `last_audit_date` changes every time
the oracle is re-audited. Writing it into the pack means the pack's bytes drift
on a schedule, and any future decision to hash-pin the oracle definitions
becomes impossible without unpinning them again.

## Options considered

1. **Write the values into the pack YAMLs.** Simplest; satisfies the literal
   `TODO` location. Rejected: builder-side edit of owner authority, and it makes
   the definitions unhashable.
2. **Ask the owner to fill them.** Rejected under §20.2 — both values are
   mechanically derivable by inspection, and a question about a discoverable
   fact is drift, not diligence. It would also burn one of at most one owner
   question round on something the harness can compute in milliseconds.
3. **Compute at mint, emit alongside.** Chosen.

## Decision

Mint records are computed by `src/oracles/minting.py` and written to
`src/oracles/minted/ORACLE-00N.mint.json`. The pack YAMLs are never modified.

- `content_hash` is the sha256 of the **pack definition's bytes**, so any edit
  to the definition invalidates the mint rather than silently surviving it.
  `tests/unit/test_oracle_minting.py::test_the_content_hash_binds_the_pack_definition_bytes`
  is the check.
- `last_audit_date` is the UTC date of the mint. `AUDIT_MAX_AGE_DAYS = 90`
  makes a stale audit a test failure rather than a quiet fact.
- The record is a full `governance.envelope.CompiledObject`, so it is sealed and
  its own integrity is verifiable.
- `DeterministicOracle.health()` reads the record. An oracle with no record
  emits `last_audit_date: NOT_MINTED`, and `oracles.registry.require_minted`
  refuses to let it gate.

## Consequences

- The pack stays byte-stable. If the owner later adds the oracle definitions to
  `ASSERTION_HASHES.txt`, nothing here has to change.
- Minting is a **gate**, not a stamp. `mint()` runs the fixture suite, runs the
  declared mutants, and computes the structural no-judge proof; an oracle that
  fails any of the eleven §17.4 properties is recorded as `minted: false` with
  the reasons. That behaviour is itself tested — remove a pinned checker suite
  and the mint fails.
- Re-minting is a deliberate act (`python -m oracles.minting`), not a side
  effect of running the gates, so the recorded audit date means something.

## Owner action still required

None for this decision. The `TODO_computed_at_mint` entries in
`owner_todos.json` can be closed: the values are computed, recorded, and tested.
