# INSTRUCTION-002 — Sandboxed, deterministic build path with mechanical box separation

**Status:** OPEN — issued 2026-08-03 by the owner, not yet started
**Supersedes nothing.** Extends PLAN-001. Depends on the six decisions in GitHub issue #3.
**Companion:** `docs/handoff/HANDOFF-004-2026-08-03.md` (state of the world at issue time)

---

## The instruction, as given

> look into stupidly simple cortex and task methodology, and lets use gVisor for sandbox
> environment for our agent to perfect these task and deterministic pipeline so they do it
> in a proper way with proven sources and shadow tests before implementation and check
> methodology for M3 M4 M5 M0 and other methods that are strong cross vendor methods —
> they should have methods of how to do graybox vs whitebox validation as well as oracle
> verdict.
>
> make sure if we wire them mechanically they're done through walking skeleton.
>
> write another contract for it — maybe a contract patch is in order.

---

## Why this instruction exists

EFAH currently **cannot write code**. `execute_work_unit` hashes the work unit's own request
metadata and returns. There is no sandbox, no worktree, no patch application, no test
execution. Every gate downstream of "did the candidate build something" is therefore
`UNVERIFIABLE` by construction — not because the checker is weak, but because nothing has
ever been submitted to it.

The repo verifier is **not** missing. It exists, it is sealed, it works. It has never been
handed a candidate.

So this is not a quality problem. It is a **missing organ**.

---

## The seven work items

### 1. Mine the cortex methodology catalog — mechanically, not by reading claims

Source of truth: `/home/yoav/ssc-github/docs/methodology/WORK-METHODOLOGIES.md`
(72 KB, **M0 through M31**, not M0–M29 as several cortex docs still claim).

The ones this instruction names, with their real line anchors:

| ID | Title | Line | Why it matters here |
|----|-------|------|---------------------|
| **M0** | Mechanism over memory (the meta-rule) | 48 | *"Wire patterns as gates/hooks/schemas, not instructions."* This is the standard the rest are judged against. |
| **M3** | The P4 build lane (how every kernel module was built) | 116 | The build-lane shape we are missing. |
| **M4** | Sealed holdout verification | 152 | Already partly implemented in EFAH (mint/grade split). |
| **M5** | Multi-model arbitration (produce → independent critique → adjudicate) | 182 | The cross-vendor loop. |
| **M20** | Oracle minting (deterministic checkers + hard gold) | 574 | The **oracle verdict** half of the ask. |
| **M29** | Seat access-control matrix (box model + forced-RAG per seat) | 936 | The **graybox/whitebox/blackbox** half of the ask. |
| **M30** | Wiring check (end-to-end from the origin, every unit) | 988 | The **walking skeleton** requirement. |
| **M31** | Declare the wire BEFORE you build | 1054 | The other half of M30. |

**Mandatory precondition before adopting any of these.** Cortex's KEDB and audit logs carry
known false positives *and* false negatives — the owner said so explicitly and it has been
confirmed repeatedly this session. **Do not adopt a method because the catalog describes it
well.** For each method, find the code that enforces it, or record that none exists.

This is not hypothetical caution. It already caught the biggest one:

> **M29 is written but not enforced.** `summon_agent()` takes **no `role`, no `read_set`, no
> `toolset` argument.** Every seat receives the same ten-tool map and the same global read
> radius, and the sealed holdouts sit inside `ALLOWED_ROOTS`. The box model is a *sentence in
> a brief*. PR #29 was not an exploit — it was the default behaviour, because blindness was
> never established. **M29 violates M0.**

That single finding is most of why this instruction exists.

### 2. gVisor sandbox for the build path

**gVisor is already installed.** Do not re-provision:

```
/usr/bin/runsc            release-20260721.0, spec 1.2.1
docker runtimes:          runc · io.containerd.runc.v2 · runsc
install log:              /home/yoav/projects/runsc-install-2026-07-25.log
```

Note the runtime's own reported feature set differs from `runc` in ways that matter for a
build sandbox and must be measured, not assumed: `cgroup.v2: false`, `apparmor: false`,
`selinux: false`, `network: sandbox`, `host-uds: none`, `oci-seccomp: false`,
`platform: systrap`. A build container that expects cgroup v2 or host UDS will behave
differently under `runsc` than under `runc`. **Measure this before building on it.**

Requirements for the sandbox:

- One container per work unit, `--runtime=runsc`.
- The only writable mount is the work unit's own worktree.
- Network egress **denied by default**; an explicit allowlist per unit if a unit genuinely
  needs the model gateway.
