"""Session-local background execution and event collection, independent of UI."""

from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, Thread
from time import perf_counter
from pydantic import ValidationError

from workflow_studio.errors import friendly_error
from workflow_studio.budget import RunBudget
from workflow_studio.llm.options import thinking_mode
from workflow_studio.diagnostics import log_failure
from workflow_studio.agents.requirements_agent import MIN_REQUEST_LENGTH
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.review import MAX_REVISIONS
from workflow_studio.state.workflow import (
    WorkflowEvent, WorkflowState, WorkflowStatus, create_initial_state, failure_update,
    merge_events, validate_state,
)
from workflow_studio.workflows.runner import WorkflowStreamPacket, stream_workflow_events
from workflow_studio.storage.checkpoints import CheckpointError, default_checkpoints
from workflow_studio.workflows.durable import (
    DurableView, create_durable, fork_durable_attempt, inspect_durable, stream_durable,
)

AGENTS = ("requirements_agent", "design_agent", "coding_agent")


@dataclass(frozen=True)
class WorkerFinished:
    elapsed: float
    error_type: str | None = None


@dataclass(frozen=True)
class DurableFinished:
    view: DurableView


@dataclass
class TaskHandle:
    queue: Queue[WorkflowStreamPacket | WorkerFinished | DurableFinished] = field(default_factory=Queue)
    thread: Thread | None = None
    cancel_event: Event = field(default_factory=Event)


@dataclass
class PageState:
    thinking_enabled: bool | None = None
    running: bool = False
    review_enabled: bool = False
    max_revisions: int = 1
    task: TaskHandle | None = None
    snapshot: WorkflowState | None = None
    events: list[WorkflowEvent] = field(default_factory=list)
    result: WorkflowResult | None = None
    request: str = ""
    model_name: str = ""
    started_at: float = 0.0
    elapsed: float = 0.0
    error: str | None = None
    exports: dict[str, bytes] = field(default_factory=dict)
    persisted_run_id: str | None = None
    persistence_error: str | None = None
    history_error: str | None = None
    from_history: bool = False
    durable_thread_id: str | None = None
    checkpoint_id: str | None = None
    durable_status: str | None = None
    durable_attempts: list[dict] = field(default_factory=list)
    waiting: bool = False


def _worker(handle: TaskHandle, request: str, review_enabled: bool = False, max_revisions: int = 1, thinking_enabled: bool | None = None, budget: RunBudget | None = None, thread_id=None, checkpoint_id=None, answers=None) -> None:
    started = perf_counter()
    failure: str | None = None
    store = None
    try:
        with thinking_mode(thinking_enabled):
            if thread_id is not None:
                store = default_checkpoints()
                stream = stream_durable(store, thread_id, expected_checkpoint_id=checkpoint_id, answers=answers, cancel_event=handle.cancel_event)
            else:
                stream = stream_workflow_events(request, review_enabled=review_enabled, max_revisions=max_revisions, budget=budget, cancel_event=handle.cancel_event)
            for packet in stream:
                handle.queue.put(packet)
            if store is not None:
                handle.queue.put(DurableFinished(inspect_durable(store, thread_id)))
    except Exception as error:
        log_failure("stream_execution", error)
        failure = type(error).__name__
    finally:
        if store is not None:
            store.close()
        handle.queue.put(WorkerFinished(perf_counter() - started, failure))


