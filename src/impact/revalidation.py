"""AMENDMENT-001 recompilation and revalidation.

Contract Section 1.3 lists seven steps for a valid material change. Steps 1-5
are the owner's and are already done (exact clause, impact analysis, approval,
new version, commit). Steps 6 and 7 are the builder's and are implemented here:

* **step 6** -- recompiled workflow and gate definitions. The gate set is
  recompiled from the pack, GATE-D1-10 must appear in it as blocking on Day 1,
  and the Section 14.4 walking-skeleton trace is recompiled with the step the
  amendment adds.
* **step 7** -- revalidation of affected objects. The amendment's own impact
  analysis names them; this module reads that analysis rather than restating it,
  and emits one revalidation record per named object saying whether it changed
  and why. An object the amendment marks *unchanged* still gets a record --
  Section 15.7 requires affected objects to be revalidated, not assumed.

DEC-005 is compiled alongside it. The owner reordered ``delivery_priority`` so
GATE-D1-10 precedes GATE-D1-07. Both remain blocking; a reorder is an
``OWNER_PRIORITY_DECISION`` under Section 10.7, not a Section 1.3 amendment, and
the compiled output records that distinction explicitly so a later reader cannot
mistake it for a weakening.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import markdown
from governance.states import ContractReviewOutcome, OwnerInterrupt

AMENDMENT_FILENAME = "AMENDMENT-001-owner-control-surface.md"
AMENDMENT_ID = "AMENDMENT-001"
AMENDMENT_GATE = "GATE-D1-10"
DEC_005_GLOB = "DEC-005-*.md"

_SECTION_REF = re.compile(r"^§(\d+(?:\.\d+)*)\s+(.*)$")
_BACKTICKED = re.compile(r"`([^`]+)`")
_GATE_ID = re.compile(r"GATE-D\d-\d\d")


class AmendmentError(RuntimeError):
    """The amendment cannot be compiled -- Section 1.3 is not satisfied."""


@dataclass(frozen=True)
class RevalidationRecord:
    """Section 1.3 step 7, one per object the amendment's analysis names."""

    object_ref: str
    object_name: str
    changed: bool
    reason: str
    amendment_id: str
    from_contract_version: str
    to_contract_version: str
    outcome: str
    revalidation_action: str

    def as_body(self) -> dict[str, Any]:
        return {
            "object_ref": self.object_ref,
            "object_name": self.object_name,
            "changed": self.changed,
            "reason": self.reason,
            "amendment_id": self.amendment_id,
            "from_contract_version": self.from_contract_version,
            "to_contract_version": self.to_contract_version,
            "outcome": self.outcome,
            "revalidation_action": self.revalidation_action,
        }


@dataclass
class WalkingSkeletonRecompilation:
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    added_step: str = ""
    inserted_after: str = ""

    def as_body(self) -> dict[str, Any]:
        return {
            "contract_ref": "contract.md#14.4 as amended by AMENDMENT-001",
            "steps_before_amendment": self.before,
            "steps_after_amendment": self.after,
            "added_step": self.added_step,
            "inserted_after": self.inserted_after,
            "step_count_before": len(self.before),
            "step_count_after": len(self.after),
        }


@dataclass
class DeliveryPriority:
    """DEC-005 output: the owner's reordered priority list."""

    source: str = ""
    superseded_source: str = ""
    ordered_items: list[str] = field(default_factory=list)
    gate_order: list[str] = field(default_factory=list)
    superseded_gate_order: list[str] = field(default_factory=list)
    blocking: dict[str, bool] = field(default_factory=dict)
    interrupt_class: str = str(OwnerInterrupt.OWNER_PRIORITY_DECISION)
    reorder_not_weakening: bool = True

    def gates_still_blocking_all(self) -> bool:
        """A reorder may not weaken: every reordered gate stays blocking."""
        return bool(self.blocking) and all(self.blocking.values())

    def as_body(self) -> dict[str, Any]:
        return {
            "decision_id": "DEC-005",
            "source": self.source,
            "supersedes": self.superseded_source,
            "ordered_items": self.ordered_items,
            "gate_execution_order": self.gate_order,
            "superseded_gate_execution_order": self.superseded_gate_order,
            "gates_still_blocking": self.blocking,
            "interrupt_class": self.interrupt_class,
            "reorder_not_weakening": self.reorder_not_weakening,
            "contract_ref": "contract.md#10.7 OWNER_PRIORITY_DECISION",
        }


