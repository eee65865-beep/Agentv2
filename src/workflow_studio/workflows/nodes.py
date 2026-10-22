"""Independent LangGraph nodes, returning validated partial State updates."""

from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter

from langgraph.config import get_stream_writer
from pydantic import BaseModel, ValidationError
from workflow_studio.budget import BudgetPersistenceError, RunStopped, STOP_LABELS, current_budget

from workflow_studio.diagnostics import log_failure
from workflow_studio.config import load_settings
from workflow_studio.llm.telemetry import collect_llm_calls
from workflow_studio.llm.repairs import collect_repairs
from workflow_studio.llm.options import current_model_profile
from workflow_studio.state.telemetry import aggregate_usage

from workflow_studio.agents.requirements_agent import analyze_requirements
from workflow_studio.agents.design_agent import design_system
from workflow_studio.agents.coding_agent import generate_code
from workflow_studio.agents.review_agent import review_code
from workflow_studio.state.review import ReviewOutput, ReviewRecord, ReviewStatus, merge_review_history
from workflow_studio.state.review_policy import evaluate_review, review_failure_fingerprint
from workflow_studio.state.requirements import ClarificationAnswer, RequirementsOutput, effective_request
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.workflow import (
    WorkflowEvent, WorkflowState, WorkflowStateError, WorkflowStatus, WorkflowUpdate,
    failure_update, merge_events, validate_state, validate_update,
)


class WorkflowNodeError(Exception):
    """Abort graph scheduling while preserving a safe, validated failed snapshot."""

    def __init__(self, state: WorkflowState) -> None:
        super().__init__("Workflow node failed; inspect the structured result.")
        self.state = validate_state(state)


def _emit(event: WorkflowEvent) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:
        # Direct unit-test invocation outside a LangGraph execution context.
        return
    writer(event.model_dump(mode="json"))


def _snapshot(state: WorkflowState, update: WorkflowUpdate) -> WorkflowState:
    return validate_state({**state, **update, "events": merge_events(state["events"], update.get("events", [])), "review_history": merge_review_history(state["review_history"], update.get("review_history", []))})


def _configured_model() -> str | None:
    profile = current_model_profile()
    if profile is not None:
        return profile["deepseek_model"]
    try:
        return load_settings().deepseek_model
    except (ValidationError, OSError):
        # The Agent/client remains responsible for rejecting invalid config.
        return None


def _stop_node(state, agent_name, reason, began, clock, calls=(), specific=None, repair_receipt=None):
    tracker = current_budget()
    update = {**(specific or {}), "status": WorkflowStatus.STOPPED, "current_agent": agent_name, "error": None,
              "budget": tracker.snapshot().model_dump(mode="json")}
    ended = max(began, datetime.now(timezone.utc))
    event = WorkflowEvent(
        run_id=state["run_id"], agent_name=agent_name, event_type="stopped", status=WorkflowStatus.STOPPED,
        message=STOP_LABELS[reason], started_at=began, ended_at=ended, timestamp=ended,
        elapsed_seconds=perf_counter() - clock, revision_count=state["revision_count"],
        output_fields=(*update, "events"), llm_calls=tuple(calls), token_usage=aggregate_usage(tuple(calls)),
        repair_receipt=repair_receipt,
    )
    update["events"] = [event]
    stopped = _snapshot(state, validate_update(state, update))
    _emit(event)
    raise WorkflowNodeError(stopped)