def start_task(page: PageState, request: str, configured: bool, model_name: str, *, review_enabled: bool = False, max_revisions: int = 1, thinking_enabled: bool | None = None, budget: RunBudget | None = None, durable: bool = False) -> bool:
    """Only explicit submit invokes this; redraw/download never starts a task."""
    if page.running:
        return False
    if not configured:
        page.error = "DeepSeek API 未配置，请在项目根目录 .env 中配置 DEEPSEEK_API_KEY。"
        return False
    if not isinstance(request, str) or len(request.strip()) < MIN_REQUEST_LENGTH:
        page.error = f"请填写至少 {MIN_REQUEST_LENGTH} 个字符的开发需求。"
        return False
    if type(review_enabled) is not bool or type(max_revisions) is not int or not 0 <= max_revisions <= MAX_REVISIONS:
        page.error = "审查开关必须为布尔值，最大修复次数必须为 0 到 3 的整数。"
        return False
    if thinking_enabled is not None and type(thinking_enabled) is not bool:
        page.error = "思考模式开关必须为布尔值。"
        return False
    if type(durable) is not bool:
        page.error = "持久任务开关必须为布尔值。"
        return False
    if budget is not None and not isinstance(budget, RunBudget):
        page.error = "运行预算必须为有效的 RunBudget 配置。"
        return False
    if budget is not None:
        try:
            budget = RunBudget.model_validate(budget.model_dump())
        except ValidationError:
            page.error = "运行预算参数无效。"
            return False
    view = None
    if durable:
        store = None
        try:
            store = default_checkpoints()
            view = create_durable(store, request, review_enabled=review_enabled, max_revisions=max_revisions, thinking_enabled=thinking_enabled, budget=budget)
        except Exception as error:
            page.error = friendly_error(error)
            return False
        finally:
            if store is not None:
                store.close()
    page.thinking_enabled = thinking_enabled
    page.review_enabled, page.max_revisions = review_enabled, max_revisions
    handle = TaskHandle()
    page.running, page.task = True, handle
    page.snapshot, page.result = None, None
    page.events, page.exports = [], {}
    page.persisted_run_id, page.persistence_error, page.history_error = None, None, None
    page.from_history = False
    page.durable_thread_id = view.thread_id if view is not None else None
    page.checkpoint_id = view.checkpoint_id if view is not None else None
    page.durable_status = view.status if view is not None else None
    page.durable_attempts = view.attempts if view is not None else []
    page.waiting = False
    page.request, page.model_name = request.strip(), model_name
    page.error, page.elapsed, page.started_at = None, 0.0, perf_counter()
    handle.thread = Thread(target=_worker, args=(handle, page.request, review_enabled, max_revisions, thinking_enabled, budget, page.durable_thread_id, page.checkpoint_id), daemon=True, name="workflow-studio-task")
    handle.thread.start()
    return True


def _apply_durable_view(page, view):
    page.durable_thread_id, page.checkpoint_id, page.durable_status = view.thread_id, view.checkpoint_id, view.status
    page.durable_attempts = view.attempts
    page.snapshot, page.events, page.result = validate_state(view.state), list(view.state["events"]), view.result
    page.waiting = view.state["status"] == "waiting"
    page.request, page.model_name, page.elapsed = view.state["user_request"], view.model_name, view.budget.elapsed_seconds
    page.thinking_enabled = view.thinking_enabled
    page.review_enabled, page.max_revisions = view.state["review_enabled"], view.state["max_revisions"]


def open_durable_task(page, thread_id):
    if page.running:
        return False
    store = None
    try:
        store = default_checkpoints()
        view = inspect_durable(store, thread_id)
        _apply_durable_view(page, view)
        page.task, page.exports = None, {}
        page.error = friendly_error(page.result.error) if page.result and page.result.status == "failed" else None
        page.history_error, page.persistence_error, page.persisted_run_id = None, None, None
        page.from_history = True
        return True
    except Exception as error:
        page.history_error = friendly_error(error)
        return False
    finally:
        if store is not None:
            store.close()


