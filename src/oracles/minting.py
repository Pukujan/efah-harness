"""Oracle minting — the eleven Section 17.4 properties, checked and recorded.

``owner_todos.json`` lists ``content_hash`` and ``last_audit_date`` as
``TODO_computed_at_mint`` for all three oracles. They are computed here. They
are not owner questions: both are mechanically derivable from the pack and the
implementation, and contract Section 20.2 forbids asking the owner anything
discoverable by inspection.

The minted record is written to ``src/oracles/minted/`` rather than back into
the pack. ``acceptance/visible/ASSERTION_HASHES.txt`` pins the pack's bytes
(Section 14.3); a builder that edits the pack to satisfy its own gate has no
gates at all. The record therefore sits alongside the definition and binds to
it by hash, so a pack edit invalidates the mint instead of silently surviving
it.

Minting is a gate, not a stamp. :func:`mint` runs the fixture suite, runs the
declared mutants, and computes the structural no-judge proof. An oracle that
fails any of the eleven is recorded as ``minted: false`` with the reasons --
and :func:`oracles.registry.require_minted` will then refuse to let it gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.binding import CandidateBinding
from governance.envelope import CompiledObject, content_hash
from oracles import fixtures as fx
from oracles.base import MINTING_REQUIREMENTS, DeterministicOracle
from oracles.definitions import (
    MINTED_DIR,
    ORACLE_IDS,
    definition_bytes,
    load_all_definitions,
    minted_path,
)
from oracles.no_judge import prove_no_judge
from oracles.registry import IMPLEMENTATIONS, VERDICT_PATH_MODULES

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A mint older than this is stale and the oracle must be re-audited.
AUDIT_MAX_AGE_DAYS = 90


@dataclass
class RequirementCheck:
    requirement: str
    satisfied: bool
    detail: str


@dataclass
class MintRecord:
    oracle_id: str
    oracle_version: str
    content_hash: str
    last_audit_date: str
    definition_path: str
    checks: list[RequirementCheck] = field(default_factory=list)
    fixture_suite_result: str = "NOT_RUN"
    checker_test_suite_result: str = "NOT_RUN_IN_THIS_PROCESS"
    no_judge_proof: dict[str, Any] = field(default_factory=dict)
    fixture_suite: dict[str, Any] = field(default_factory=dict)
    mutants_killed: list[str] = field(default_factory=list)
    mutants_surviving: list[str] = field(default_factory=list)
    candidate_commit: str | None = None

    @property
    def minted(self) -> bool:
        return all(c.satisfied for c in self.checks)

    @property
    def unsatisfied(self) -> list[str]:
        return [c.requirement for c in self.checks if not c.satisfied]

    def as_body(self) -> dict[str, Any]:
        return {
            "oracle_id": self.oracle_id,
            "oracle_version": self.oracle_version,
            "content_hash": self.content_hash,
            "last_audit_date": self.last_audit_date,
            "definition_path": self.definition_path,
            "candidate_commit": self.candidate_commit,
            "minted": self.minted,
            "unsatisfied_requirements": self.unsatisfied,
            "requirements": [
                {"requirement": c.requirement, "satisfied": c.satisfied, "detail": c.detail}
                for c in self.checks
            ],
            "fixture_suite_result": self.fixture_suite_result,
            "checker_test_suite_result": self.checker_test_suite_result,
            "no_judge_proof": self.no_judge_proof,
            "fixture_suite": self.fixture_suite,
            "mutants_killed": self.mutants_killed,
            "mutants_surviving": self.mutants_surviving,
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.oracle_mint_record",
            created_by_alias="oracle-o02",
            body=self.as_body(),
        )


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _mutant_reports_for(oracle_id: str, oracles: dict[str, DeterministicOracle]) -> tuple[list[str], list[str]]:
    """Run only the mutants that name this oracle, and report kills and survivors."""
    from mutants.runner import all_mutants  # local: keeps the verdict path import-free

    killed: list[str] = []
    survived: list[str] = []
    for mutant in all_mutants():
        if mutant.target != oracle_id or mutant.declared_as is None:
            continue
        try:
            report = mutant.run(oracles)
        except Exception as exc:
            survived.append(f"{mutant.declared_as} (raised {type(exc).__name__}: {exc})")
            continue
        (killed if report.killed else survived).append(mutant.declared_as)
    return killed, survived


def mint(
    oracle_id: str,
    oracles: dict[str, DeterministicOracle],
    *,
    candidate_commit: str | None = None,
) -> MintRecord:
    """Compute the Section 17.4 checklist for one oracle and produce its record."""
    oracle = oracles[oracle_id]
    definition = oracle.definition
    raw = definition_bytes(oracle_id)

    record = MintRecord(
        oracle_id=oracle_id,
        oracle_version=oracle.oracle_version,
        # The hash binds the *pack bytes*, so an edit to the definition
        # invalidates the mint rather than silently surviving it.
        content_hash=content_hash(raw),
        last_audit_date=_today(),
        definition_path=str(definition.get("_source_path", "")),
        candidate_commit=candidate_commit,
    )

    suite = fx.run_fixture_suite(oracle)
    record.fixture_suite = suite.as_evidence()
    record.fixture_suite_result = suite.summary

    proof = prove_no_judge(VERDICT_PATH_MODULES[oracle_id])
    record.no_judge_proof = proof.as_evidence()

    killed, survived = _mutant_reports_for(oracle_id, oracles)
    record.mutants_killed = killed
    record.mutants_surviving = survived

    declared_mutants = [str(m) for m in (definition.get("mutants_killed") or [])]
    missing_mutants = sorted(set(declared_mutants) - set(killed))
    missing_fixtures = fx.missing_fixture_ids(definition)
    pinned = REPO_ROOT / str(definition.get("pinned_checker_test_suite", ""))

    kinds = {f.kind for f in fx.fixtures_for(oracle_id)}
    unverifiable_declared = list(definition.get("unverifiable_when") or [])

    checks = {
        "deterministic_verdict_path_with_no_hidden_model_call": (
            definition.get("deterministic_verdict_path") is True
            and definition.get("model_call_in_verdict_path") is False,
            (
                "definition declares deterministic_verdict_path="
                f"{definition.get('deterministic_verdict_path')}, "
                "model_call_in_verdict_path="
                f"{definition.get('model_call_in_verdict_path')}"
            ),
        ),
        "structural_proof_no_judge_participates": (
            proof.holds,
            (
                f"import closure of {proof.entry_module} covers "
                f"{len(proof.modules_in_closure)} modules; violations={proof.violations}"
            ),
        ),
        "independent_second_checker_comparison_where_feasible": (
            bool((definition.get("independent_second_checker") or {}).get("method")),
            f"second checker: {(definition.get('independent_second_checker') or {}).get('method')}",
        ),
        "known_good_fixtures": (
            fx.KNOWN_GOOD in kinds and not missing_fixtures,
            (
                f"fixture kinds present: {sorted(kinds)}; "
                f"definition IDs with no fixture: {missing_fixtures}"
            ),
        ),
        "known_bad_fixtures": (
            fx.KNOWN_BAD in kinds and not missing_fixtures,
            f"known-bad fixtures implemented; missing declared IDs: {missing_fixtures}",
        ),
        "gaming_probes": (
            fx.GAMING_PROBE in kinds,
            f"{sum(1 for f in fx.fixtures_for(oracle_id) if f.kind == fx.GAMING_PROBE)} probes",
        ),
        "mutants_that_it_kills": (
            bool(declared_mutants) and not missing_mutants and not survived,
            (
                f"declared={declared_mutants}; killed={killed}; survived={survived}; "
                f"declared-but-never-killed={missing_mutants}"
            ),
        ),
        "honest_unverifiable_output": (
            bool(unverifiable_declared) and fx.UNVERIFIABLE_PROBE in kinds and suite.ok,
            (
                f"unverifiable_when={unverifiable_declared}; "
                f"probes exercised and matched={suite.ok}"
            ),
        ),
        "pinned_checker_test_suite": (
            pinned.is_file(),
            (
                f"{definition.get('pinned_checker_test_suite')} "
                f"{'exists' if pinned.is_file() else 'DOES NOT EXIST'}"
            ),
        ),
        "version_and_content_hash": (
            bool(oracle.oracle_version) and record.content_hash.startswith("sha256:"),
            f"version={oracle.oracle_version}, content_hash={record.content_hash}",
        ),
        "last_audit_date_and_health_emitted_with_every_result": (
            bool(record.last_audit_date) and bool(oracle.declared_health_fields),
            (
                f"last_audit_date={record.last_audit_date}; "
                f"health fields declared={oracle.declared_health_fields}"
            ),
        ),
    }

    for requirement in MINTING_REQUIREMENTS:
        satisfied, detail = checks[requirement]
        record.checks.append(RequirementCheck(requirement, bool(satisfied), detail))
    return record


def mint_all(
    oracles: dict[str, DeterministicOracle] | None = None,
    *,
    candidate_commit: str | None = None,
) -> dict[str, MintRecord]:
    from oracles.registry import build_oracles

    built = oracles or build_oracles()
    return {
        oracle_id: mint(oracle_id, built, candidate_commit=candidate_commit)
        for oracle_id in ORACLE_IDS
    }


def write_records(records: dict[str, MintRecord], directory: Path | None = None) -> list[Path]:
    target = directory or MINTED_DIR
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for oracle_id, record in records.items():
        obj = record.to_compiled_object()
        path = minted_path(oracle_id, target)
        path.write_text(json.dumps(obj.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def audit_age_days(record_body: dict[str, Any], today: str | None = None) -> int:
    minted_on = datetime.fromisoformat(record_body["last_audit_date"]).date()
    now = datetime.fromisoformat(today).date() if today else datetime.now(UTC).date()
    return (now - minted_on).days


def main(argv: list[str] | None = None) -> int:
    """``python -m oracles.minting`` — mint all three and write their records."""
    from oracles.registry import build_oracles

    try:
        binding = CandidateBinding.from_head()
        commit = binding.commit_sha
    except Exception:
        commit = None

    definitions = load_all_definitions()
    built = {
        oid: IMPLEMENTATIONS[oid](definitions[oid]) for oid in ORACLE_IDS
    }
    records = mint_all(built, candidate_commit=commit)
    paths = write_records(records)

    failed = False
    for oracle_id, record in sorted(records.items()):
        status = "MINTED" if record.minted else "NOT MINTED"
        print(f"{oracle_id} {status}  version={record.oracle_version} hash={record.content_hash[:19]}")
        for check in record.checks:
            if not check.satisfied:
                print(f"    UNSATISFIED {check.requirement}: {check.detail}")
        failed |= not record.minted
    for path in paths:
        print(f"  wrote {path.relative_to(REPO_ROOT)}")

    # Reload so the emitted records are what the registry will actually use.
    build_oracles()
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
