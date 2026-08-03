# DEC-601 — The API port seam and its default in-process adapters

- **Status:** recorded by WS-F, 2026-08-02
- **Contract:** EFAH-CONTRACT-001 v1.1 · §5.1, §5.2, §11.3, §11.5, §14.2, §15.2
- **Type:** architecture decision (not a `BUILD_VS_INTEGRATE` waiver — see below)
- **Affects:** WS-B (TerminusDB), WS-C (LangGraph), the orchestrator's owner
  control surface (§11.7 / GATE-D1-10)

## Decision

The API declares its dependencies as `typing.Protocol` ports in
`src/api/ports.py`, and ships default in-process adapters for two of them:

| Port | Default adapter | Replaced by |
|---|---|---|
| `ControlPlaneReadPort` / `ControlPlaneWritePort` | `api.adapters.control_plane_memory.InMemoryControlPlane` | WS-B TerminusDB adapter |
| `RuntimePort` | `api.adapters.control_plane_memory.RecordingRuntime` | WS-C LangGraph runtime |
| `DriftEnginePort` | `api.controllers.projects.ContractDriftEngine` | WS-A/WS-E drift engine |
| `ProjectionPort` | `integrations.plane.PlaneProjection` | — (this workstream) |

The composition root is `api.deps.Container.build(...)`; swapping an
implementation is one keyword argument there and changes nothing in a
controller.

## Why ports and not direct calls

§5.1: *"Cross-module operations MUST use declared application interfaces or
domain events"* and *"A composition root MUST show how every required module is
constructed and registered."* §11.5 forbids persistence-specific code in a
controller. A controller that called TerminusDB directly would violate both, and
would also make WS-F unable to run until WS-B landed — six workstreams
serialised behind one.

They are `Protocol`s rather than ABCs so that WS-B's adapter does not have to
import `api.ports` to satisfy it. The dependency arrow points inward; the
persistence adapter never becomes a compile-time dependency of the API.

## Why this is not a `BUILD_VS_INTEGRATE` waiver

§14.2 and `dependency-policy.yaml -> prohibited` forbid *custom equivalents of a
selected dependency*: a custom graph database, a custom workflow engine. Neither
default adapter is one:

- `InMemoryControlPlane` stores nothing durably, has no query language, no
  branching, no versioning, and no provenance history. It is not a candidate
  replacement for TerminusDB and could not become one — §15.2 makes TerminusDB
  authoritative and this object cannot satisfy any of what that means.
- `RecordingRuntime` runs no graph. It exposes `executes_graph = False`, which
  `GET /health` reports, so "the runtime is not wired yet" is visible in the
  API's own output rather than inferable only from reading the source. DEC-001
  makes LangGraph permanent and this does not compete with it.

The alternative — shipping the API with no adapter at all — would leave every
endpoint unexecutable, which §5.2's wiring rule treats as *not complete*:
*"a module exists but is not reachable through an approved user-to-result
execution path"*. A working default is what makes the walking skeleton an
executing path rather than a diagram.

## What the default adapter genuinely does

Not a stub, and it does not pretend to facts it lacks:

- `import_project` runs the **real** `integrations.pack.load_pack`, so
  `POST /projects/import` really validates and hashes the pack and really
  rejects an invalid one with a typed `FAILED_CONTRACT`.
- The dependency registry, oracle-health rows, requirement traceability rows and
  release gate list are read out of the pack — owner facts already present, not
  invented ones.
- `terminus_commit` is reported as `null`, because no TerminusDB commit was
  obtained. The provenance view counts edges with no commit binding and shows
  it. §18 is not satisfied by this adapter and the projection says so rather
  than filling the field.

## Consequences

- WS-B replaces the control plane by satisfying the two Protocols. The ingest
  methods (`upsert_task`, `upsert_evaluation`, `upsert_model_run`,
  `upsert_knowledge`, `upsert_drift_finding`, `set_release`, `add_provenance`)
  are the shapes WS-A and WS-C are already pushing; keeping them on the
  TerminusDB adapter keeps the call sites unchanged.
- WS-C replaces the runtime and sets `executes_graph = True`. Until it does,
  `GET /health` reports `runtime_executes_graph: false`.
- A composition verifier (§5.2, GATE-D2-10) can read `executes_graph` and the
  `terminus_commit` null to tell a wired system from a partly-wired one without
  reading source.
