"""Request and response schemas (contract Sections 8.1, 11.4).

Section 8.1 forbids silent defaults for material fields, so every command model
sets ``extra='forbid'`` and makes material fields required. A request carrying
an unexpected key is rejected rather than partially honoured -- otherwise a
typo in ``contract_version`` would silently skip version binding.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from governance.envelope import CONTRACT_ID, CONTRACT_VERSION
from governance.states import ContractReviewOutcome


class Command(BaseModel):
    """Base for every write command. Closed world, no defaults for material fields."""

    model_config = ConfigDict(extra="forbid")


class ImportProjectCommand(Command):
    """``POST /projects/import``. Section 6: the owner supplies one directory."""

    pack_root: str = Field(min_length=1, description="Filesystem path to the project pack")
    mode: str = Field(default="autonomous", pattern="^(autonomous|supervised)$")


class RunProjectCommand(Command):
    """``POST /projects/{id}/run``."""

    mode: str = Field(default="autonomous", pattern="^(autonomous|supervised)$")
    reason: str = Field(default="", max_length=2000)


class ResumeTaskCommand(Command):
    """``POST /tasks/{id}/resume``. Section 10.6: resume, never restart."""

    reason: str = Field(default="", max_length=2000)
    #: Optional owner answer supplied together with the resume, when the task is
    #: BLOCKED_OWNER_DECISION. Recorded as a Decision bound to a contract version.
    owner_answer: str | None = Field(default=None, max_length=8000)


class ApproveContractCommand(Command):
    """``POST /contracts/{id}/approve``. Section 20.1 initial contract approval."""

    approved_version: str = Field(min_length=1)
    approver: str = Field(min_length=1, max_length=200)
    rationale: str = Field(default="", max_length=8000)


class ReviewContractCommand(Command):
    """``POST /contracts/{id}/review``. Section 19.3/19.4."""

    outcome: ContractReviewOutcome
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=8000)
    project_id: str = Field(min_length=1)


class Acknowledgement(BaseModel):
    """Every write response carries the binding that authorised it."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    contract_id: str = CONTRACT_ID
    contract_version: str = CONTRACT_VERSION
    correlation_id: str | None = None
    trace_id: str | None = None
    detail: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Section 5.2 requires every module to declare a health check."""

    model_config = ConfigDict(extra="forbid")

    status: str
    contract_id: str = CONTRACT_ID
    contract_version: str = CONTRACT_VERSION
    projects_loaded: int = 0
    runtime_executes_graph: bool = False
    projection_available: bool | None = None
    tracing_installed: bool = False
