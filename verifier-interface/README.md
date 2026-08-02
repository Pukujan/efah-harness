# Protected verifier interface — v1.0.0

Contract: `EFAH-CONTRACT-001 v1.1` §17.2 · `repositories.yaml`
`sealed_repos[].permitted_submission_fields` / `permitted_response_shape`

This directory is the **seam**. It is published on the build side so the sealed
side can implement the verifier independently — in another repository, under
another service identity, on another host — without the build side ever
learning a route to it.

Nothing here contains an endpoint, a credential, or a hostname, and nothing here
ever will. `repositories.yaml` records the sealed repository's URL as
`not_supplied_to_builder`; that is the correct permanent state, not a gap to
close. GATE-D1-08's `on_fail` block says it directly:

```yaml
remediation_must_not_include: granting_builder_access_to_sealed_side
```

A failure of GATE-D1-08 is fixed by proving isolation, never by wiring access.

## The two shapes

The candidate may submit **only** these four fields
(`schema/v1/submission.schema.json`):

| Field | Type | Meaning |
|---|---|---|
| `artifact_or_commit_identifier` | string | The exact candidate commit SHA. One commit for all three lanes (GATE-D2-19). |
| `allowed_runtime_inputs` | object of string→string | Inputs the evaluation is permitted to use. Not fixtures, not expectations. |
| `evaluation_request_id` | string | Correlates request and result. |
| `required_contract_or_oracle_version` | string | The version the candidate believes it is being judged against. |

It receives **only** these five (`schema/v1/result.schema.json`):

| Field | Type | Meaning |
|---|---|---|
| `evaluation_request_id` | string | Echo of the request. |
| `verdict` | `PASS` \| `FAIL` \| `UNVERIFIABLE` | Contract §17.2. `UNVERIFIABLE` is not a soft pass. |
| `oracle_version` | string | Which oracle version decided. |
| `oracle_health` | object of scalars, closed key set | §17.4 health, emitted with every result. |
| `failure_class` | typed enum or null | A **class**, never a reason narrative. |

Anything else in a response is rejected by the build-side client as
`FAILED_PROVENANCE` (GATE-D1-08 A5). That includes a well-shaped response whose
`oracle_health` smuggles content: the key set is closed by allowlist and values
must be scalars, because `oracle_health` is the only open mapping in the shape
and therefore the only place a leak could ride.

## What the sealed side must not return

- hidden assertion text
- private fixture content
- mutant source
- holdout case bodies

These are not merely discouraged. The build-side client inspects for them and
refuses the result. A verifier that leaks them makes the evaluation circular —
the candidate could then be written against the hidden assertions.

## Implementing the sealed half

1. Read `interface-v1.yaml` and the two JSON Schemas. They are the whole
   contract; there is no other channel.
2. Accept a submission, validate it against `submission.schema.json`, and
   **reject any additional field** rather than ignoring it. A verifier that
   tolerates extra fields lets the build side start sending hints.
3. Resolve `artifact_or_commit_identifier` yourself, from the public build
   repository. The build side does not push artifacts to you.
4. Return exactly `result.schema.json`. Emit health on **every** result,
   including failures and `UNVERIFIABLE` — §17.4 requires it unconditionally.
5. Run under a service identity distinct from the builder's (GATE-D1-08 A4),
   and keep the builder token out of your repository's access list (A3).

## Versioning

`VERSION` holds the interface version, currently `1.0.0`. It is semver over the
two shapes:

- **patch** — documentation only, no shape change;
- **minor** — a field becomes optional, or an enum gains a value the build side
  already tolerates;
- **major** — any change to the permitted field sets. That is a contract change
  under §17.2 and needs an amendment, not a release.

The build-side client pins `INTERFACE_VERSION` in
`src/evaluation/verifier_client.py`; a mismatch is a typed failure, not a
warning.

## Status on the build side today

The endpoint is **not configured**, by design. `ProtectedVerifierClient()`
constructed without an injected `VerifierEndpointConfig` returns
`BLOCKED_EXTERNAL_ACCESS` and stops. It does not guess a URL, read one from the
pack, or fall back to a local implementation — a local fallback would make the
verifier circular, which is worse than having none.
