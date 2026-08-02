"""EFAH module: dashboard. Contract EFAH-CONTRACT-001 v1.1 Sections 5.1, 11.6.

Read projections only. This package contains no write path by construction --
see :mod:`dashboard.source`.
"""

from dashboard.projections import build_projection, derived_durations, project_from_source
from dashboard.redaction import ProtectedContentLeak, assert_no_protected_content
from dashboard.source import MutationAttemptedFromDashboard, ReadOnlySource
from dashboard.views import REQUIRED_VIEWS, DashboardProjection

__all__ = [
    "REQUIRED_VIEWS",
    "DashboardProjection",
    "MutationAttemptedFromDashboard",
    "ProtectedContentLeak",
    "ReadOnlySource",
    "assert_no_protected_content",
    "build_projection",
    "derived_durations",
    "project_from_source",
]
