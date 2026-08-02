"""EFAH module: provenance. Contract EFAH-CONTRACT-001 v1.1 Sections 8, 15.2, 18.

Every material write is bound to an attributable immutable TerminusDB commit,
and candidate changes land on an isolated branch.
"""

from provenance.binding import (
    REQUIRED_ENVELOPE_FIELDS,
    MissingProvenanceBinding,
    StaleContractVersion,
    assert_fully_bound,
    entity_body,
    require_current_contract,
    seal_entity,
    verify_entity,
)
from provenance.importer import (
    EFAH_DATABASE,
    PackImportResult,
    build_pack_entities,
    import_project_pack,
    make_import_branch_name,
)
from provenance.writer import ProvenanceWriter, WriteReceipt

__all__ = [
    "EFAH_DATABASE",
    "REQUIRED_ENVELOPE_FIELDS",
    "MissingProvenanceBinding",
    "PackImportResult",
    "ProvenanceWriter",
    "StaleContractVersion",
    "WriteReceipt",
    "assert_fully_bound",
    "build_pack_entities",
    "entity_body",
    "import_project_pack",
    "make_import_branch_name",
    "require_current_contract",
    "seal_entity",
    "verify_entity",
]
