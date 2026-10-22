"""Structured Agent outputs for future shared workflow state."""

from workflow_studio.state.design import DesignOutput, ModuleDesign
from workflow_studio.state.requirements import AcceptanceCriterion, FunctionalRequirement, RequirementsOutput, RequirementsOutputV2

from workflow_studio.state.coding import CodeFile, CodingOutput

__all__ = ["RequirementsOutput", "DesignOutput", "ModuleDesign", "CodeFile", "CodingOutput"]
__all__ += ["RequirementsOutputV2", "FunctionalRequirement", "AcceptanceCriterion"]

from workflow_studio.state.workflow import (
    WorkflowEvent, WorkflowState, WorkflowStatus, WorkflowUpdate,
    create_initial_state, failure_update, merge_events, validate_state, validate_update,
)

__all__ += [
    "WorkflowEvent", "WorkflowState", "WorkflowStatus", "WorkflowUpdate",
    "create_initial_state", "failure_update", "merge_events", "validate_state", "validate_update",
]

from workflow_studio.state.result import WorkflowResult

__all__ += ["WorkflowResult"]

from workflow_studio.state.review import ReviewAssessmentV2, ReviewIssue, ReviewOutput, ReviewRecord, ReviewStatus

__all__ += ["ReviewOutput", "ReviewRecord", "ReviewStatus"]
__all__ += ["ReviewIssue", "ReviewAssessmentV2"]

from workflow_studio.budget import RunBudget, BudgetSnapshot

__all__ += ["RunBudget", "BudgetSnapshot"]
