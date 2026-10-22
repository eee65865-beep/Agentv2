"""Evidence-aware static review with an independent deterministic joint gate."""

from dataclasses import dataclass
from hashlib import sha256

from workflow_studio.agents.review_agent import review_code
from workflow_studio.state.review import ReviewOutput
from workflow_studio.state.review_policy import evaluate_review, validate_review_binding
from workflow_studio.validation.models import ValidationReport
from workflow_studio.validation.service import validate_report_binding


@dataclass(frozen=True)
class EvidenceReview:
    report: ValidationReport
    report_digest: str
    review: ReviewOutput
    accepted: bool


def review_validated(result, report, *, max_repair_attempts=2):
    if result.run_id != report.run_id:
        raise ValueError("validation_run_id_mismatch")
    validate_report_binding(report, result.requirements, result.design, result.code)
    review = review_code(result.requirements, result.design, result.code, max_repair_attempts=max_repair_attempts, validation_report=report)
    validate_review_binding(review, result.requirements, result.design, result.code)
    return EvidenceReview(report=report, report_digest=sha256(report.model_dump_json().encode()).hexdigest(), review=review,
                          accepted=result.status == "completed" and report.status == "passed" and evaluate_review(review).passed)
