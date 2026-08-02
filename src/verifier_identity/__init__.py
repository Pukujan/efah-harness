"""The verifier service identity — DEC-006 option B, build side.

This package is what the **builder** is allowed to know about the sealed side:
where the boundary is, how to knock on it, and how to prove the boundary holds.
It contains no holdout content, no oracle internals, and no way to obtain them.

The two halves:

``identity``
    Declares the boundary and **measures** it. Every property is checked against
    the live filesystem and process table rather than asserted, because
    GATE-D1-08 is a gate that must pass by proving access is impossible, not by
    stating that it is.

``seam``
    The opaque subprocess call. The builder invokes the generator under the
    verifier identity and receives an exit status and a count. Content cannot
    come back: the receipt shape is closed by allowlist, every field is a
    validated scalar, and the subprocess's stderr is never read into the
    builder's process at all.
"""

from verifier_identity.identity import (
    BUILDER_CANNOT_ESCALATE,
    VerifierIdentity,
    default_identity,
    measure,
)
from verifier_identity.seam import (
    PERMITTED_RECEIPT_FIELDS,
    GenerationReceipt,
    GenerationRequest,
    GenerationSeam,
    SeamOutcome,
    validate_receipt,
)

__all__ = [
    "BUILDER_CANNOT_ESCALATE",
    "PERMITTED_RECEIPT_FIELDS",
    "GenerationReceipt",
    "GenerationRequest",
    "GenerationSeam",
    "SeamOutcome",
    "VerifierIdentity",
    "default_identity",
    "measure",
    "validate_receipt",
]
