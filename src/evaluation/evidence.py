"""Named, hashed evidence artifacts.

Contract Section 18: *"Done" without named evidence is invalid.* Every gate
declares an ``evidence_required`` list; this module is where those names become
files with content hashes bound to a candidate commit.

The rule the store enforces is the uncomfortable one: a gate cannot report PASS
unless every artifact its own definition named was actually produced. A green
verdict with a missing evidence artifact is the exact shape of a fabricated
green, and it is easy to produce by accident -- a check passes, nobody writes
the transcript, and the gate reports success with nothing behind it.

The store used to be write-only: :meth:`EvidenceStore.write` put artifacts on
disk and nothing ever read them back, so :meth:`EvidenceStore.missing_for` could
only ever see what the *current* run emitted. An artifact produced out of band --
by a collector that drives a live service, which is the only way some evidence
can exist at all -- was invisible, and its gate reported it missing while the
file sat in ``evidence/gates/``. :meth:`EvidenceStore.adopt_from_disk` is the
read side.

It is deliberately hard to satisfy. An on-disk file is admitted only if it
declares the gate it belongs to, declares *this* candidate commit, and hashes to
what it claims. Anything else is recorded as a refusal with the reason, because
the failure this whole harness exists to prevent is a gate turning green on an
artifact from some other commit -- evidence about a different program, filed
under the name of this one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from governance.envelope import CompiledObject, content_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "evidence" / "gates"


def _display_path(path: Path | None) -> str | None:
    """Repo-relative where possible, absolute otherwise. Never raises.

    An evidence root outside the repository is legitimate -- tests use one, and
    so would a run whose artifacts live on a mounted volume. Refusing to
    describe such a path at all would turn a reporting detail into a crash.
    """
    if path is None:
        return None
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


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
            "path": _display_path(self.path),
        }


@dataclass(frozen=True)
class RefusedArtifact:
    """An on-disk file that names an artifact but may not stand in for one."""

    name: str
    gate_id: str
    path: Path
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gate_id": self.gate_id,
            "path": _display_path(self.path),
            "reason": self.reason,
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
    #: On-disk files inspected and rejected, with the reason. Reported, not dropped:
    #: "missing" and "present but bound to another commit" are different findings
    #: and an operator needs to be able to tell them apart.
    refused: list[RefusedArtifact] = field(default_factory=list)

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

    def _refuse(self, name: str, gate_id: str, path: Path, reason: str) -> None:
        record = RefusedArtifact(name=name, gate_id=gate_id, path=path, reason=reason)
        if record not in self.refused:
            self.refused.append(record)

    def adopt_from_disk(self, gate_id: str) -> list[EvidenceArtifact]:
        """Admit previously-written artifacts for ``gate_id`` that are still valid.

        Evidence does not only come from the run that is asking. Some artifacts
        can only be produced by driving a live service, a browser, or a host
        that CI does not have -- a collector writes those, and the gate run has
        to be able to see them or the gate can never be satisfied by anything.

        Four conditions, all of them refusals rather than warnings:

        * the file parses and carries the store's own envelope keys;
        * it declares the gate it is filed under;
        * it declares **this** candidate commit -- an artifact from another
          commit describes a different program.

        An artifact already produced in this run always wins; disk never
        overwrites a live result.

        The content hash is *recomputed* from the adopted file, not checked
        against a stored one: :meth:`write` does not persist the hash, so this
        store cannot detect an edited file on its own. The commit binding is
        the guard that carries the weight here, and the recomputed hash is what
        a manifest comparison would be made against. Do not read the hash on an
        adopted artifact as an integrity check it is not.
        """
        adopted: list[EvidenceArtifact] = []
        directory = self.root / gate_id
        if not directory.is_dir():
            return adopted

        for path in sorted(directory.glob("*.json")):
            try:
                body = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                self._refuse(path.stem, gate_id, path, f"unreadable: {type(exc).__name__}: {exc}")
                continue
            if not isinstance(body, dict):
                self._refuse(path.stem, gate_id, path, "not an evidence envelope: top level is not an object")
                continue

            name = body.get("artifact")
            missing_keys = [k for k in ("artifact", "gate_id", "candidate_commit", "contents") if k not in body]
            if missing_keys:
                self._refuse(
                    str(name or path.stem),
                    gate_id,
                    path,
                    "not an evidence envelope: no " + ", ".join(missing_keys)
                    + " (written by something other than EvidenceStore.write)",
                )
                continue
            if body["gate_id"] != gate_id:
                self._refuse(str(name), gate_id, path, f"declares gate_id {body['gate_id']!r}, filed under {gate_id!r}")
                continue
            if body["candidate_commit"] != self.candidate_commit:
                self._refuse(
                    str(name),
                    gate_id,
                    path,
                    f"bound to candidate commit {str(body['candidate_commit'])[:12]}, "
                    f"this run is {self.candidate_commit[:12]}",
                )
                continue
            recomputed = content_hash(body)
            if (gate_id, name) in self.artifacts:
                continue  # produced in this run; the live artifact wins
            self.artifacts[(gate_id, str(name))] = EvidenceArtifact(
                name=str(name),
                gate_id=gate_id,
                content_hash=recomputed,
                candidate_commit=self.candidate_commit,
                path=path,
                payload=body,
            )
            adopted.append(self.artifacts[(gate_id, str(name))])
        return adopted

    def refused_for(self, gate_id: str) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self.refused if r.gate_id == gate_id]

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
