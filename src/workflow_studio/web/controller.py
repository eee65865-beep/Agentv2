"""Session-local background execution and event collection, independent of UI."""

from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread
from time import perf_counter

from workflow_studio.errors import friendly_error
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

AGENTS = ("requirements_agent", "design_agent", "coding_agent")


@dataclass(frozen=True)
class WorkerFinished:
    elapsed: float
    error_type: str | None = None


@dataclass
class TaskHandle:
    queue: Queue[WorkflowStreamPacket | WorkerFinished] = field(default_factory=Queue)
    thread: Thread | None = None


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


def _worker(handle: TaskHandle, request: str, review_enabled: bool = False, max_revisions: int = 1, thinking_enabled: bool | None = None) -> None:
    started = perf_counter()
    failure: str | None = None
    try:
        with thinking_mode(thinking_enabled):
            stream = stream_workflow_events(request, review_enabled=True, max_revisions=max_revisions) if review_enabled else stream_workflow_events(request)
            for packet in stream:
                handle.queue.put(packet)
    except Exception as error:
        log_failure("stream_execution", error)
        failure = type(error).__name__
    finally:
        handle.queue.put(WorkerFinished(perf_counter() - started, failure))


def start_task(page: PageState, request: str, configured: bool, model_name: str, *, review_enabled: bool = False, max_revisions: int = 1, thinking_enabled: bool | None = None) -> bool:
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
    page.thinking_enabled = thinking_enabled
    page.review_enabled, page.max_revisions = review_enabled, max_revisions
    handle = TaskHandle()
    page.running, page.task = True, handle
    page.snapshot, page.result = None, None
    page.events, page.exports = [], {}
    page.persisted_run_id, page.persistence_error, page.history_error = None, None, None
    page.from_history = False
    page.request, page.model_name = request.strip(), model_name
    page.error, page.elapsed, page.started_at = None, 0.0, perf_counter()
    handle.thread = Thread(target=_worker, args=(handle, page.request, review_enabled, max_revisions, thinking_enabled), daemon=True, name="workflow-studio-task")
    handle.thread.start()
    return True


def _finish(page: PageState, item: WorkerFinished) -> None:
    page.running = False
    page.elapsed = item.elapsed
    if page.snapshot is not None and page.snapshot["status"] in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES, WorkflowStatus.FAILED):
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
        if isinstance(item, WorkerFinished):
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
            statuses[event.agent_name] = {"started": "运行中", "completed": "已完成", "failed": "执行失败"}.get(event.event_type, statuses[event.agent_name])
    return statuses


