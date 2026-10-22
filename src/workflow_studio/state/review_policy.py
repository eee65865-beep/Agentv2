"""Deterministic static gates and input binding; never execute generated code."""

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.review import REVIEW_POLICY_VERSION, ReviewAssessmentV2, ReviewOutput


class ReviewBindingError(ValueError):
    """Safe error codes without private source, code or raw output."""


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    passed: bool
    action: Literal["pass", "code_revision", "manual_required"]
    blocking_issue_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    policy_version: str
    verification_status: Literal["not_run"] = "not_run"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def artifact_digest(code: CodingOutput) -> str:
    return _digest(CodingOutput.model_validate(code.model_dump()).model_dump(mode="json"))


def review_context_digest(requirements: RequirementsOutput, design: DesignOutput, code: CodingOutput) -> str:
    req_data = RequirementsOutput.model_validate(requirements.model_dump()).model_dump(mode="json")
    # Keep static-review-v1 hashes readable when legacy records predate questions.
    if not req_data["open_questions"]:
        req_data.pop("open_questions")
    return _digest({
        "requirements": req_data,
        "design": DesignOutput.model_validate(design.model_dump()).model_dump(mode="json"),
        "code": CodingOutput.model_validate(code.model_dump()).model_dump(mode="json"),
    })


def validate_review_code_binding(review: ReviewOutput, code: CodingOutput) -> None:
    if review.contract_version == 2 and review.artifact_digest != artifact_digest(code):
        raise ReviewBindingError("review_artifact_digest_mismatch")


def validate_review_binding(review: ReviewOutput, requirements: RequirementsOutput, design: DesignOutput, code: CodingOutput) -> None:
    if review.contract_version == 1:
        return
    validate_review_code_binding(review, code)
    if review.context_digest != review_context_digest(requirements, design, code):
        raise ReviewBindingError("review_context_digest_mismatch")
    paths = {item.path: item for item in code.files}
    requirement_ids = {item["id"] for item in requirements.requirement_catalog()}
    acceptance_ids = {item.id for item in requirements.acceptance_details}
    for issue in review.issue_details:
        if set(issue.requirement_ids) - requirement_ids or set(issue.acceptance_ids) - acceptance_ids:
            raise ReviewBindingError("unknown_review_requirement_or_acceptance_reference")
        if issue.file is not None:
            if issue.file not in paths:
                raise ReviewBindingError("unknown_review_file_reference")
            content = paths[issue.file].content
            if issue.line_start is not None:
                lines = content.splitlines(keepends=True)
                if issue.line_end > len(lines):
                    raise ReviewBindingError("review_line_out_of_range")
                content = "".join(lines[issue.line_start - 1:issue.line_end])
            if issue.evidence_quote is not None and issue.evidence_quote not in content:
                raise ReviewBindingError("review_quote_not_in_referenced_code")


def bind_review(assessment: ReviewAssessmentV2, requirements: RequirementsOutput, design: DesignOutput, code: CodingOutput) -> ReviewOutput:
    assessed = ReviewAssessmentV2.model_validate(assessment.model_dump())
    result = ReviewOutput.model_validate({
        **assessed.model_dump(), "issues": [item.message for item in assessed.issue_details],
        "artifact_digest": artifact_digest(code), "context_digest": review_context_digest(requirements, design, code),
        "policy_version": REVIEW_POLICY_VERSION,
    })
    validate_review_binding(result, requirements, design, code)
    return result


def evaluate_review(review: ReviewOutput) -> ReviewDecision:
    """Conservative: model rejection or any major/blocker prevents passing."""
    checked = ReviewOutput.model_validate(review.model_dump())
    if checked.contract_version == 1:
        return ReviewDecision(passed=checked.passed, action="pass" if checked.passed else "code_revision", blocking_issue_ids=(), reasons=("legacy_model_verdict",), policy_version="legacy-static")
    blocking = [item for item in checked.issue_details if item.severity in ("blocker", "major")]
    passed = checked.passed and not blocking
    targets = blocking or ([] if passed else checked.issue_details)
    manual = any(item.category in ("requirement", "design") or item.suggested_owner != "coding" for item in targets)
    reasons = tuple(reason for condition, reason in ((bool(blocking), "blocking_issues"), (not checked.passed, "model_rejected")) if condition)
    return ReviewDecision(
        passed=passed, action="pass" if passed else ("manual_required" if manual else "code_revision"),
        blocking_issue_ids=tuple(item.issue_id for item in blocking), reasons=reasons or ("static_gate_passed",),
        policy_version=REVIEW_POLICY_VERSION,
    )


def review_failure_fingerprint(review: ReviewOutput) -> str | None:
    if review.contract_version != 2 or evaluate_review(review).action != "code_revision":
        return None
    issues = [item for item in review.issue_details if item.severity in ("blocker", "major")]
    values = [item.model_dump(mode="json", exclude={"issue_id"}) for item in (issues or review.issue_details)]
    # Exact structured equality only: do not infer semantic equivalence of wording.
    values.sort(key=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False))
    return _digest(values)
