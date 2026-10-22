"""Public sync/async and streaming entry points with isolated run identity."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from threading import Event
from pydantic import ValidationError
from workflow_studio.budget import BudgetTracker, RunBudget, STOP_LABELS, budget_scope

from workflow_studio.diagnostics import log_failure
from workflow_studio.agents.requirements_agent import MIN_REQUEST_LENGTH
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.workflow import WorkflowEvent, WorkflowState, WorkflowStateError, create_initial_state, failure_update, merge_events, validate_state
from workflow_studio.workflows.graph import build_workflow
from workflow_studio.workflows.nodes import WorkflowNodeError


class WorkflowInputError(ValueError):
    """Reject empty/insufficient requests before compiling or calling Agents."""


def _initial(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1) -> WorkflowState:
    if not isinstance(user_request, str) or len(user_request.strip()) < MIN_REQUEST_LENGTH:
        raise WorkflowInputError(f"user_request 必须为非空字符串，且至少 {MIN_REQUEST_LENGTH} 个字符。")
    try:
        return create_initial_state(user_request.strip(), review_enabled=review_enabled, max_revisions=max_revisions)
    except WorkflowStateError:
        raise WorkflowInputError("review_enabled 必须为布尔值；max_revisions 必须为 0 到 3 的整数。") from None


def _infrastructure_failure(initial: WorkflowState, error: Exception, tracker: BudgetTracker) -> WorkflowState:
    log_failure("graph_execution", error, run_id=initial["run_id"])
    budget = tracker.snapshot()
    if budget.stop_reason is not None:
        event = WorkflowEvent(run_id=initial["run_id"], agent_name="workflow", event_type="stopped", status="stopped", message=STOP_LABELS[budget.stop_reason])
        update = {"status": "stopped", "current_agent": "workflow", "error": None, "events": [event]}
    else:
        update = failure_update(initial, "workflow", error)
    update["budget"] = budget.model_dump(mode="json")
    return validate_state({**initial, **update, "events": merge_events(initial["events"], update["events"])})


def _execution(user_request, review_enabled, max_revisions, budget, cancel_event):
    if budget is not None and not isinstance(budget, RunBudget):
        raise WorkflowInputError("budget 必须为 RunBudget 或 None。")
    if cancel_event is not None and not isinstance(cancel_event, Event):
        raise WorkflowInputError("cancel_event 必须为 threading.Event 或 None。")
    try:
        tracker = BudgetTracker(budget if budget is not None else RunBudget(), cancel_event)
    except ValidationError:
        raise WorkflowInputError("运行预算参数无效。") from None
    initial = _initial(user_request, review_enabled=review_enabled, max_revisions=max_revisions)
    return validate_state({**initial, "budget": tracker.snapshot().model_dump(mode="json")}), tracker


def _budgeted_stream(stream, tracker):
    """Do not leak the run ContextVar into the consumer between yields."""
    try:
        while True:
            with budget_scope(tracker):
                try:
                    packet = next(stream)
                except StopIteration:
                    return
            yield packet
    finally:
        with budget_scope(tracker):
            stream.close()


def run_workflow(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1, budget: RunBudget | None = None, cancel_event: Event | None = None) -> WorkflowResult:
    """Run the compiled StateGraph; a failed node aborts downstream scheduling."""
    initial, tracker = _execution(user_request, review_enabled, max_revisions, budget, cancel_event)
    try:
        with budget_scope(tracker):
            state = build_workflow(review_enabled=review_enabled).invoke(initial)
        return WorkflowResult.from_state(state)
    except WorkflowNodeError as error:
        return WorkflowResult.from_state(error.state)
    except Exception as error:
        return WorkflowResult.from_state(_infrastructure_failure(initial, error, tracker))


async def arun_workflow(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1, budget: RunBudget | None = None, cancel_event: Event | None = None) -> WorkflowResult:
    """LangGraph runs the synchronous Agent nodes in its async executor."""
    initial, tracker = _execution(user_request, review_enabled, max_revisions, budget, cancel_event)
    try:
        with budget_scope(tracker):
            state = await build_workflow(review_enabled=review_enabled).ainvoke(initial)
        return WorkflowResult.from_state(state)
    except WorkflowNodeError as error:
        return WorkflowResult.from_state(error.state)
    except Exception as error:
        return WorkflowResult.from_state(_infrastructure_failure(initial, error, tracker))


def stream_workflow(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1, budget: RunBudget | None = None, cancel_event: Event | None = None) -> Iterator[WorkflowState]:
    """Yield State snapshots, including a terminal failed snapshot when aborted.

    For immediate started/completed events while a node is running, use the
    compiled graph's stream/astream with stream_mode=["custom", "values"].
    """
    initial, tracker = _execution(user_request, review_enabled, max_revisions, budget, cancel_event)
    latest = initial
    try:
        for state in _budgeted_stream(build_workflow(review_enabled=review_enabled).stream(initial, stream_mode="values"), tracker):
            latest = validate_state(state)
            yield latest
    except WorkflowNodeError as error:
        yield validate_state(error.state)
    except Exception as error:
        yield _infrastructure_failure(latest, error, tracker)


@dataclass(frozen=True)
class WorkflowStreamPacket:
    """A real LangGraph custom event or committed State snapshot."""

    kind: Literal["event", "state"]
    event: WorkflowEvent | None = None
    state: WorkflowState | None = None


def stream_workflow_events(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1, budget: RunBudget | None = None, cancel_event: Event | None = None) -> Iterator[WorkflowStreamPacket]:
    """Service stream for UI consumers; preserves failed terminal snapshots."""
    initial, tracker = _execution(user_request, review_enabled, max_revisions, budget, cancel_event)
    latest = initial
    try:
        for mode, data in _budgeted_stream(build_workflow(review_enabled=review_enabled).stream(initial, stream_mode=["custom", "values"]), tracker):
            if mode == "custom":
                yield WorkflowStreamPacket(kind="event", event=WorkflowEvent.model_validate(data))
            else:
                latest = validate_state(data)
                yield WorkflowStreamPacket(kind="state", state=latest)
    except WorkflowNodeError as error:
        yield WorkflowStreamPacket(kind="state", state=validate_state(error.state))
    except Exception as error:
        yield WorkflowStreamPacket(kind="state", state=_infrastructure_failure(latest, error, tracker))
