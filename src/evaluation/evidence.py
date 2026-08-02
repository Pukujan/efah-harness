"""Named, hashed evidence artifacts.

Contract Section 18: *"Done" without named evidence is invalid.* Every gate
declares an ``evidence_required`` list; this module is where those names become
files with content hashes bound to a candidate commit.

The rule the store enforces is the uncomfortable one: a gate cannot report PASS
unless every artifact its own definition named was actually produced. A green
verdict with a missing evidence artifact is the exact shape of a fabricated
green, and it is easy to produce by accident -- a check passes, nobody writes
the transcript, and the gate reports success with nothing behind it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from governance.envelope import CompiledObject, content_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "evidence" / "gates"


@dataclass(frozen=True)
class EvidenceArtifact:
    name: str
    gate_id: str
    content_hash: str
    candidate_commit: str
    path: Path | None
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def as_reference(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gate_id": self.gate_id,
            "content_hash": self.content_hash,
            "candidate_commit": self.candidate_commit,
            "path": str(self.path.relative_to(REPO_ROOT)) if self.path else None,
        }


@dataclass
class EvidenceStore:
    """Collects artifacts in memory, and writes them only when asked to.

    Keeping the write optional matters for tests: a gate run must be
    exercisable without leaving files behind, and an artifact that only exists
    because a test ran is not evidence about the product.
    """

    candidate_commit: str
    root: Path = DEFAULT_EVIDENCE_ROOT
    artifacts: dict[tuple[str, str], EvidenceArtifact] = field(default_factory=dict)

    def add(self, gate_id: str, name: str, payload: dict[str, Any]) -> EvidenceArtifact:
        body = {
            "artifact": name,
            "gate_id": gate_id,
            "candidate_commit": self.candidate_commit,
            "contents": payload,
        }
        artifact = EvidenceArtifact(
            name=name,
            gate_id=gate_id,
            content_hash=content_hash(body),
            candidate_commit=self.candidate_commit,
            path=None,
            payload=body,
        )
        self.artifacts[(gate_id, name)] = artifact
        return artifact

    def names_for(self, gate_id: str) -> set[str]:
        return {name for (gid, name) in self.artifacts if gid == gate_id}

    def missing_for(self, gate_id: str, required: tuple[str, ...]) -> list[str]:
        produced = self.names_for(gate_id)
        return [name for name in required if name not in produced]

    def references_for(self, gate_id: str) -> list[dict[str, Any]]:
        return [
            artifact.as_reference()
            for (gid, _), artifact in sorted(self.artifacts.items())
            if gid == gate_id
        ]

    def write(self, gate_id: str | None = None) -> list[Path]:
        """Persist artifacts to disk and rebind each artifact to its real path."""
        written: list[Path] = []
        for key, artifact in list(self.artifacts.items()):
            gid, name = key
            if gate_id is not None and gid != gate_id:
                continue
            directory = self.root / gid
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{name}.json"
            path.write_text(json.dumps(artifact.payload, indent=2, sort_keys=True, default=str) + "\n")
            self.artifacts[key] = EvidenceArtifact(
                name=artifact.name,
                gate_id=artifact.gate_id,
                content_hash=artifact.content_hash,
                candidate_commit=artifact.candidate_commit,
                path=path,
                payload=artifact.payload,
            )
            written.append(path)
        return written

    def to_compiled_object(self, gate_id: str) -> CompiledObject:
        return CompiledObject.create(
            schema_id="efah.evidence_manifest",
            created_by_alias="auditor-a07",
            body={
                "gate_id": gate_id,
                "candidate_commit": self.candidate_commit,
                "artifacts": self.references_for(gate_id),
            },
        )
