# DEC-202 — Lease ledger protocol, and where fencing authority lives

**Bound to:** EFAH-CONTRACT-001 v1.1 §9.5, §9.8 · ORACLE-002 · GATE-D2-12
**Class:** implementation decision inside the contract, recorded
**Date:** 2026-08-02 · **Decided by:** builder, WS-C

## Context

ORACLE-002's method reads `read_current_lease_record_from_terminusdb`. TerminusDB
is WS-B's lane and the `efah` database does not exist yet. WS-C owns leases and
fencing and cannot wait for it, but must not grow a second authority either —
§10.1 gives TerminusDB the "what is true" question, and a lease record is a
truth.

## Decisions

### 1. Fencing logic is written against a protocol, not a store

`assignments.leases.LeaseLedger` is a `Protocol`. `LeaseFencingOracle` consumes
it and never touches storage. WS-B's TerminusDB-backed ledger satisfies the same
protocol and drops in without touching a line of ORACLE-002's verdict path.

`InMemoryLeaseLedger` is a complete implementation of that protocol, not a stub:
generation numbering, supersession, worktree and branch exclusivity, expiry, an
append-only event log. It is what the runtime uses today, and it is what the
gate is executed against.

**Known gap, stated plainly:** in-process leases do not survive a process
restart, so lease records are not yet durable across the kill/restart boundary.
GATE-D2-12's assertions are about generation fencing, not durability, and all
four hold. Handing the constructor a TerminusDB-backed ledger closes the gap
with no change to `fencing.py`.

### 2. Time comes from the ledger's clock, never from the submitter

`Submission.claimed_submitted_at` is recorded and excluded from every decision.
§9.8: "Time MUST be measured from system events, not agent estimates." This is
what defeats ORACLE-002 GP-003 — a backdated timestamp changes the transcript's
`clock_skew_observed` and changes no verdict.

`ManualClock` exists because a 1800-second lease cannot otherwise be expired
inside a test. It controls time, not the ledger: the implementation under test is
the real one.

### 3. Renewal cannot resurrect an expired lease

`renew()` raises `LeaseExpiredError` rather than extending. GP-001 is exactly the
attempt to heartbeat a dead lease back into ownership at submission time. A
counter-test (`test_a_live_lease_can_be_heartbeated`) exists so the refusal
cannot be implemented by refusing every renewal.

### 4. UNVERIFIABLE is kept distinct from FAIL

Absent lease record, missing lease identifier, and clock skew on the expiry
boundary return `UNVERIFIABLE`, never `STALE_ASSIGNMENT`. Collapsing it into
`PASS` merges unfenced work; collapsing it into `FAIL` reports a finding that was
never established. §17.2 keeps three values and so does this oracle.

### 5. Fencing runs before application, structurally

`SubmissionGateway` holds the applier and calls it only on `PASS`. GATE-D2-12 A4
("a rejected stale submission does not corrupt the winning worker's branch") is
then a property of the control flow rather than of a check someone remembered to
put first.

## Rejected alternatives

- **A local SQLite lease table** — a second durable store for a truth TerminusDB
  owns. §14.2: integrate, do not rebuild.
- **Comparing lease IDs without generations** — named in ORACLE-002
  `mutants_killed` as a mutant the oracle must reject. The oracle compares both,
  and `test_gate_d2_12_a3_...` fails if the generation comparison is removed.
- **Trusting the submitter's timestamp when no skew is detected** — "no skew
  detected" is a statement about a value the attacker supplies.
