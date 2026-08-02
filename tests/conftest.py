"""Shared pytest configuration.

The only thing declared here is the boundary between tests that are free and
tests that spend money. Every test in the suite is deterministic and offline
except those marked ``live``, which perform real model generations through the
harness against the eval gateway.

Those are opt-in rather than opt-out on purpose. ``request_policy`` in
``model-policy.yaml`` records an account-wide 100 req/min ceiling shared by every
process on this host, and ``unthrottled_fanout: forbidden`` — so a live test
cannot be made faster by parallelising it, and a suite that ran them by default
would turn an ordinary ``pytest`` into a slow, billed operation that also
competes with whatever else is dispatching.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests that perform real, billed model generations",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: performs a real, billed model generation")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="needs --run-live (performs a billed generation)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
