"""Terminal public workflow result, with typed Agent outputs."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from workflow_studio.budget import BudgetSnapshot

from workflow_studio.state.review import MAX_REVISIONS, ReviewOutput, ReviewRecord, ReviewStatus, validate_review_context
from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import ClarificationAnswer, RequirementsOutput
from workflow_studio.state.workflow import WorkflowEvent, WorkflowState, WorkflowStatus, validate_state
from workflow_studio.state.validation import validate_design_requirements
from workflow_studio.state.patch import validate_repair_receipts


class WorkflowResult(BaseModel):
    """A terminal outcome; reaching LangGraph END alone never implies success."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: str
    status: WorkflowStatus
    requirements: RequirementsOutput | None
    design: DesignOutput | None
    code: CodingOutput | None
    events: list[WorkflowEvent]
    error: str | None
    budget: BudgetSnapshot | None = None
    clarifications: list[ClarificationAnswer] = Field(default_factory=list, max_length=48)
    review: ReviewOutput | None = None
    review_enabled: bool = Field(default=False, strict=True)
    revision_count: int = Field(default=0, ge=0, le=MAX_REVISIONS, strict=True)
    max_revisions: int = Field(default=1, ge=0, le=MAX_REVISIONS, strict=True)
    review_status: ReviewStatus = ReviewStatus.DISABLED
    review_history: list[ReviewRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def terminal_outcome(self) -> Self:
        if self.status not in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES, WorkflowStatus.FAILED, WorkflowStatus.STOPPED):
            raise ValueError("WorkflowResult must be terminal.")
        if self.status in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES):
            if self.requirements is None or self.design is None or self.code is None or self.error is not None:
                raise ValueError("Completed result requires all outputs and no error.")
        elif self.status == WorkflowStatus.FAILED and (self.error is None or not self.error.strip()):
            raise ValueError("Failed result requires an error.")
        if self.status == WorkflowStatus.STOPPED and self.error is not None:
            raise ValueError("Cooperative stop is not an execution error.")
        if (self.status == WorkflowStatus.STOPPED) != (self.budget is not None and self.budget.stop_reason is not None):
            raise ValueError("Stopped result requires a budget stop reason.")
        if self.budget is not None and self.budget.in_flight_calls:
            raise ValueError("Terminal result cannot have unsettled model calls.")
        if self.design is not None:
            if self.requirements is None:
                raise ValueError("Design requires requirements.")
            validate_design_requirements(self.design, self.requirements)
        if self.code is not None and (self.requirements is None or self.design is None):
            raise ValueError("Code requires preceding outputs.")
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("Result event belongs to another run.")
        validate_review_context(
            self.run_id, self.status, self.review_enabled, self.revision_count, self.max_revisions,
            self.review_status, self.review, self.review_history, self.code, self.requirements, self.design,
        )
        validate_repair_receipts(self.events, self.code.model_dump() if self.code else None, self.review_history, self.revision_count,
                                 self.requirements.model_dump() if self.requirements else None, self.design.model_dump() if self.design else None)
        return self

    @classmethod
    def from_state(cls, state: WorkflowState) -> Self:
        validated = validate_state(state)
        return cls.model_validate({key: validated[key] for key in cls.model_fields})
