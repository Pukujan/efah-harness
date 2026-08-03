"""EFAH module: projects. Contract EFAH-CONTRACT-001 v1.1 Sections 6.1, 6.2, 9.

Project records in the authoritative graph, plus the Section 6.2 terminal-state
rule.
"""

from projects.repository import ProjectRepository, ProjectSummary, TerminalStateViolation

__all__ = ["ProjectRepository", "ProjectSummary", "TerminalStateViolation"]
