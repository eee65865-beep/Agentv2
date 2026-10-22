"""Pure conditional routes; decisions depend on validated State, not UI."""

from typing import Literal

from workflow_studio.state.review import ReviewStatus
from workflow_studio.state.workflow import WorkflowState, WorkflowStateError, WorkflowStatus, validate_state


def route_after_coding(state: WorkflowState) -> Literal["review_agent", "__end__"]:
    current = validate_state(state)
    if current["status"] == WorkflowStatus.FAILED:
        raise WorkflowStateError("Exceptional failure cannot be routed as a verdict.")
    if current["status"] == WorkflowStatus.STOPPED:
        return "__end__"
    return "review_agent" if current["review_enabled"] else "__end__"


def route_after_review(state: WorkflowState) -> Literal["coding_agent", "__end__"]:
    current = validate_state(state)
    if current["status"] in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES, WorkflowStatus.STOPPED):
        return "__end__"
    if current["review_status"] == ReviewStatus.NEEDS_REVISION and current["revision_count"] < current["max_revisions"]:
        return "coding_agent"
    raise WorkflowStateError("Review did not produce a valid routing outcome.")
