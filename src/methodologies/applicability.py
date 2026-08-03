"""Applicability compiler -- required methodologies by task class and risk.

Contract Section 13.3: "The contract compiler MUST select required
methodologies from task class and risk. Agents shall not manually decide which
methods 'feel relevant.'"

GATE-D1-03 A5 checks exactly that: every task must carry
``methodology_source == applicability_compiler``. The constant below is the only
value this module ever writes into that field, and nothing else in the codebase
writes the field at all -- so an agent-chosen methodology set cannot masquerade
as a compiled one.

The catalog and the applicability table are read from
``project-pack/methodology-policy.yaml``. This module contains no methodology
list of its own; a method that is not in the owner's catalog cannot be selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from integrations.pack import ProjectPack

#: GATE-D1-03 A5 expects this exact provenance string.
METHODOLOGY_SOURCE = "applicability_compiler"

#: Section 13.3 conditional keys. Closed: a condition outside this set is a
#: policy error, not a silently ignored line.
CONDITION_KEYS = ("external_research", "disputed_design", "tunable_selection", "new_dependency", "competing_causes")


class MethodologyPolicyError(ValueError):
    """The methodology policy cannot serve a requested task class."""


@dataclass(frozen=True)
class MethodologySelection:
    task_id: str
    task_class: str
    risk: str
    required: tuple[str, ...]
    conditional: dict[str, tuple[str, ...]] = field(default_factory=dict)
    methodology_source: str = METHODOLOGY_SOURCE
    catalog_version: str = ""

    @property
    def methodology_ids(self) -> list[str]:
        return list(self.required)

    def as_body(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_class": self.task_class,
            "risk": self.risk,
            "methodology_ids": list(self.required),
            "conditional_methodologies": {k: list(v) for k, v in sorted(self.conditional.items())},
            "methodology_source": self.methodology_source,
            "methodology_catalog_version": self.catalog_version,
        }


class ApplicabilityCompiler:
    """Deterministic Section 13.3 selector over the owner's catalog."""

    def __init__(self, pack: ProjectPack) -> None:
        policy = pack.yaml("methodology-policy.yaml")
        self.catalog_version = str(policy.get("methodology_catalog_version", ""))
        self.catalog: dict[str, dict[str, Any]] = {
            entry["id"]: entry for entry in policy.get("catalog", []) if isinstance(entry, dict)
        }
        self.rules: list[dict[str, Any]] = [r for r in policy.get("applicability", []) if isinstance(r, dict)]
        self.defaults: dict[str, Any] = dict(policy.get("defaults", {}))
        if not self.catalog:
            raise MethodologyPolicyError("methodology-policy.yaml declares an empty catalog")
        if not self.rules:
            raise MethodologyPolicyError("methodology-policy.yaml declares no applicability rules")
        self._validate_catalog_references()

    @property
    def task_classes(self) -> list[str]:
        return sorted({str(rule["task_class"]) for rule in self.rules})

    def _validate_catalog_references(self) -> None:
        unknown: list[str] = []
        for rule in self.rules:
            for method_id in list(rule.get("required", [])):
                if method_id not in self.catalog:
                    unknown.append(f"{rule.get('task_class')}.required:{method_id}")
            for condition, methods in (rule.get("conditional") or {}).items():
                if condition not in CONDITION_KEYS:
                    unknown.append(f"{rule.get('task_class')}.conditional:{condition}")
                for method_id in methods or []:
                    if method_id not in self.catalog:
                        unknown.append(f"{rule.get('task_class')}.conditional.{condition}:{method_id}")
        if unknown:
            raise MethodologyPolicyError(f"applicability references methods outside the catalog: {sorted(unknown)}")

    def _match(self, task_class: str, risk: str) -> dict[str, Any]:
        exact = [r for r in self.rules if r["task_class"] == task_class and str(r.get("risk")) == risk]
        if exact:
            return exact[0]
        wildcard = [r for r in self.rules if r["task_class"] == task_class and str(r.get("risk")) == "any"]
        if wildcard:
            return wildcard[0]
        same_class = [r for r in self.rules if r["task_class"] == task_class]
        if same_class:
            return same_class[0]
        raise MethodologyPolicyError(
            f"no applicability rule for task_class={task_class!r}; "
            f"the catalog offers {self.task_classes}. Section 8.1 forbids a silent default here."
        )

    def select(self, *, task_id: str, task_class: str, risk: str) -> MethodologySelection:
        rule = self._match(task_class, risk)
        conditional = {
            str(condition): tuple(methods or [])
            for condition, methods in sorted((rule.get("conditional") or {}).items())
        }
        return MethodologySelection(
            task_id=task_id,
            task_class=task_class,
            risk=risk,
            required=tuple(rule.get("required", [])),
            conditional=conditional,
            catalog_version=self.catalog_version,
        )

    def catalog_body(self) -> dict[str, Any]:
        return {
            "methodology_catalog_version": self.catalog_version,
            "methodology_count": len(self.catalog),
            "methodologies": [self.catalog[k] for k in sorted(self.catalog)],
            "task_classes": self.task_classes,
            "defaults": self.defaults,
            "methodology_source": METHODOLOGY_SOURCE,
        }
