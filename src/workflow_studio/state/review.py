"""Model review is advisory; it does not represent executed tests."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Self
from uuid import uuid4, UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from workflow_studio.state.coding import CodingOutput

MAX_REVISIONS = 3
MAX_REVIEW_BYTES = 32768


class ReviewStatus(StrEnum):
    DISABLED = "disabled"
    PENDING = "pending"
    NEEDS_REVISION = "needs_revision"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    passed: bool = Field(strict=True, description="LLM 静态审查判断，不表示真实测试通过。")
    score: int = Field(ge=0, le=100, strict=True, description="模型给出的参考分数，范围 0–100。")
    issues: list[str] = Field(max_length=64, description="发现的需求、设计、语法、逻辑、说明或文件结构问题。")
    suggestions: list[str] = Field(max_length=64, description="具体修复建议，不改变已确认需求约束。")
    summary: str = Field(min_length=1, max_length=4000, description="审查总结及静态检查局限；未执行测试。")

    @field_validator("issues", "suggestions")
    @classmethod
    def nonblank_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("invalid_review_item")
        return values

    @model_validator(mode="after")
    def useful_feedback(self) -> Self:
        if not self.summary.strip() or (not self.passed and not self.issues):
            raise ValueError("failed_review_requires_actionable_issues")
        return self


class ReviewRecord(BaseModel):
    """Business history retains the exact reviewed code, separate from event logs."""
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    revision_count: int = Field(ge=0, le=MAX_REVISIONS, strict=True)
    reviewed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    review: ReviewOutput
    code: CodingOutput

    @field_validator("record_id", "run_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical_uuid")
        return value


def merge_review_history(existing: list[ReviewRecord], incoming: list[ReviewRecord]) -> list[ReviewRecord]:
    result: list[ReviewRecord] = []
    by_id: dict[str, ReviewRecord] = {}
    by_revision: set[tuple[str, int]] = set()
    for source in (existing, incoming):
        if not isinstance(source, list):
            raise ValueError("review_history_must_be_list")
        for raw in source:
            entry = ReviewRecord.model_validate(raw.model_dump() if isinstance(raw, ReviewRecord) else raw)
            previous = by_id.get(entry.record_id)
            if previous is not None:
                if previous != entry:
                    raise ValueError("review_history_record_conflict")
                continue
            key = (entry.run_id, entry.revision_count)
            if key in by_revision:
                raise ValueError("duplicate_review_revision")
            by_revision.add(key)
            by_id[entry.record_id] = entry
            result.append(entry)
    return result


def validate_review_context(
    run_id: str, status: str, enabled: bool, revision_count: int, max_revisions: int,
    review_status: ReviewStatus, review: ReviewOutput | None, history: list[ReviewRecord], code: CodingOutput | None,
) -> None:
    """Shared invariants for full State and terminal public Result."""
    if revision_count > max_revisions:
        raise ValueError("revision_budget_exceeded")
    if not enabled:
        if review is not None or history or revision_count or review_status != ReviewStatus.DISABLED or status == "completed_with_issues":
            raise ValueError("standard_mode_cannot_have_review_data")
        return
    if review_status == ReviewStatus.DISABLED:
        raise ValueError("enabled_review_cannot_be_disabled")
    if [item.revision_count for item in history] != list(range(len(history))):
        raise ValueError("review_history_must_be_consecutive")
    if any(item.run_id != run_id for item in history):
        raise ValueError("review_history_run_mismatch")
    if history:
        if review is None or history[-1].review != review or not history[-1].revision_count <= revision_count <= history[-1].revision_count + 1:
            raise ValueError("latest_review_or_revision_mismatch")
    elif review is not None or revision_count:
        raise ValueError("review_history_missing")
    if review is not None and code is None:
        raise ValueError("review_requires_code")
    if review_status in (ReviewStatus.PASSED, ReviewStatus.NEEDS_REVISION, ReviewStatus.FAILED):
        if review is None or history[-1].revision_count != revision_count or history[-1].code != code:
            raise ValueError("review_must_describe_current_code")
        if (review_status == ReviewStatus.PASSED) != review.passed:
            raise ValueError("review_status_does_not_match_verdict")
    if review_status == ReviewStatus.NEEDS_REVISION and (revision_count >= max_revisions or status not in ("running", "failed")):
        raise ValueError("no_revision_budget_remaining")
    if review_status == ReviewStatus.FAILED and revision_count != max_revisions:
        raise ValueError("review_failed_before_budget_exhausted")
    if review_status == ReviewStatus.ERROR and status != "failed":
        raise ValueError("review_error_requires_failed_workflow")
    if status == "completed" and review_status != ReviewStatus.PASSED:
        raise ValueError("review_mode_completion_requires_passed_review")
    if status == "completed_with_issues" and review_status != ReviewStatus.FAILED:
        raise ValueError("issues_terminal_requires_failed_review")
    if review_status == ReviewStatus.FAILED and status not in ("completed_with_issues", "failed"):
        raise ValueError("exhausted_review_requires_terminal_state")