def _result_update(state: WorkflowState, key: str, payload: dict[str, object]) -> dict[str, object]:
    status = WorkflowStatus.COMPLETED if key == "code" and not state["review_enabled"] else WorkflowStatus.RUNNING
    update: dict[str, object] = {key: payload, "status": status}
    if key == "code" and state["review_enabled"]:
        update["review_status"] = ReviewStatus.PENDING
        if state["revision_count"]:
            update["revision_count"] = state["revision_count"]
    if key == "review":
        verdict = ReviewOutput.model_validate(payload)
        record = ReviewRecord(run_id=state["run_id"], revision_count=state["revision_count"], review=verdict, code=CodingOutput.model_validate(state["code"]))
        update["review_history"] = [record]
        decision = evaluate_review(verdict)
        if decision.passed:
            update.update(status=WorkflowStatus.COMPLETED, review_status=ReviewStatus.PASSED)
        elif decision.action == "manual_required":
            update.update(status=WorkflowStatus.COMPLETED_WITH_ISSUES, review_status=ReviewStatus.FAILED)
        elif state["revision_count"] < state["max_revisions"]:
            update["review_status"] = ReviewStatus.NEEDS_REVISION
        else:
            update.update(status=WorkflowStatus.COMPLETED_WITH_ISSUES, review_status=ReviewStatus.FAILED)
    return update


def _execute(
    state: WorkflowState, agent_name: str, output_key: str,
    schema: type[BaseModel], action: Callable[[WorkflowState], BaseModel],
) -> WorkflowUpdate:
    current = validate_state(state)
    if current["status"] not in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING):
        raise WorkflowStateError("Cannot execute an Agent on a terminal state.")
    inputs = {"requirements": ("user_request",), "design": ("requirements",), "code": ("requirements", "design"), "review": ("requirements", "design", "code")}[output_key]
    revising = output_key == "code" and current["review_status"] == ReviewStatus.NEEDS_REVISION
    attempt = current["revision_count"] + int(revising)
    if revising:
        inputs += ("code", "review")
    if output_key == "requirements" and current["clarifications"]:
        inputs += ("clarifications",)
    if output_key == "design" and current["design_feedback"]:
        inputs += ("design_feedback",)
    began = datetime.now(timezone.utc)
    clock = perf_counter()
    tracker = current_budget()
    if tracker is not None:
        try:
            tracker.reserve_node()
        except RunStopped as stop:
            _stop_node(current, agent_name, stop.reason, began, clock)
    model = _configured_model()
    started = WorkflowEvent(
        run_id=current["run_id"], agent_name=agent_name, event_type="started",
        message=f"Started {agent_name}.", status=WorkflowStatus.RUNNING,
        timestamp=began, started_at=began, model_name=model, input_fields=inputs, revision_count=attempt,
    )
    begin_update: dict[str, object] = {"status": "running", "current_agent": agent_name, "events": [started]}
    if tracker is not None:
        begin_update["budget"] = tracker.snapshot().model_dump(mode="json")
    if revising:
        begin_update.update(revision_count=attempt, review_status=ReviewStatus.PENDING)
    running = _snapshot(current, validate_update(current, begin_update))
    _emit(started)
    with collect_llm_calls() as calls, collect_repairs() as repairs:
        try:
            output = action(running)
            if not isinstance(output, schema):
                raise WorkflowStateError("Agent returned an unexpected output type.")
            payload = schema.model_validate(output.model_dump()).model_dump(mode="json")
            specific = _result_update(running, output_key, payload)
            if tracker is not None:
                try:
                    if output_key == "review" and specific["status"] == WorkflowStatus.RUNNING:
                        tracker.observe_review(review_failure_fingerprint(ReviewOutput.model_validate(payload)))
                    tracker.check()
                except RunStopped as stop:
                    _stop_node(running, agent_name, stop.reason, began, clock, calls, specific, repairs[-1] if repairs else None)
                specific["budget"] = tracker.snapshot().model_dump(mode="json")
            ended = max(began, datetime.now(timezone.utc))
            completed = WorkflowEvent(
                run_id=running["run_id"], agent_name=agent_name, event_type="completed",
                message=f"Completed {agent_name}.", status=specific["status"],
                timestamp=ended, started_at=began, ended_at=ended,
                elapsed_seconds=perf_counter() - clock,
                model_name=calls[-1].model_name if calls else model,
                input_fields=inputs, output_fields=(*specific, "current_agent", "events"), revision_count=attempt,
                llm_calls=tuple(calls), token_usage=aggregate_usage(tuple(calls)),
                repair_receipt=repairs[-1] if repairs else None,
            )
            update = validate_update(running, {**specific, "current_agent": agent_name, "events": [started, completed]})
        except (WorkflowNodeError, BudgetPersistenceError):
            raise
        except RunStopped as stop:
            _stop_node(running, agent_name, stop.reason, began, clock, calls)
        except Exception as error:
            log_failure("agent_execution", error, run_id=running["run_id"], agent=agent_name)
            ended = max(began, datetime.now(timezone.utc))
            fields = ("status", "current_agent", "error", "events")
            if tracker is not None:
                fields += ("budget",)
            if output_key == "review":
                fields += ("review_status",)
            if revising:
                fields += ("revision_count", "review_status")
            failed_event = WorkflowEvent(
                run_id=running["run_id"], agent_name=agent_name, event_type="failed",
                message=f"Agent execution failed ({type(error).__name__}).", status=WorkflowStatus.FAILED,
                timestamp=ended, started_at=began, ended_at=ended,
                elapsed_seconds=perf_counter() - clock,
                model_name=calls[-1].model_name if calls else model,
                input_fields=inputs, output_fields=fields, revision_count=attempt,
                error_type=type(error).__name__, llm_calls=tuple(calls), token_usage=aggregate_usage(tuple(calls)),
            )
            failed_update = failure_update(running, agent_name, error, event=failed_event)
            if tracker is not None:
                failed_update["budget"] = tracker.snapshot().model_dump(mode="json")
            failed = _snapshot(running, failed_update)
            _emit(failed_event)
            raise WorkflowNodeError(failed) from None
    _emit(completed)
    return update