def resume_task(page, configured, *, answers=None, mode="resume"):
    if page.running or not page.durable_thread_id:
        return False
    if not configured:
        page.error = "继续任务需要配置 API；可先查看已保存的进度。"
        return False
    store = None
    try:
        store = default_checkpoints()
        view = inspect_durable(store, page.durable_thread_id)
        if view.checkpoint_id != page.checkpoint_id:
            raise CheckpointError("checkpoint_changed_refresh_before_resume")
        if mode in ("retry", "replan"):
            view = fork_durable_attempt(store, view.thread_id, expected_checkpoint_id=view.checkpoint_id, replan=mode == "replan")
        elif mode != "resume":
            raise ValueError("invalid_resume_mode")
        if view.state["status"] == "waiting":
            from workflow_studio.state.requirements import OpenQuestion
            from workflow_studio.workflows.clarification import validate_answers
            validate_answers([OpenQuestion.model_validate(item) for item in view.state["awaiting_questions"]], answers)
        _apply_durable_view(page, view)
    except Exception as error:
        page.error = friendly_error(error)
        return False
    finally:
        if store is not None:
            store.close()
    handle = TaskHandle()
    page.running, page.task, page.waiting = True, handle, False
    page.result, page.exports, page.error = None, {}, None
    page.persisted_run_id, page.persistence_error = None, None
    page.from_history = False
    page.started_at = perf_counter() - page.elapsed
    handle.thread = Thread(target=_worker, args=(handle, page.request, page.review_enabled, page.max_revisions, page.thinking_enabled, None, page.durable_thread_id, page.checkpoint_id, answers), daemon=True, name="workflow-studio-resume")
    handle.thread.start()
    return True


def request_stop(page: PageState) -> bool:
    if page.running and page.task is not None:
        page.task.cancel_event.set()
        return True
    return False


def _finish(page: PageState, item: WorkerFinished) -> None:
    page.running = False
    if not page.durable_thread_id:
        page.elapsed = item.elapsed
    if page.durable_thread_id and item.error_type is not None:
        page.error = friendly_error(item.error_type)
        page.result = None
        return
    if page.durable_thread_id and page.snapshot is not None and page.snapshot["status"] == "waiting":
        page.waiting, page.result, page.error = True, None, None
        return
    if page.snapshot is not None and page.snapshot["status"] in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES, WorkflowStatus.FAILED, WorkflowStatus.STOPPED):
        page.result = WorkflowResult.from_state(page.snapshot)
    else:
        base = page.snapshot or create_initial_state(page.request, review_enabled=page.review_enabled, max_revisions=page.max_revisions)
        update = failure_update(base, base["current_agent"] or "workflow", RuntimeError("stream ended without a terminal state"))
        page.snapshot = validate_state({**base, **update, "events": merge_events(base["events"], update["events"])})
        page.result = WorkflowResult.from_state(page.snapshot)
        page.error = "工作流未返回完整结果，请检查网络与 API 配置后手动重试。"
    page.events = merge_events(page.events, page.result.events)
    if page.result.status == WorkflowStatus.FAILED:
        page.error = friendly_error(page.result.error)
    from workflow_studio.web.persistence import save_page
    save_page(page)


def collect_updates(page: PageState) -> bool:
    """Drain existing packets only; polling never calls the Workflow service."""
    was_running = page.running
    if page.running:
        page.elapsed = perf_counter() - page.started_at
    if page.task is None:
        return False
    while True:
        try:
            item = page.task.queue.get_nowait()
        except Empty:
            break
        if isinstance(item, DurableFinished):
            _apply_durable_view(page, item.view)
        elif isinstance(item, WorkerFinished):
            _finish(page, item)
        elif item.kind == "state" and item.state is not None:
            page.snapshot = validate_state(item.state)
            page.events = merge_events(page.events, page.snapshot["events"])
        elif item.kind == "event" and item.event is not None:
            page.events = merge_events(page.events, [item.event])
    return was_running and not page.running


def agent_statuses(page: PageState) -> dict[str, str]:
    statuses = dict.fromkeys((*AGENTS, "review_agent") if page.review_enabled else AGENTS, "等待中")
    for event in page.events:
        if page.review_enabled and event.agent_name == "coding_agent" and event.event_type == "started" and event.revision_count:
            statuses["review_agent"] = "等待中"
        if event.agent_name in statuses:
            statuses[event.agent_name] = {"started": "运行中", "completed": "已完成", "failed": "执行失败", "stopped": "已停止"}.get(event.event_type, statuses[event.agent_name])
    return statuses


