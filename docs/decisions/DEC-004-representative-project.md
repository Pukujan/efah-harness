# DEC-004 — Representative project selection

**Bound to:** EFAH-CONTRACT-001 v1.1
**Class:** recorded decision under `project.yaml → representative_project.selection_mode: builder_selects_and_records`
**Date:** 2026-08-02 · **Decided by:** builder, under owner authorization dated 2026-08-01

## Selection

**WU-REP-001 — the harness's own contract-compilation path, carried end to end.**

Compile `contract.md` v1.1 → requirements → tasks → dependency graph → a real
code change → visible test → protected verifier verdict → CI gate → merge.

## Against the recorded constraints

| Constraint | How it is met |
|---|---|
| `must_be_real_not_synthetic` | It is this build's own critical path, not a fixture authored to be passed. |
| `must_have_a_deterministic_oracle` | ORACLE-001 (composition reachability) and ORACLE-003 (provenance binding) both decide it with no model in the verdict path. |
| `must_be_completable_within_the_day_3_window` | Every input already exists in the pack; no external dependency is introduced. |
| `must_exercise_the_full_path_not_a_subset` | Traverses all fifteen §14.4 stations, including the AMENDMENT-001 owner control surface. |

## Rejected alternatives

- **A synthetic toy repository** — fails `must_be_real_not_synthetic`, and a
  builder-authored fixture that the builder then passes is precisely the circular
  validation contract §2.1 and §12.2 forbid.
- **An unrelated third-party repository** — would introduce discovery and
  environment cost inside a 48-hour window with no gain in path coverage, and
  contract §24's delivery rule forbids broadening task families while a required
  end-to-end gate remains unwired.
