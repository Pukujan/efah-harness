"""Candidate binding — one exact commit for every evaluation lane.

GATE-D2-19 exists because of a specific cheat: pass the visible tests on one
commit, pass the holdouts on another, and report both greens as if they
described the same artifact. The fix is not a policy, it is a data structure --
every lane run carries the candidate SHA, and an :class:`EvaluationSet` that
sees two different SHAs invalidates itself instead of reporting a verdict.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from governance.envelope import CompiledObject, utc_now
from governance.states import Verdict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Lane(StrEnum):
    """Contract Section 17.1 evaluation lanes that must agree on a commit."""

    VISIBLE = "visible"
    HIDDEN = "hidden"
    MUTANT = "mutant"


class CommitUnavailable(RuntimeError):
    """The candidate commit could not be resolved, so nothing may be bound to it."""


def resolve_head(repo_root: Path | None = None) -> str:
    """Return the exact candidate commit SHA. No fallback, no 'unknown'."""
    root = repo_root or REPO_ROOT
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CommitUnavailable(f"cannot resolve HEAD in {root}: {exc}") from exc
    sha = completed.stdout.strip()
    if len(sha) != 40:
        raise CommitUnavailable(f"git returned an unusable SHA: {sha!r}")
    return sha


@dataclass(frozen=True)
class CandidateBinding:
    """The artifact identity every lane, oracle result, and gate result cites."""

    commit_sha: str
    repo_root: Path = REPO_ROOT
    contract_version: str = "1.1"
    bound_at: str = field(default_factory=utc_now)

    @classmethod
    def from_head(cls, repo_root: Path | None = None, contract_version: str = "1.1") -> CandidateBinding:
        root = repo_root or REPO_ROOT
        return cls(commit_sha=resolve_head(root), repo_root=root, contract_version=contract_version)

    @property
    def short(self) -> str:
        return self.commit_sha[:12]


@dataclass
class LaneRun:
    lane: Lane
    candidate_commit: str
    verdict: Verdict
    detail: str = ""
    started_at: str = field(default_factory=utc_now)


@dataclass
class EvaluationSet:
    """Visible, hidden, and mutant lanes bound to one candidate commit."""

    evaluation_request_id: str
    binding: CandidateBinding
    runs: dict[Lane, LaneRun] = field(default_factory=dict)
    invalidated_because: list[str] = field(default_factory=list)

    def record(self, run: LaneRun) -> None:
        if run.candidate_commit != self.binding.commit_sha:
            # GATE-D2-19 A3: a commit change between lanes invalidates the set.
            self.invalidated_because.append(
                f"{run.lane.value} lane ran against {run.candidate_commit[:12]}, "
                f"the set is bound to {self.binding.short}"
            )
        self.runs[run.lane] = run

    @property
    def invalidated(self) -> bool:
        return bool(self.invalidated_because)

    @property
    def lanes_agree(self) -> bool:
        shas = {run.candidate_commit for run in self.runs.values()}
        return len(shas) == 1 and not self.invalidated

    @property
    def complete(self) -> bool:
        return set(self.runs) == set(Lane)

    def verdict(self) -> Verdict:
        """A set that is not complete or not in agreement cannot say PASS."""
        if self.invalidated:
            return Verdict.FAIL
        if not self.complete:
            return Verdict.UNVERIFIABLE
        if any(run.verdict is Verdict.FAIL for run in self.runs.values()):
            return Verdict.FAIL
        if any(run.verdict is Verdict.UNVERIFIABLE for run in self.runs.values()):
            return Verdict.UNVERIFIABLE
        return Verdict.PASS

    def as_evidence(self) -> dict[str, Any]:
        return {
            "check": "commit_sha_compare_across_lanes",
            "expected": "all_three_equal",
            "evaluation_request_id": self.evaluation_request_id,
            "candidate_commit": self.binding.commit_sha,
            "contract_version": self.binding.contract_version,
            "lanes": {
                lane.value: {
                    "candidate_commit": run.candidate_commit,
                    "verdict": run.verdict.value,
                    "detail": run.detail,
                    "started_at": run.started_at,
                }
                for lane, run in sorted(self.runs.items())
            },
            "lanes_present": sorted(lane.value for lane in self.runs),
            "lanes_missing": sorted(lane.value for lane in set(Lane) - set(self.runs)),
            "invalidated": self.invalidated,
            "invalidated_because": self.invalidated_because,
            "set_verdict": self.verdict().value,
        }

    def to_compiled_object(self) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.evaluation_set",
            created_by_alias="oracle-o02",
            body=self.as_evidence(),
        )
