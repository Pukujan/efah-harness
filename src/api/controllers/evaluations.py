"""Evaluation use cases (contract Sections 11.5, 17.2).

The controller reports *status*. It does not run an evaluation, score one, or
hold evaluator logic -- Section 11.5 forbids hidden evaluator logic in a
controller, and Section 17.2 puts the holdout internals on the sealed side
where this process cannot reach them at all.
"""

from __future__ import annotations

from api.errors import NotFound
from api.ports import ControlPlaneReadPort
from dashboard.redaction import assert_no_protected_content
from dashboard.views import EvaluationStatusRow


class EvaluationController:
    """``GET /evaluations/{id}``."""

    def __init__(self, *, reader: ControlPlaneReadPort) -> None:
        self._reader = reader

    def get(self, *, evaluation_id: str) -> EvaluationStatusRow:
        evaluation = self._reader.get_evaluation(evaluation_id)
        if evaluation is None:
            raise NotFound(f"evaluation {evaluation_id} is not recorded")
        row = EvaluationStatusRow(
            evaluation_id=evaluation.evaluation_id,
            task_id=evaluation.task_id,
            visible_verdict=evaluation.visible_verdict,
            visible_passed=evaluation.visible_passed,
            visible_total=evaluation.visible_total,
            hidden_suite_name=evaluation.hidden_suite_name,
            hidden_suite_verdict=evaluation.hidden_suite_verdict,
            hidden_assertions_total=evaluation.hidden_assertions_total,
            hidden_assertions_failed=evaluation.hidden_assertions_failed,
            oracle_version=evaluation.oracle_version,
            failure_class=evaluation.failure_class,
        )
        # Cheap, and the one place an upstream mistake would become public.
        assert_no_protected_content(row.model_dump(mode="json"), where="evaluation_status")
        return row
