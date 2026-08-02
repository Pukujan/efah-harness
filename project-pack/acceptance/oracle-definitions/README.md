# Oracle definitions — visible side

Contract: EFAH-CONTRACT-001 v1.0 · Sections 17.3, 17.4

These are the **visible** oracle definitions the builder may read and implement.
Sealed oracle *internals* — hidden fixtures, private mutants, holdout case
bodies — live in `efah-lab-verifier` under a separate service identity and are
never resolvable from this pack.

## The hierarchy is binding, not advisory

Contract Section 17.3 orders oracles from 1 (strongest) to 7 (weakest):

1. exact deterministic execution/state oracle
2. static / AST / type / policy checker
3. property, differential, or metamorphic test
4. reference implementation
5. reproducible empirical benchmark
6. calibrated model judge
7. owner adjudication

> An available higher-level deterministic oracle MUST NOT be replaced by a
> lower-level subjective one.

The practical failure this prevents: a builder that cannot get a deterministic
check to pass reaches for a model judge, the judge says "looks correct," and the
gate goes green on an opinion. `GATE-D2-20` exists to catch exactly that, by
asserting there is no model call anywhere in a deterministic verdict path.

## Minting requirements (Section 17.4)

Every trusted oracle must carry all eleven properties. An oracle missing any one
of them is not a trusted oracle and its verdicts do not gate:

- deterministic verdict path with no hidden model call
- structural proof that no judge participates in the verdict path
- independent second-checker comparison where feasible
- known-good fixtures
- known-bad fixtures
- gaming probes
- mutants that it kills
- honest `UNVERIFIABLE` output where it cannot decide
- pinned checker test suite
- version and content hash
- last audit date, and health emitted with every result

## Files

| File | Level | Purpose |
|---|---|---|
| `ORACLE-001-composition-reachability.yaml` | 1 | Every module reachable from the composition root through a real user-to-result path. |
| `ORACLE-002-lease-fencing.yaml` | 1 | A submission from an expired or superseded lease generation is rejected. |
| `ORACLE-003-provenance-binding.yaml` | 2 | Every artifact, test, and trace binds to an exact contract version and commit. |

Three deterministic oracles are the Day-1/2 minimum. They are chosen because
each one kills a specific failure listed in contract Section 26 — unwired
modules, stale workers, and unbound evidence — rather than because they were
easy to write.

## The `UNVERIFIABLE` obligation

An oracle that cannot decide must say so. It must not default to PASS (which
launders an unknown into a green gate) or to FAIL (which trains the system to
route around it). `UNVERIFIABLE` sends the work unit to the next oracle in the
hierarchy, and if none can decide, to owner adjudication as a typed blocker.