def requirements_agent_node(state: WorkflowState) -> WorkflowUpdate:
    def action(current: WorkflowState) -> RequirementsOutput:
        if current["clarifications"]:
            answers = [ClarificationAnswer.model_validate(item) for item in current["clarifications"]]
            return analyze_requirements(effective_request(current["user_request"], answers),
                previous_requirements=RequirementsOutput.model_validate(current["requirements"]),
                source_texts=(current["user_request"], *(item.answer for item in answers)))
        return analyze_requirements(current["user_request"])
    return _execute(state, "requirements_agent", "requirements", RequirementsOutput, action)


def design_agent_node(state: WorkflowState) -> WorkflowUpdate:
    def action(current: WorkflowState) -> DesignOutput:
        if current["requirements"] is None:
            raise WorkflowStateError("Design node requires requirements.")
        requirements = RequirementsOutput.model_validate(current["requirements"])
        if current["design_feedback"]:
            return design_system(requirements, user_request="需在原需求约束内修订的静态审查反馈：\n" + current["design_feedback"])
        return design_system(requirements)
    return _execute(state, "design_agent", "design", DesignOutput, action)


def coding_agent_node(state: WorkflowState) -> WorkflowUpdate:
    def action(current: WorkflowState) -> CodingOutput:
        if current["requirements"] is None or current["design"] is None:
            raise WorkflowStateError("Coding node requires requirements and design.")
        requirements = RequirementsOutput.model_validate(current["requirements"])
        design = DesignOutput.model_validate(current["design"])
        if current["review_enabled"] and current["revision_count"] > 0:
            if current["review"] is None or current["code"] is None:
                raise WorkflowStateError("Revision requires actual feedback and previous code.")
            return generate_code(requirements, design, review=ReviewOutput.model_validate(current["review"]), previous_code=CodingOutput.model_validate(current["code"]))
        return generate_code(requirements, design)
    return _execute(state, "coding_agent", "code", CodingOutput, action)


def review_agent_node(state: WorkflowState) -> WorkflowUpdate:
    def action(current: WorkflowState) -> ReviewOutput:
        if not current["review_enabled"] or any(current[key] is None for key in ("requirements", "design", "code")):
            raise WorkflowStateError("Review node requires enabled review and all three upstream outputs.")
        return review_code(RequirementsOutput.model_validate(current["requirements"]), DesignOutput.model_validate(current["design"]), CodingOutput.model_validate(current["code"]))
    return _execute(state, "review_agent", "review", ReviewOutput, action)
