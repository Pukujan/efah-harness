"""Controllers (contract Section 11.5).

> Controllers translate commands into application use cases. They MUST NOT
> contain persistence-specific code, model prompts, or hidden evaluator logic.

Every controller in this package talks to :mod:`api.ports` and returns a model
from :mod:`api.state` or :mod:`dashboard.views`. There is no SQL, no WOQL, no
HTTP client, no prompt string, and no evaluator here -- an architecture test in
``tests/unit/test_api_controllers.py`` asserts that mechanically with an AST
scan, so the rule survives the next person in a hurry.
"""

from api.controllers.contracts import ContractController
from api.controllers.dependencies import DependencyController
from api.controllers.evaluations import EvaluationController
from api.controllers.projects import ProjectController
from api.controllers.tasks import TaskController

__all__ = [
    "ContractController",
    "DependencyController",
    "EvaluationController",
    "ProjectController",
    "TaskController",
]
