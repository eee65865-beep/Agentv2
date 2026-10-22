"""Explicit, bounded validation-driven file repair, preserving all parent runs."""

from dataclasses import dataclass
from uuid import uuid4

from workflow_studio.agents.coding_agent import generate_code
from workflow_studio.budget import BudgetSnapshot, BudgetTracker, RunBudget, RunStopped, budget_scope, current_budget
from workflow_studio.llm.repairs import collect_repairs
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.patch import RepairReceipt
from workflow_studio.state.review_policy import evaluate_review
from workflow_studio.validation.models import ValidationLimits, ValidationReport
from workflow_studio.validation.service import validate_artifact
from workflow_studio.validation.store import ValidationStore


def regression_cases(before, after):
    """None means not comparable, never 'no regression' for incomplete evidence."""
    if before.suite_digest != after.suite_digest or before.environment != after.environment or before.environment == "unavailable":
        return None
    if any(report.reason != "finished" or len(report.cases) != report.expected_cases or not report.cases or any(case.status not in ("passed", "failed") for case in report.cases) for report in (before, after)):
        return None
    if {case.case_id for case in before.cases} != {case.case_id for case in after.cases}:
        return None
    previously_passed = {case.case_id for case in before.cases if case.status == "passed"}
    return tuple(case.case_id for case in after.cases if case.case_id in previously_passed and case.status == "failed")


@dataclass(frozen=True)
class ValidationRepairAttempt:
    result: WorkflowResult
    report: ValidationReport
    parent_run_id: str | None = None
    receipt: RepairReceipt | None = None
    regressions: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ValidationRepairRun:
    original_run_id: str
    status: str
    attempts: tuple[ValidationRepairAttempt, ...]
    budget: BudgetSnapshot
    error_type: str | None = None
    pending_candidate: WorkflowResult | None = None
    pending_receipt: RepairReceipt | None = None

    @property
    def metrics(self):
        receipts = [attempt.receipt for attempt in self.attempts if attempt.receipt is not None]
        if self.pending_receipt is not None:
            receipts.append(self.pending_receipt)
        comparable = sum(attempt.regressions is not None for attempt in self.attempts)
        regressions = sum(bool(attempt.regressions) for attempt in self.attempts)
        return {"repair_rounds": len(receipts), "validated_repair_rounds": max(0, len(self.attempts) - 1),
                "first_passed": bool(self.attempts and self.attempts[0].report.status == "passed"),
                "final_passed": self.status == "passed",
                "regression_rounds": regressions, "comparable_rounds": comparable,
                "regression_rate": regressions / comparable if comparable else None,
                "input_context_bytes": sum(receipt.input_context_bytes for receipt in receipts),
                "normalized_patch_bytes": sum(receipt.normalized_patch_bytes for receipt in receipts),
                "merged_artifact_bytes": sum(receipt.merged_artifact_bytes for receipt in receipts),
                "accepted_patch_format_failures": sum(receipt.format_failures for receipt in receipts),
                "logical_calls_cumulative": self.budget.logical_calls,
                "known_tokens_cumulative": self.budget.known_tokens,
                "unknown_usage_calls_cumulative": self.budget.unknown_usage_calls}


def repair_and_validate(result, suite_id, *, max_repair_rounds=1, limits=None, budget=None, policy=None, backend=None, cancel=None, store=None):
    """No automatic invocation by the UI/CLI; may call the model and container.

    Child candidates have new run IDs and independent reports. Their old static
    review is invalidated; a fixed-suite pass is not a new static-review pass.
    """
    if current_budget() is not None:
        raise ValueError("validation_repair_requires_top_level_budget_scope")
    result = WorkflowResult.model_validate(result.model_dump())
    if type(max_repair_rounds) is not int or not 0 <= max_repair_rounds <= 3:
        raise ValueError("invalid_validation_repair_round_limit")
    if result.code is None:
        raise ValueError("validation_repair_requires_code")
    if result.budget is not None and budget is not None:
        raise ValueError("cannot_override_inherited_run_budget")
    tracker = BudgetTracker.restore(result.budget, cancel) if result.budget is not None else BudgetTracker(budget or RunBudget(), cancel)
    limits = ValidationLimits() if limits is None else ValidationLimits.model_validate(limits.model_dump())
    store = store if store is not None else ValidationStore()
    attempts = []
    pending_candidate, pending_receipt = None, None
    def done(status, error_type=None):
        snapshot = tracker.snapshot()
        pending = pending_candidate
        if pending is not None:
            pending = WorkflowResult.model_validate({**pending.model_dump(), "status": "stopped" if snapshot.stop_reason is not None else pending.status, "budget": snapshot.model_dump()})
        return ValidationRepairRun(result.run_id, status, tuple(attempts), snapshot, error_type, pending, pending_receipt)
    def validate(candidate):
        tracker.reserve_node()
        remaining = tracker.limits.max_elapsed_seconds - tracker.snapshot().elapsed_seconds
        if remaining < 1:
            tracker._stop("deadline")
        constrained = limits.model_copy(update={"total_seconds": min(limits.total_seconds, remaining)})
        report = validate_artifact(candidate, suite_id, backend=backend, limits=constrained, cancel=tracker.cancel)
        return report
    try:
        with budget_scope(tracker):
            tracker.check()
            candidate = result
            report = validate(candidate)
            attempts.append(ValidationRepairAttempt(candidate, report))
            store.save(report)
            tracker.check()
            if result.review is not None and evaluate_review(result.review).action == "manual_required":
                return done("manual_required")
            rounds = min(max_repair_rounds, max(0, result.max_revisions - result.revision_count))
            for repair_index in range(rounds):
                if report.status != "failed":
                    break
                tracker.reserve_node()
                if tracker.logical_calls >= tracker.limits.max_logical_calls:
                    tracker._stop("logical_calls")
                with collect_repairs() as receipts:
                    code = generate_code(candidate.requirements, candidate.design, previous_code=candidate.code,
                                         validation_report=report, repair_policy=policy)
                if not receipts:
                    raise ValueError("incremental_repair_did_not_publish_receipt")
                child = WorkflowResult(run_id=str(uuid4()), status="completed", requirements=candidate.requirements, design=candidate.design,
                    code=code, events=[], error=None, clarifications=candidate.clarifications, budget=tracker.snapshot(),
                    max_revisions=max(0, result.max_revisions - result.revision_count - repair_index - 1))
                pending_candidate, pending_receipt = child, receipts[-1]
                tracker.check()
                updated = validate(child)
                child = WorkflowResult.model_validate({**child.model_dump(), "budget": tracker.snapshot().model_dump()})
                regressions = regression_cases(report, updated)
                attempts.append(ValidationRepairAttempt(child, updated, candidate.run_id, receipts[-1], regressions))
                pending_candidate, pending_receipt = None, None
                store.save(updated)
                candidate, report = child, updated
                tracker.check()
                if regressions:
                    return done("regression")
            # Require a post-repair static review separately; report status is fixed-suite only.
            return done(report.status)
    except RunStopped:
        return done("stopped")
    except Exception as error:
        return done("error", type(error).__name__)
