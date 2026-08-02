# DEC-602 — Plane projection endpoint shapes, measured rather than assumed

- **Status:** recorded by WS-F, 2026-08-02
- **Contract:** EFAH-CONTRACT-001 v1.1 · §1.2, §4, §4.1, §9.8, §16.1, §16.3
- **Evidence:** live probe of `https://api.plane.so` against workspace `efah`,
  project `0f843a48-6969-498c-9d60-64f41147bbb2`, 2026-08-02; Context7 snapshot
  `C7-plane-developers-2026-08-02-29aba5867455`
- **Gate:** GATE-D2-11 (Plane projection completeness), GATE-D2-15 (Context7
  snapshot hash and dependency link)

## Decision

`src/integrations/plane.py` is written against **measured** endpoint behaviour.
Where the pinned documentation and the live API disagree, §1.2's authority order
puts *measured live state* above *pinned docs*, so the measurement wins and the
divergence is recorded here rather than silently coded around.

## What the live probe found that the docs did not

| Finding | Consequence in the adapter |
|---|---|
| `app.plane.so` (the host in `environments.yaml`) serves **GET** but returns **405** to POST/PATCH/DELETE. `api.plane.so` accepts both. | `PlaneConfig.api_host` maps the configured base to the API host. The pack is *not* edited: it records the owner's fact, and the write host is a transport detail the adapter owns (§5.1). |
| There is **no** `sub-work-items` endpoint (404 on both spellings). | `WorkUnit -> sub_work_item` is implemented as a work item carrying `parent`. Parents are upserted first so the child can bind in the same pass. |
| `POST /work-items/` with a duplicate `external_id` returns **409 with the existing `id` in the body**. | Genuine upsert with no read-before-write: POST, and on 409 PATCH the id the server just named. No create/create race. |
| `POST /cycles/` requires `project_id` **in the body**, and rejects the spelling `project` with `{"non_field_errors": ["Project ID is required"]}`. | The client injects `project_id` for the `cycles` collection only. |
| Cycles enforce a separate **name** uniqueness rule and answer **400 with no id**, unlike work items' 409. | `upsert` falls back to `find_by_external_id` + PATCH on 400. Without this, every 30-second poll would have created duplicate cycles. |
| `?external_source=&external_id=` resolves to a **single object** for work items and modules, and is **ignored by cycles**, which return the full list. | `find_by_external_id` tries the filtered GET, then matches client-side. |
| Worklogs return `404 {"message": "Worklog is not enabled for the project"}`, as does `/total-worklogs/`. | Derived durations are rendered onto the work item instead, and `ProjectionResult.worklog_api_available` reports `false`. The durations are never dropped and never replaced by an estimate (§9.8). |
| An invalid API key returns **403**, not 401. | Both are treated as a credential rejection and degrade the projection. |

## Why probing rather than trusting the snapshot

`dependency-policy.yaml` requires a Context7 snapshot per load-bearing
dependency, and one is cached. But two of the findings above — the 405 write
host and the cycle-vs-work-item conflict asymmetry — are properties of *this
deployment and this project*, not of the documented API. Coding to the docs alone
would have produced an adapter that reads correctly and writes nothing, and the
failure would have surfaced as an empty Plane board rather than an error.

The snapshot and the probe are also two genuinely different origins, which
matters: `dependency-policy.yaml` is explicit that the two Context7 credentials
are capacity/failover and **not** independent evidence sources, so a second
Context7 retrieval would not have corroborated the first. The live probe does.

## Projection safety properties, and how they are held

- **One way.** `PlaneProjection.may_mutate_authoritative_state = False`;
  `write_back()` exists only to raise `AuthoritativeMutationAttempted`; the
  constructor refuses any source that is not a `dashboard.source.ReadOnlySource`.
- **Outage is not failure.** `plane.yaml` sets `outage_blocks_project: false`
  and `outage_state: degraded_projection`. Every network path converges on a
  `ProjectionResult`, never an exception. A pass where nothing landed reports
  `degraded_projection`; a pass with some per-item failures is still a
  projection and lists them.
- **No protected content.** The payload is assembled and scanned *before* the
  first network call, so a leak is caught locally rather than after half of it
  is published.
- **Separable from sample data.** `plane.yaml` records that the project ships
  with Plane demo content. Everything this adapter creates carries
  `external_source: efah-projection`.
