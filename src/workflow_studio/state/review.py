"""Model review is advisory; it does not represent executed tests."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import uuid4, UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import AcceptanceId, RequirementId, RequirementsOutput
from workflow_studio.exporters.paths import validate_export_path

MAX_REVISIONS = 3
MAX_REVIEW_BYTES = 32768
REVIEW_POLICY_VERSION = "static-review-v1"
ReviewText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReviewIssue(BaseModel):
    """Model observations; all references are checked against trusted inputs."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    issue_id: Annotated[str, StringConstraints(pattern=r"^RV-[1-9][0-9]*$")] = Field(description="当轮唯一问题编号，如 RV-1；不表示已实现跨轮问题追踪。")
    category: Literal["requirement", "design", "implementation", "test", "documentation"] = Field(description="问题归属类别。")
    severity: Literal["blocker", "major", "minor", "suggestion"] = Field(description="blocker/major 阻止静态门禁通过；minor/suggestion 可保留。")
    message: ReviewText = Field(description="简短、可行动的问题说明。")
    file: str | None = Field(description="实际被审查文件的精确相对路径；缺失文件或位置未知时为 null。")
    line_start: int | None = Field(ge=1, strict=True, description="实际文件起始行，未知为 null；不得编造。")
    line_end: int | None = Field(ge=1, strict=True, description="实际文件结束行，未知为 null；不得编造。")
    evidence_quote: ReviewText | None = Field(description="实际代码连续片段，未知为 null；提供行范围时须位于范围内；不是执行证据。")
    requirement_ids: list[RequirementId] = Field(max_length=64, description="相关持久 FR 编号，无确定关联时为空。")
    acceptance_ids: list[AcceptanceId] = Field(max_length=64, description="相关持久 AC 编号；旧 v1 需求没有 AC 目录时必须为空。")
    expected: ReviewText = Field(description="依据需求或设计期望的行为。")
    observed: ReviewText = Field(description="从代码静态观察到的差异，禁止伪称实际测试结果。")
    suggested_owner: Literal["coding", "design", "requirements"] = Field(description="建议处理角色，程序会按类别和策略复核。")

    @field_validator("file")
    @classmethod
    def safe_file(cls, value: str | None) -> str | None:
        return validate_export_path(value) if value is not None else None

    @model_validator(mode="after")
    def valid_location(self) -> Self:
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("review_line_range_requires_both_ends")
        if self.line_start is not None and (self.file is None or self.line_end < self.line_start):
            raise ValueError("invalid_review_line_range")
        if self.evidence_quote is not None and self.file is None:
            raise ValueError("review_quote_requires_file")
        for refs in (self.requirement_ids, self.acceptance_ids):
            if len(refs) != len(set(refs)):
                raise ValueError("duplicate_review_reference")
        return self


class ReviewAssessmentV2(BaseModel):
    """Generation-only schema: no model-controlled hashes or policy decisions."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    contract_version: Literal[2] = Field(description="审查子契约版本，必须为整数 2。")
    passed: bool = Field(description="模型的静态意见；程序门禁可以否决，不表示测试通过。")
    score: int = Field(ge=0, le=100, description="展示用参考分数，不控制门禁。")
    issue_details: list[ReviewIssue] = Field(max_length=64, description="结构化问题列表，没有问题时为空。")
    suggestions: list[ReviewText] = Field(max_length=64, description="可操作的建议，不修改已确认需求。")
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)] = Field(description="静态审查总结，必须说明没有执行测试。")

    @field_validator("contract_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("review_contract_version_must_be_integer")
        return value

    @model_validator(mode="after")
    def valid_issues(self) -> Self:
        if len({item.issue_id for item in self.issue_details}) != len(self.issue_details):
            raise ValueError("duplicate_review_issue_id")
        if not self.passed and not self.issue_details:
            raise ValueError("failed_review_requires_actionable_issues")
        return self


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
    contract_version: Literal[1, 2] = Field(default=1, description="无字段的旧审查按 v1 读取；新生成经适配器绑定为 v2。")
    issue_details: list[ReviewIssue] = Field(default_factory=list, max_length=64, description="v2 结构化问题；旧记录为空，不补造严重程度。")
    artifact_digest: Digest | None = Field(default=None, description="程序计算的当轮 CodingOutput 摘要，非模型提供。")
    context_digest: Digest | None = Field(default=None, description="程序计算的需求、设计和代码整体摘要。")
    policy_version: Literal["static-review-v1"] | None = Field(default=None, description="适用的确定性静态门禁版本。")

    @field_validator("contract_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("review_contract_version_must_be_integer")
        return value

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
        if self.contract_version == 1:
            if self.issue_details or any(value is not None for value in (self.artifact_digest, self.context_digest, self.policy_version)):
                raise ValueError("legacy_review_cannot_claim_v2_evidence")
        else:
            if self.artifact_digest is None or self.context_digest is None or self.policy_version is None:
                raise ValueError("v2_review_requires_trusted_binding")
            assessment = ReviewAssessmentV2.model_validate({
                key: getattr(self, key) for key in ReviewAssessmentV2.model_fields
            })
            if self.issues != [item.message for item in assessment.issue_details]:
                raise ValueError("review_issues_must_match_details")
            if len(self.model_dump_json().encode("utf-8")) > MAX_REVIEW_BYTES:
                raise ValueError("review_output_too_large")
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

    @model_validator(mode="after")
    def code_binding(self) -> Self:
        from workflow_studio.state.review_policy import validate_review_code_binding
        validate_review_code_binding(self.review, self.code)
        return self

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
    requirements: RequirementsOutput | None = None, design: DesignOutput | None = None,
) -> None:
    """Shared invariants for full State and terminal public Result."""
    from workflow_studio.state.review_policy import evaluate_review, validate_review_binding

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
    for item in history:
        if item.review.contract_version == 2:
            if requirements is None or design is None:
                raise ValueError("v2_review_requires_upstream_context")
            validate_review_binding(item.review, requirements, design, item.code)
    decision = evaluate_review(review) if review is not None else None
    if review_status in (ReviewStatus.PASSED, ReviewStatus.NEEDS_REVISION, ReviewStatus.FAILED):
        if review is None or history[-1].revision_count != revision_count or history[-1].code != code:
            raise ValueError("review_must_describe_current_code")
        if (review_status == ReviewStatus.PASSED) != decision.passed:
            raise ValueError("review_status_does_not_match_verdict")
    if review_status == ReviewStatus.NEEDS_REVISION and (revision_count >= max_revisions or status not in ("running", "failed", "stopped")):
        raise ValueError("no_revision_budget_remaining")
    if review_status == ReviewStatus.NEEDS_REVISION and decision.action != "code_revision":
        raise ValueError("review_does_not_allow_code_revision")
    if review_status == ReviewStatus.FAILED and revision_count != max_revisions and decision.action != "manual_required":
        raise ValueError("review_failed_before_budget_exhausted")
    if review_status == ReviewStatus.ERROR and status != "failed":
        raise ValueError("review_error_requires_failed_workflow")
    if status == "completed" and review_status != ReviewStatus.PASSED:
        raise ValueError("review_mode_completion_requires_passed_review")
    if status == "completed_with_issues" and review_status != ReviewStatus.FAILED:
        raise ValueError("issues_terminal_requires_failed_review")
    if review_status == ReviewStatus.FAILED and status not in ("completed_with_issues", "failed", "stopped"):
        raise ValueError("exhausted_review_requires_terminal_state")
