"""Bind every material write to an attributable immutable commit (Section 15.2).

A write goes out in two commits, and that is a deliberate cost rather than an
accident of implementation:

1. **materialise** -- the entities are inserted with their envelopes carrying
   ``terminus_database`` and ``terminus_branch``. ``terminus_commit`` is still
   unknown, because the commit is what this insert is about to create.
2. **bind** -- the commit id is read back from the branch head and written into
   each envelope, which is then re-sealed. The stored object now names the
   commit that first materialised it.

The alternative -- writing ``terminus_commit: null`` and calling it done -- fails
GATE-D1-02 A1, and back-filling the id without re-sealing would leave a
``content_hash`` that no longer verifies (A4). Two commits is the honest shape.

The receipt returned from :meth:`ProvenanceWriter.write` is the evidence record:
it names the database, branch, both commit ids, the author alias, and the
document ids, which is what Section 18's repository-change and artifact rows ask
for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from governance.envelope import content_hash
from integrations.terminusdb import CommitRecord, TerminusClient
from ontology.jsonld import to_terminus_document
from ontology.schema import ControlPlaneEntity
from provenance.binding import (
    MissingProvenanceBinding,
    assert_fully_bound,
    require_current_contract,
    seal_entity,
    verify_entity,
)

__all__ = ["ProvenanceWriter", "WriteReceipt"]


@dataclass(frozen=True)
class WriteReceipt:
    """What a material write leaves behind. Section 18 evidence, not a log line."""

    database: str
    branch: str
    author_alias: str
    message: str
    document_ids: tuple[str, ...]
    materialise_commit: str
    bind_commit: str | None
    written_at: str
    commit_record: CommitRecord | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def commit_id(self) -> str:
        """The commit that first materialised the documents."""
        return self.materialise_commit

    @property
    def is_attributable(self) -> bool:
        """GATE-D1-01 A3: an author is present and it is not the system."""
        record = self.commit_record
        if record is None:
            return bool(self.author_alias)
        return bool(record.author) and record.author != "system"

    def as_evidence(self) -> dict[str, Any]:
        payload = {
            "terminus_database": self.database,
            "terminus_branch": self.branch,
            "materialise_commit": self.materialise_commit,
            "bind_commit": self.bind_commit,
            "author_alias": self.author_alias,
            "message": self.message,
            "document_ids": list(self.document_ids),
            "written_at": self.written_at,
            "commit_is_immutable": bool(self.commit_record and self.commit_record.is_immutable),
            "author_present": self.is_attributable,
        }
        payload.update(self.extra)
        payload["evidence_hash"] = content_hash(payload)
        return payload


class ProvenanceWriter:
    """The only sanctioned way to put a control-plane entity into the graph."""

    def __init__(
        self,
        client: TerminusClient,
        *,
        database: str,
        branch: str,
        author_alias: str,
    ) -> None:
        if not author_alias.strip():
            raise MissingProvenanceBinding(
                "a material write needs an author alias (contract Section 15.2)"
            )
        self._client = client
        self._database = database
        self._branch = branch
        self._author = author_alias

    @property
    def database(self) -> str:
        return self._database

    @property
    def branch(self) -> str:
        return self._branch

    @property
    def author_alias(self) -> str:
        return self._author

    async def ensure_schema(self, schema_documents: Sequence[dict[str, Any]], *, message: str) -> str | None:
        """Install schema classes that are not present yet. Returns the commit id."""
        existing = {
            doc.get("@id")
            for doc in await self._client.get_documents(
                self._database, branch=self._branch, graph_type="schema"
            )
        }
        missing = [doc for doc in schema_documents if doc.get("@id") not in existing]
        if not missing:
            return await self._client.head_commit(self._database, self._branch)
        await self._client.insert_documents(
            self._database,
            missing,
            author=self._author,
            message=message,
            branch=self._branch,
            graph_type="schema",
        )
        return await self._client.head_commit(self._database, self._branch)

    async def write(
        self,
        entities: Sequence[ControlPlaneEntity],
        *,
        message: str,
        extra_evidence: dict[str, Any] | None = None,
        upsert: bool = False,
    ) -> WriteReceipt:
        """Materialise *entities* and bind them to the resulting commit.

        *upsert* is for **derived projections only** (Section 9.2 current-state
        views), which are recomputed from an append-only event stream. Never use
        it for a ledger event or an artifact record: overwriting one of those
        destroys the history the provenance gate reads.
        """
        if not entities:
            raise MissingProvenanceBinding("refusing to record an empty material write")
        for entity in entities:
            require_current_contract(entity.envelope)

        phase_one = [
            seal_entity(entity, database=self._database, branch=self._branch) for entity in entities
        ]
        documents = [to_terminus_document(e) for e in phase_one]
        if upsert:
            document_ids = await self._client.replace_documents(
                self._database,
                documents,
                author=self._author,
                message=message,
                branch=self._branch,
                create=True,
            )
        else:
            document_ids = await self._client.insert_documents(
                self._database,
                documents,
                author=self._author,
                message=message,
                branch=self._branch,
            )
        materialise_commit = await self._client.head_commit(self._database, self._branch)
        if not materialise_commit:
            raise MissingProvenanceBinding(
                f"no commit head after writing to {self._database}/{self._branch}"
            )

        phase_two = [seal_entity(entity, commit=materialise_commit) for entity in phase_one]
        for entity in phase_two:
            assert_fully_bound(entity)
            if not verify_entity(entity):
                raise MissingProvenanceBinding(f"{entity.document_id} failed its own content hash")

        await self._client.replace_documents(
            self._database,
            [to_terminus_document(e) for e in phase_two],
            author=self._author,
            message=f"bind provenance commit {materialise_commit}: {message}",
            branch=self._branch,
        )
        bind_commit = await self._client.head_commit(self._database, self._branch)
        record = await self._client.latest_commit(self._database, branch=self._branch)

        return WriteReceipt(
            database=self._database,
            branch=self._branch,
            author_alias=self._author,
            message=message,
            document_ids=tuple(document_ids),
            materialise_commit=materialise_commit,
            bind_commit=bind_commit,
            written_at=datetime.now(UTC).isoformat(),
            commit_record=record,
            extra=dict(extra_evidence or {}),
        )

    async def read(
        self, model: type[ControlPlaneEntity], entity_id: str
    ) -> ControlPlaneEntity | None:
        """Read one entity back and validate it against its pydantic model."""
        from ontology.jsonld import from_terminus_document

        doc = await self._client.get_document(
            self._database, f"{model.__name__}/{entity_id}", branch=self._branch
        )
        if doc is None:
            return None
        return from_terminus_document(model, doc)

    async def read_all(self, model: type[ControlPlaneEntity]) -> list[ControlPlaneEntity]:
        from ontology.jsonld import from_terminus_document

        docs = await self._client.get_documents(
            self._database, branch=self._branch, doc_type=model.__name__
        )
        return [from_terminus_document(model, doc) for doc in docs]