@dataclass
class AmendmentRecompilation:
    amendment_id: str = AMENDMENT_ID
    from_version: str = "1.0"
    to_version: str = "1.1"
    approved_by: str = ""
    approved_at: str = ""
    document_hash: str = ""
    gate_ids: list[str] = field(default_factory=list)
    amendment_gate_ids: list[str] = field(default_factory=list)
    blocking_gate_ids: list[str] = field(default_factory=list)
    day_1_gate_ids: list[str] = field(default_factory=list)
    walking_skeleton: WalkingSkeletonRecompilation = field(default_factory=WalkingSkeletonRecompilation)
    revalidation_records: list[RevalidationRecord] = field(default_factory=list)
    delivery_priority: DeliveryPriority = field(default_factory=DeliveryPriority)

    def step_6_body(self) -> dict[str, Any]:
        return {
            "contract_ref": "contract.md#1.3 step 6 -- recompiled workflow and gate definitions",
            "amendment_id": self.amendment_id,
            "from_contract_version": self.from_version,
            "to_contract_version": self.to_version,
            "gate_count": len(self.gate_ids),
            "gate_ids": self.gate_ids,
            "gates_added_by_amendment": self.amendment_gate_ids,
            "blocking_gate_ids": self.blocking_gate_ids,
            "day_1_gate_ids": self.day_1_gate_ids,
            "amendment_gate_is_blocking_day_1": (
                AMENDMENT_GATE in self.blocking_gate_ids and AMENDMENT_GATE in self.day_1_gate_ids
            ),
            "walking_skeleton": self.walking_skeleton.as_body(),
        }

    def step_7_body(self) -> dict[str, Any]:
        return {
            "contract_ref": "contract.md#1.3 step 7 -- revalidation of affected objects",
            "amendment_id": self.amendment_id,
            "affected_object_count": len(self.revalidation_records),
            "changed": [r.object_ref for r in self.revalidation_records if r.changed],
            "unchanged": [r.object_ref for r in self.revalidation_records if not r.changed],
            "records": [r.as_body() for r in self.revalidation_records],
        }


# --------------------------------------------------------------------------


def _amendment_path(pack_root: Path) -> Path:
    path = pack_root / "evidence" / "owner-documents" / AMENDMENT_FILENAME
    if not path.is_file():
        raise AmendmentError(f"{AMENDMENT_ID} not found at {path}; contract v1.1 cannot be compiled without it")
    return path


def parse_impact_analysis(amendment_text: str) -> list[tuple[str, str, str]]:
    """Return ``(object_ref, object_name, reason)`` for each affected section.

    The amendment's "Requirements affected" list mixes a contract *property*
    (``product.vendor_neutral_after_deadline``, the gap being closed) with the
    contract *sections* whose objects need revalidating. Only the section
    bullets name objects, so only those become revalidation records.
    """
    body = markdown.section(amendment_text, "Impact analysis")
    affected: list[tuple[str, str, str]] = []
    for bullet in markdown.bullets(body):
        text = bullet.strip()
        if not text.startswith("§"):
            continue
        head, _, reason = text.partition("—")
        if not reason:
            head, _, reason = text.partition(" - ")
        match = _SECTION_REF.match(head.strip())
        if not match:
            continue
        affected.append((f"§{match.group(1)}", match.group(2).strip(), reason.strip()))
    return affected


def _walking_skeleton_change(amendment_text: str) -> tuple[str, str]:
    """Read the added step and its anchor out of the amendment's own bullet."""
    for object_ref, _, reason in parse_impact_analysis(amendment_text):
        if object_ref != "§14.4":
            continue
        quoted = _BACKTICKED.findall(reason)
        if len(quoted) >= 2:
            return quoted[0].strip(), quoted[1].strip()
    raise AmendmentError("AMENDMENT-001 does not state the walking-skeleton step it adds")


def recompile_walking_skeleton(contract_md: str, amendment_text: str) -> WalkingSkeletonRecompilation:
    """Section 1.3 step 6 for the Section 14.4 trace."""
    before = markdown.arrow_steps(markdown.fenced_block(markdown.section(contract_md, "14.4 Walking skeleton")))
    if not before:
        raise AmendmentError("contract.md Section 14.4 declares no walking-skeleton trace")
    added, anchor = _walking_skeleton_change(amendment_text)
    if anchor not in before:
        raise AmendmentError(f"AMENDMENT-001 anchors {added!r} after {anchor!r}, which is not a Section 14.4 step")
    after = list(before)
    after.insert(after.index(anchor) + 1, added)
    return WalkingSkeletonRecompilation(before=before, after=after, added_step=added, inserted_after=anchor)


def _approval(amendment_text: str) -> tuple[str, str]:
    block = markdown.fenced_block(markdown.section(amendment_text, "Owner approval"))
    values = {}
    for line in block:
        key, _, value = line.partition(":")
        values[key.strip().lower()] = value.strip()
    return values.get("approved by", ""), values.get("date", "")