- Holdouts, gold, and the verifier's state directory are **not mounted at all** — not
  mounted read-only. A read-only mount of a holdout is still a leak.
- Wall-clock and memory ceilings, enforced by the runtime, not by asking.

### 3. Deterministic pipeline: proven sources and shadow tests *before* implementation

Ordering is the whole point. The pipeline must refuse to reach implementation until the
prior stages have produced artefacts:

```
research (M1/M22, citations resolve)          → CITED
   ↓  fail-closed: no resolving citation, no build
spec + oracle authored by a DIFFERENT seat    → SPECIFIED
   ↓  fail-closed: the implementer never sees the oracle source
shadow tests written and RUN against HEAD     → SHADOW-RED
   ↓  fail-closed: a shadow test that passes before implementation is void
implementation inside the gVisor sandbox      → BUILT
   ↓
shadow tests re-run                           → SHADOW-GREEN
   ↓
sealed holdout graded by the verifier         → VERDICT
```

**Shadow test discipline** (this is the part most likely to be got wrong): a shadow test
authored *before* the implementation must **fail against current HEAD**. If it passes before
anything is built, it is not testing the new behaviour and must be rejected by the pipeline,
not by a reviewer's judgement. Record the pre-implementation red result as evidence; a
green-only record proves nothing.

### 4. Graybox vs whitebox vs blackbox — mechanically enforced

Cortex M29 defines the three boxes (`WORK-METHODOLOGIES.md:936–970`):

- **WHITE-box** — reads everything relevant, including implementation source. For seats
  whose job requires it.
- **GRAY-box** — spec and interface, not implementation.
- **BLACK-box** — brief plus sealed inputs only; **no repo reads at all**.

The definitions are sound. The enforcement does not exist. **In EFAH the enforcement must be
the toolset itself**: a black-box seat is a seat *constructed without file tools*, in a
container whose mount table does not contain the repo. Not a seat asked politely.

EFAH's current seats happen to satisfy this by accident — they are toolless HTTP calls. **The
moment a seat gets a file tool, the guarantee evaporates.** Build the enforcement before
building the tool, or the tool re-creates PR #29 inside EFAH.

### 5. Oracle verdict

Keep the vocabulary already in the contract: `PASS` / `FAIL` / `UNVERIFIABLE`, with
`UNVERIFIABLE` explicitly **not** a soft pass.

Apply the session's central finding to every new checker without exception:

> **Any checker whose expected value is a constant the candidate knows is forgeable.** The
> expected value must carry per-run entropy the candidate cannot predict, **and** travel on a
> channel the candidate cannot write.

Nine checkers in cortex failed this test this session and were closed. Assume new ones will
fail it too, and test for it as a matter of routine rather than suspicion.

### 6. Everything wired through the walking skeleton

§14.4, all 15 stations. The rule, from M30/M31: **a station or it did not happen.** A module
that exists but has no station is exactly the `§26 "built but not wired"` failure that has
already bitten this project twice — most visibly the Plane projection, which was complete and
measured against the live API and had *never once* projected anything.

M31 adds the ordering constraint: **declare the wire before building the module.** For each
new component in this instruction, the station is written first.

### 7. Contract patch

Write `EFAH-CONTRACT-001 v1.2` as a **patch document**, not a rewrite. Rewriting invalidates
the 94 assertions that currently pass and destroys the row-parity baseline.

The patch must add, at minimum:

- The build-lane stage machine from item 3, with its fail-closed transitions.
- The sandbox requirements from item 2, as assertions a gate can check (runtime is `runsc`;
  mount table excludes holdout paths; egress denied).
- The box model from item 4, as a **constructed** property of a seat rather than a
  documented one.
- Shadow-test red-before-green as an assertion.
- New stations for each of the above, extending §14.4 past 15.

**Row-parity discipline applies.** Baseline run twice, re-grade every persisted row before and
after, zero drift required. A contract patch that moves a verdict it did not intend to move is
a defect in the patch.

---

## Explicit non-goals

- **Do not fan out 20–40 agents to build this.** The throttle is
  `account_wide_not_per_model` at 90 rpm with `unthrottled_fanout: forbidden`, and the policy's
  own rationale is that any 429 caused by our own fan-out is fabricated evidence. Parallel
  *verification* scaled well this session; parallel *construction* against a harness that
  cannot build produces descriptions of building.
- **Do not adopt a cortex method on the strength of its documentation.** See item 1.
- **Do not cite `BUILD-VS-INTEGRATE-001-claims.json`.** Its "8/8 SUPPORTED" verdicts were
  produced by the citation validator that was later found broken. They are not evidence.
