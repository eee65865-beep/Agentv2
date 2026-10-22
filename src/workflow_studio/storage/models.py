"""Versioned manifests and validated historical results."""

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.review import MAX_REVISIONS, ReviewOutput, ReviewRecord, ReviewStatus
from workflow_studio.state.workflow import WorkflowStatus

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_RUN_BYTES = 8 * 1024 * 1024
DATA_FILES = {"requirements.json", "design.json", "code.json", "events.json"}


class RunMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    run_id: str
    status: WorkflowStatus
    saved_at: AwareDatetime
    started_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    user_request: str = Field(min_length=1, max_length=131072)
    model_name: str = Field(max_length=256)
    error: str | None
    review_enabled: bool = Field(strict=True)
    revision_count: int = Field(ge=0, le=MAX_REVISIONS, strict=True)
    max_revisions: int = Field(ge=0, le=MAX_REVISIONS, strict=True)
    review_status: ReviewStatus
    sha256: dict[str, str]

    @model_validator(mode="after")
    def valid_manifest(self) -> Self:
        expected = DATA_FILES | ({"review.json"} if self.review_enabled else set())
        if set(self.sha256) != expected or any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in self.sha256.values()):
            raise ValueError("invalid_manifest")
        if self.status not in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES, WorkflowStatus.FAILED):
            raise ValueError("only_terminal_results_are_persisted")
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("invalid_time_range")
        return self


class ReviewArchive(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    latest: ReviewOutput | None
    history: list[ReviewRecord]


@dataclass(frozen=True)
class SavedRun:
    metadata: RunMetadata
    result: WorkflowResult


@dataclass(frozen=True)
class HistoryProblem:
    run_id: str
    category: str


@dataclass(frozen=True)
class RunListing:
    entries: tuple[RunMetadata, ...]
    problems: tuple[HistoryProblem, ...]
