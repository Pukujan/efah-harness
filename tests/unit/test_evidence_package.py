"""Contract §27 final evidence package (GATE-D3-26).

These tests exist to stop the package from becoming reassuring. §18 says
"'Done' without named evidence is invalid", so the properties worth protecting
are the unflattering ones: an unmeasured field must stay visible, an unmeasured
field must not borrow an evidence tier, a stale test run must not be reused, and
the package must never assert VERIFIED_COMPLETE on its own authority.

The count assertions are deliberately loose about *which* fields are missing on
any given host — that changes with what has been built and run. What is pinned
is the behaviour: gaps are named, tiers are earned, status is measured.
"""

from __future__ import annotations

import json

import pytest

from evidence.package import (
    FIELD_KEYS,
    UNAVAILABLE,
    EvidencePackage,
    PackageField,
    Tier,
    build,
    deferred_non_goals,
    honest_debt,
    render_text,
    section_27_lines,
)
from governance.states import ProjectState


@pytest.fixture(scope="module")
def package() -> EvidencePackage:
    return build()


# -- the field list comes from the contract -------------------------------
def test_the_field_list_is_read_from_the_contract_not_transcribed():
    lines = section_27_lines()
    assert lines, "§27's fenced block did not parse"
    assert lines[0].startswith("Project status")
    assert [line for line, _ in FIELD_KEYS] == lines


def test_the_package_covers_every_section_27_line(package):
    assert [f.contract_line for f in package.fields] == section_27_lines()


def test_the_field_count_discrepancy_with_the_gate_is_recorded_not_resolved(package):
    """GATE-D3-26 A1 says "twenty-three"; §27 lists 22. Report it, don't pick."""
    discrepancy = package.field_count_discrepancy
    assert discrepancy["contract_section_27_lines"] == 22
    assert discrepancy["gate_d3_26_a1_claim_text"] == "twenty-three"
    assert "assertion hash" in discrepancy["resolution"]


# -- gaps stay visible ----------------------------------------------------
def test_an_unmeasured_field_is_not_present():
    field = PackageField("k", "line", UNAVAILABLE, "nothing measured it", Tier.NOT_MEASURED)
    assert field.present is False


def test_an_unmeasured_field_cannot_borrow_an_evidence_tier(package):
    """A gap wearing DETERMINISTIC_ORACLE is worse than a gap."""
    for field in package.fields:
        if not field.present:
            assert field.tier is Tier.NOT_MEASURED, f"{field.key} claims {field.tier}"


def test_every_field_names_a_source(package):
    """§18: "Done" without named evidence is invalid."""
    for field in package.fields:
        assert field.source, f"{field.key} names no source"


def test_a_measured_zero_is_present_not_missing():
    """"Zero scope-drift findings" is a result, not an absence."""
    assert PackageField("k", "l", 0, "counted").present is True
    assert PackageField("k", "l", False, "checked").present is True


def test_an_empty_container_is_not_a_measurement():
    assert PackageField("k", "l", {}, "s").present is False
    assert PackageField("k", "l", [], "s").present is False


def test_missing_fields_are_listed_in_the_body(package):
    body = package.as_body()
    assert body["fields_missing"] == package.missing
    assert body["fields_present"] == len(package.fields) - len(package.missing)
    assert body["package_complete"] is (not package.missing)


# -- status is measured, never asserted -----------------------------------
def test_the_package_does_not_assert_verified_complete(package):
    """§6.2 makes VERIFIED_COMPLETE the only success terminal.

    It is the gate run's to decide. A package that declared it in order to
    satisfy the gate that checks for it would be the "mostly done" report §6.2
    forbids.
    """
    status_field = next(f for f in package.fields if f.key == "project_status")
    assert "gate_runner" in status_field.source or status_field.tier is Tier.NOT_MEASURED
    if status_field.present:
        assert package.status in {s.value for s in ProjectState}


def test_status_today_is_not_verified_complete(package):
    """Guards the specific claim: holdouts are blocked, so completion is not.

    If this ever fails, either FINDING-005 was answered and holdouts were minted,
    or something started reporting success it did not earn. Both deserve a look.
    """
    assert package.status != ProjectState.VERIFIED_COMPLETE.value


# -- honest debt ----------------------------------------------------------
def test_every_debt_entry_carries_a_measurement_or_a_status():
    """A debt entry with neither is a sentence, not evidence."""
    for entry in honest_debt():
        assert entry.get("measured") or entry.get("status"), entry["id"]
        assert entry["detail"], entry["id"]


def test_debt_names_the_isolation_limit_and_the_transport_problem():
    ids = {d["id"]: d for d in honest_debt()}
    assert any("sudo" in d["detail"] for d in ids.values())
    assert any("resold" in d["detail"] or "kiro" in d["detail"].lower() for d in ids.values())


def test_deferred_non_goals_are_stated():
    assert len(deferred_non_goals()) >= 8


def test_the_debt_field_is_in_the_package(package):
    field = next(f for f in package.fields if f.key == "honest_debt_and_deferred_non_goals")
    assert field.present
    assert field.value["honest_debt"]
    assert field.value["deferred_non_goals"]


# -- blinding (§12.3) -----------------------------------------------------
def test_the_package_carries_aliases_and_no_real_model_identity(package):
    from models.policy import load_model_policy

    serialized = json.dumps(package.as_body(), default=str)
    policy = load_model_policy()
    for role, row in policy.roles.items():
        assert row.litellm_model not in serialized, f"{role} leaked its real model id"
        assert row.alias in serialized, f"{role} alias is absent"


def test_the_alias_field_records_that_family_is_only_a_label(package):
    field = next(f for f in package.fields if f.key == "model_aliases_and_audit_references")
    assert "FINDING-005" in field.note


# -- the hidden holdout field tells the truth -----------------------------
def test_the_hidden_holdout_field_reports_unverifiable_not_a_soft_pass(package):
    field = next(f for f in package.fields if f.key == "hidden_holdout_result")
    assert field.present
    assert field.value["verdict"] == "UNVERIFIABLE"
    assert field.value["holdout_content_present_on_build_side"] is False
    assert "not a soft pass" in field.note


def test_the_auto_merged_pr_field_explains_why_it_is_absent(package):
    field = next(f for f in package.fields if f.key == "auto_merged_pr_reference")
    assert not field.present
    assert "hidden_holdout" in field.note


# -- rendering ------------------------------------------------------------
def test_the_text_rendering_marks_missing_fields(package):
    text = render_text(package)
    for key in package.missing:
        line = next(f.contract_line for f in package.fields if f.key == key)
        assert f"! {line}" in text
    assert "not a claim of completion" in text


def test_the_package_is_a_compiled_object_with_an_envelope(package):
    obj = package.to_compiled_object()
    dumped = obj.model_dump(mode="json")
    assert dumped["envelope"]["schema_id"] == "efah.evidence_package"
    assert dumped["envelope"]["content_hash"].startswith("sha256:")
