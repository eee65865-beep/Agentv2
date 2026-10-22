"""Public sync/async and streaming entry points with isolated run identity."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

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


def _infrastructure_failure(initial: WorkflowState, error: Exception) -> WorkflowState:
    log_failure("graph_execution", error, run_id=initial["run_id"])
    update = failure_update(initial, "workflow", error)
    return validate_state({**initial, **update, "events": merge_events(initial["events"], update["events"])})


def run_workflow(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1) -> WorkflowResult:
    """Run the compiled StateGraph; a failed node aborts downstream scheduling."""
    initial = _initial(user_request, review_enabled=review_enabled, max_revisions=max_revisions)
    try:
        state = build_workflow(review_enabled=review_enabled).invoke(initial)
        return WorkflowResult.from_state(state)
    except WorkflowNodeError as error:
        return WorkflowResult.from_state(error.state)
    except Exception as error:
        return WorkflowResult.from_state(_infrastructure_failure(initial, error))


async def arun_workflow(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1) -> WorkflowResult:
    """LangGraph runs the synchronous Agent nodes in its async executor."""
    initial = _initial(user_request, review_enabled=review_enabled, max_revisions=max_revisions)
    try:
        state = await build_workflow(review_enabled=review_enabled).ainvoke(initial)
        return WorkflowResult.from_state(state)
    except WorkflowNodeError as error:
        return WorkflowResult.from_state(error.state)
    except Exception as error:
        return WorkflowResult.from_state(_infrastructure_failure(initial, error))


def stream_workflow(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1) -> Iterator[WorkflowState]:
    """Yield State snapshots, including a terminal failed snapshot when aborted.

    For immediate started/completed events while a node is running, use the
    compiled graph's stream/astream with stream_mode=["custom", "values"].
    """
    initial = _initial(user_request, review_enabled=review_enabled, max_revisions=max_revisions)
    latest = initial
    try:
        for state in build_workflow(review_enabled=review_enabled).stream(initial, stream_mode="values"):
            latest = validate_state(state)
            yield latest
    except WorkflowNodeError as error:
        yield validate_state(error.state)
    except Exception as error:
        yield _infrastructure_failure(latest, error)


@dataclass(frozen=True)
class WorkflowStreamPacket:
    """A real LangGraph custom event or committed State snapshot."""

    kind: Literal["event", "state"]
    event: WorkflowEvent | None = None
    state: WorkflowState | None = None


def stream_workflow_events(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1) -> Iterator[WorkflowStreamPacket]:
    """Service stream for UI consumers; preserves failed terminal snapshots."""
    initial = _initial(user_request, review_enabled=review_enabled, max_revisions=max_revisions)
    latest = initial
    try:
        for mode, data in build_workflow(review_enabled=review_enabled).stream(initial, stream_mode=["custom", "values"]):
            if mode == "custom":
                yield WorkflowStreamPacket(kind="event", event=WorkflowEvent.model_validate(data))
            else:
                latest = validate_state(data)
                yield WorkflowStreamPacket(kind="state", state=latest)
    except WorkflowNodeError as error:
        yield WorkflowStreamPacket(kind="state", state=validate_state(error.state))
    except Exception as error:
        yield WorkflowStreamPacket(kind="state", state=_infrastructure_failure(latest, error))
