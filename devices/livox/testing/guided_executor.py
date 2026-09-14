from __future__ import annotations

from devices.livox.testing.guided import (
    GuidedDecision,
    GuidedRequest,
    GuidedSubmission,
)
from devices.livox.testing.test_executor import BaseTestExecutor
from devices.livox.testing.test_result import TestOutcome, TestStatus


class GuidedWorkflowExecutor(BaseTestExecutor):
    """Bridge the Qt runner to a UI-agnostic explicit-decision provider."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._provider = None
        self._test_id = None

    def start(self, definition, context) -> None:
        provider = getattr(context, "guided_provider", None)
        if provider is None:
            self.finish(
                TestOutcome(
                    TestStatus.SKIPPED,
                    "Guided test requires an interactive provider; no result was inferred.",
                    {"guided": True, "missing_provider": True},
                )
            )
            return
        self._provider = provider
        self._test_id = definition.id
        request = GuidedRequest(
            test_id=definition.id,
            name=definition.name,
            purpose=definition.purpose,
            precondition=definition.precondition,
            procedure=definition.procedure,
            expected_result=definition.expected_result,
            parameters=definition.parameters,
            timeout_sec=definition.timeout_sec,
        )
        provider.request(request, self._submitted)

    def cancel(self) -> None:
        if self._provider is not None and self._test_id is not None:
            self._provider.cancel(self._test_id)
        super().cancel()

    def _submitted(self, submission: GuidedSubmission) -> None:
        if self.cancelled:
            return
        measurements = dict(submission.measurements)
        measurements.update(
            {
                "guided": True,
                "user_decision": submission.decision.value,
                "user_notes": submission.notes,
                "evidence_references": list(submission.evidence_references),
            }
        )
        if submission.decision is GuidedDecision.CONFIRM:
            outcome = TestOutcome(
                TestStatus.PASS,
                "User explicitly confirmed the guided acceptance criteria.",
                measurements,
            )
        elif submission.decision is GuidedDecision.REJECT:
            outcome = TestOutcome(
                TestStatus.FAIL,
                "User explicitly rejected the guided acceptance criteria.",
                measurements,
            )
        else:
            outcome = TestOutcome(
                TestStatus.SKIPPED,
                "User skipped the guided test; no result was inferred.",
                measurements,
            )
        self.finish(outcome)