def superseded_gate_order(project_yaml_text: str) -> list[str]:
    """Gate ids in ``project.yaml -> delivery_priority``, in declared order.

    Read from the raw file because the pack annotates each priority item with
    its gate id in a YAML comment, which the parser discards. The comment is
    where the binding is stated, so the raw text is the authority here.
    """
    order: list[str] = []
    inside = False
    for line in project_yaml_text.splitlines():
        if line.startswith("delivery_priority:"):
            inside = True
            continue
        if inside:
            if not line.startswith((" ", "\t", "-")) and line.strip():
                break
            for gate_id in _GATE_ID.findall(line):
                if gate_id not in order:
                    order.append(gate_id)
    return order


def compile_delivery_priority(decisions_dir: Path, project_yaml_text: str) -> DeliveryPriority:
    """Compile DEC-005 over ``project.yaml -> delivery_priority``."""
    matches = sorted(decisions_dir.glob(DEC_005_GLOB)) if decisions_dir.is_dir() else []
    superseded_gates = superseded_gate_order(project_yaml_text)
    if not matches:
        raise AmendmentError(
            f"DEC-005 not found under {decisions_dir}; the owner priority decision cannot be compiled from memory"
        )
    text = matches[0].read_text()
    items = markdown.numbered_items(markdown.section(text, "Decision"))
    if not items:
        raise AmendmentError(f"{matches[0].name} declares no ordered decision list")
    gate_order: list[str] = []
    for item in items:
        for gate_id in _GATE_ID.findall(item):
            if gate_id not in gate_order:
                gate_order.append(gate_id)
    return DeliveryPriority(
        source=f"docs/decisions/{matches[0].name}",
        superseded_source="project-pack/project.yaml#delivery_priority",
        ordered_items=[re.sub(r"[*`]", "", item).strip() for item in items],
        gate_order=gate_order,
        superseded_gate_order=superseded_gates,
    )


def recompile(
    *,
    pack_root: Path,
    contract_md: str,
    gates: dict[str, dict[str, Any]],
    project_yaml_text: str,
    decisions_dir: Path,
    document_hash: str = "",
) -> AmendmentRecompilation:
    """Run Section 1.3 steps 6 and 7 for AMENDMENT-001 plus DEC-005."""
    amendment_text = _amendment_path(pack_root).read_text()
    approved_by, approved_at = _approval(amendment_text)

    result = AmendmentRecompilation(
        approved_by=approved_by,
        approved_at=approved_at,
        document_hash=document_hash,
        gate_ids=sorted(gates),
        amendment_gate_ids=sorted(g for g, body in gates.items() if str(body.get("contract_version")) == "1.1"),
        blocking_gate_ids=sorted(g for g, body in gates.items() if body.get("blocking")),
        day_1_gate_ids=sorted(g for g, body in gates.items() if body.get("day") == 1),
        walking_skeleton=recompile_walking_skeleton(contract_md, amendment_text),
    )

    if AMENDMENT_GATE not in gates:
        raise AmendmentError(f"{AMENDMENT_GATE} is absent from the recompiled gate set (Section 1.3 step 6)")
    gate = gates[AMENDMENT_GATE]
    if not gate.get("blocking"):
        raise AmendmentError(f"{AMENDMENT_GATE} is not blocking; AMENDMENT-001 requires Day 1, blocking")
    if gate.get("day") != 1:
        raise AmendmentError(f"{AMENDMENT_GATE} is scheduled for day {gate.get('day')}, not Day 1")

    affected = parse_impact_analysis(amendment_text)
    if not affected:
        raise AmendmentError("AMENDMENT-001 impact analysis names no affected contract sections")
    for object_ref, object_name, reason in affected:
        changed = "unchanged" not in reason.lower()
        result.revalidation_records.append(
            RevalidationRecord(
                object_ref=object_ref,
                object_name=object_name,
                changed=changed,
                reason=reason,
                amendment_id=AMENDMENT_ID,
                from_contract_version=result.from_version,
                to_contract_version=result.to_version,
                # Section 19.4 outcomes. A changed object is revalidated against
                # the new version; an unchanged one is reaffirmed, not skipped.
                outcome=str(ContractReviewOutcome.CONTRACT_REAFFIRMED),
                revalidation_action=(
                    "recompile_and_revalidate_dependent_objects" if changed else "reaffirm_without_change"
                ),
            )
        )

    result.delivery_priority = compile_delivery_priority(decisions_dir, project_yaml_text)
    priority = result.delivery_priority
    if AMENDMENT_GATE in priority.gate_order and "GATE-D1-07" in priority.gate_order:
        if priority.gate_order.index(AMENDMENT_GATE) > priority.gate_order.index("GATE-D1-07"):
            raise AmendmentError("DEC-005 requires GATE-D1-10 to precede GATE-D1-07 in the execution order")
    priority.blocking = {
        gate_id: bool(gates.get(gate_id, {}).get("blocking", False)) for gate_id in priority.gate_order
    }
    if not all(priority.blocking.values()):
        raise AmendmentError(
            "DEC-005 is a reorder, not a weakening: every reordered gate must still be blocking, "
            f"got {priority.blocking}"
        )
    return result
