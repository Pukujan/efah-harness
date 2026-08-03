# DEC-201 — Checkpoint adapter shape and strict serialization posture

**Bound to:** EFAH-CONTRACT-001 v1.1 §10.3, §10.4 · GATE-D1-04
**Class:** implementation decision inside the contract, recorded (not a `BUILD_VS_INTEGRATE`)
**Date:** 2026-08-02 · **Decided by:** builder, WS-C
**Evidence:** `project-pack/evidence/context7-snapshots/C7-langgraph-1.2.10-296ef01c.json`

## Context

§10.3 requires `AsyncSqliteSaver`, *behind a checkpoint adapter*, with *strict
safe serialization*, treated as *rebuildable execution state*. §10.4 requires
twelve references on every checkpoint. DEC-001 fixes LangGraph as the permanent
runtime; Temporal is a non-goal and is not a candidate backend.

Nothing here is a reimplementation: `langgraph-checkpoint-sqlite` does the
storage. What is decided is the shape of the seam around it.

## Decisions

### 1. The adapter returns harness types, not LangGraph types

`SqliteCheckpointAdapter.list_checkpoints` returns `CheckpointRecord`, not
`CheckpointTuple`. §10.3 permits replacing the checkpointer "without changing
domain schemas"; that promise is only real if callers never held a vendor type
in the first place. `saver()` is the single deliberate leak, and it exists
because `builder.compile(checkpointer=...)` needs the vendor object.

### 2. Strict serialization is chosen explicitly, not inherited

Probed against the installed package: `JsonPlusSerializer.__init__` defaults
`allowed_msgpack_modules` to **permissive** unless `LANGGRAPH_STRICT_MSGPACK=true`
is set in the environment. An adapter that merely constructed the default and
called it "safe serialization" would be wrong, and wrong invisibly.

`strict_serializer()` therefore passes `pickle_fallback=False`,
`allowed_json_modules=None`, `allowed_msgpack_modules=None` in code, so the
posture does not depend on an environment variable being set on the host that
happens to run the graph. `extra_allowed_modules` is additive only; there is no
argument that restores the permissive default.

Consequence accepted: the state model must stay plain `str`/`int`/`list`/`dict`.
That is a benefit — `tests/unit/test_workflow_state.py` shows the twelve §10.4
fields are all JSON-shaped, so the checkpoint stays inspectable.

### 3. §10.4 is enforced at the write seam, not asserted after the fact

`_Section104EnforcingSaver.aput` raises `MissingCheckpointFields` when a
state-carrying checkpoint omits any of the twelve. GATE-D1-04 A3 asserts *every*
checkpoint carries them; a post-hoc assertion can only detect a violation that
already happened, while a refusal at `aput` makes the violating state
unreachable.

The exemption is narrow and named: checkpoints holding only framework channels
(`__start__`, `branch:*`, …) carry no graph state yet. Verified empirically —
LangGraph writes an input-staging checkpoint whose `channel_values` is
`{"__start__": …}` before the first super-step.

### 4. The store is deletable on purpose

`destroy()` is part of the adapter's public surface, and refuses to run while
the adapter is open. §10.1 says TerminusDB answers what is true; if deleting the
checkpoint store could destroy project truth, the boundary would be a slogan.
`tests/integration/test_langgraph_project_run.py` deletes it and re-derives the
plan hash from the pack.

## Rejected alternatives

- **A `BaseCheckpointSaver` wrapper that delegates by composition** — Pregel
  reaches for `serde`, sync and async methods, and version helpers on the saver
  it is given. Subclassing `AsyncSqliteSaver` and overriding one method has a
  strictly smaller surface to get wrong than re-exporting a dozen.
- **Validating §10.4 in each node** — twelve graphs times N nodes of remembering.
  One seam, one rule.
- **Setting `LANGGRAPH_STRICT_MSGPACK=true` in the environment** — makes the
  security posture a property of the deployment rather than of the code, and
  fails open on any host that forgets it.
