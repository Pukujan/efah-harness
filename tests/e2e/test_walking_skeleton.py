"""The real §14.4 walking skeleton, against the real services.

Opt-in: `EFAH_LIVE_TESTS=1 pytest tests/e2e -q`. It writes a real TerminusDB
branch and a real checkpoint, so it does not belong in the default suite.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from composition.root import HarnessConfig, StationStatus, run_walking_skeleton

pytestmark = pytest.mark.skipif(
    os.environ.get("EFAH_LIVE_TESTS") != "1", reason="live services required"
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
async def run():
    return await run_walking_skeleton(HarnessConfig(pack_root=REPO / "project-pack"))


async def test_no_station_is_a_placeholder(run):
    """§14.4: any placeholder or unwired component fails the phase."""
    assert run.failed == [], [f"{s.name}: {s.detail}" for s in run.failed]


async def test_composition_is_complete(run):
    assert run.composition_findings == []


async def test_the_import_went_to_an_isolated_branch(run):
    """GATE-D1-01 A1: new branch present, main head unchanged."""
    station = next(s for s in run.stations if s.name == "TerminusDB commit")
    assert station.status is StationStatus.EXERCISED
    assert station.evidence["main_head_unchanged"] is True
    assert station.evidence["new_branches"]
    assert station.evidence["branch"] != "main"


async def test_the_model_route_carries_an_alias_and_no_real_identity(run):
    """§12.3: agents see aliases only."""
    station = next(s for s in run.stations if s.name == "model alias routing")
    assert station.evidence["real_identity_fields_exposed"] == []
    assert station.evidence["alias"].startswith("implementer-")


async def test_a_vendor_neutral_worker_adapter_is_present(run):
    """GATE-D1-07 A5, exercised rather than asserted."""
    station = next(s for s in run.stations if s.name == "fresh worker session")
    assert station.evidence["vendor_neutral"]


async def test_the_owner_surface_is_reachable_and_vendor_neutral(run):
    station = next(s for s in run.stations if s.name == "owner control surface")
    assert station.status is StationStatus.EXERCISED
    assert station.evidence["gateway_class"] == "production"


async def test_the_protected_verifier_is_not_faked(run):
    """The sealed lane must report UNAVAILABLE, never a fabricated PASS."""
    station = next(s for s in run.stations if s.name == "protected verifier call")
    assert station.status is StationStatus.UNAVAILABLE
