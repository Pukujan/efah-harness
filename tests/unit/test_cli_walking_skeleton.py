"""The CLI is the contract's §6 one-command experience.

`harness project run ./project-pack --mode autonomous` must reach a typed §6.2
state, and must not report an absent dependency as success.
"""
from __future__ import annotations

import inspect

from cli import main as cli
from governance.states import ProjectState


def test_the_cli_no_longer_guesses_lane_entry_points():
    """It calls the composition root rather than searching for one.

    While the lanes were being built in parallel their public names were not
    fixed, so the CLI resolved `module:attribute` across candidate paths. They
    are fixed now, and a name-guessing resolver that silently reports
    UNAVAILABLE when a rename happens is worse than an import error.
    """
    source = inspect.getsource(cli)
    assert "TERMINUS_IMPORTERS" not in source
    assert "LANGGRAPH_RUNNERS" not in source
    assert "from composition.root import" in source


def test_an_unavailable_lane_is_not_reported_as_ok():
    """A station whose dependency is absent must surface, not read as fine."""
    src = inspect.getsource(cli._walking_skeleton)
    assert "station.status is StationStatus.EXERCISED" in src


def test_exit_codes_cover_every_terminal_state():
    """§6.2's states each need a distinct exit code or a caller cannot branch."""
    for state in ProjectState:
        assert state in cli.EXIT_CODES, f"{state} has no exit code"
    codes = [cli.EXIT_CODES[s] for s in ProjectState]
    assert len(set(codes)) == len(codes), "two states share an exit code"


def test_verified_complete_is_the_only_zero_exit():
    assert cli.EXIT_CODES[ProjectState.VERIFIED_COMPLETE] == 0
    for state in ProjectState:
        if state is not ProjectState.VERIFIED_COMPLETE:
            assert cli.EXIT_CODES[state] != 0, f"{state} must not exit 0"
