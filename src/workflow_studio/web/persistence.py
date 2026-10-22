"""Page actions for persistence; never start or rerun a Workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflow_studio.diagnostics import log_failure
from workflow_studio.errors import StorageError, StorageWriteError, friendly_error
from workflow_studio.state.workflow import validate_state
from workflow_studio.storage import default_store

if TYPE_CHECKING:
    from workflow_studio.web.controller import PageState


def save_page(page: PageState) -> bool:
    if page.result is None or page.running:
        return False
    try:
        default_store().save(page.result, page.request, page.model_name, page.elapsed)
    except (StorageError, OSError) as error:
        log_failure("persist_page", error, run_id=page.result.run_id, agent="storage")
        page.persistence_error = friendly_error(error if isinstance(error, StorageError) else StorageWriteError())
        return False
    page.persisted_run_id = page.result.run_id
    page.persistence_error = None
    return True


def open_history(page: PageState, run_id: str) -> bool:
    if page.running:
        page.history_error = "工作流正在运行，请等待结束后再打开历史。"
        return False
    try:
        saved = default_store().load(run_id)
    except (StorageError, OSError) as error:
        page.history_error = friendly_error(error)
        return False
    result, metadata = saved.result, saved.metadata
    snapshot = validate_state({**result.model_dump(), "user_request": metadata.user_request, "current_agent": result.events[-1].agent_name if result.events else ""})
    page.result, page.snapshot, page.events = result, snapshot, list(result.events)
    page.request, page.model_name, page.elapsed = metadata.user_request, metadata.model_name, metadata.elapsed_seconds
    page.review_enabled, page.max_revisions = result.review_enabled, result.max_revisions
    page.task, page.exports = None, {}
    page.error = friendly_error(result.error) if result.status == "failed" else None
    page.persisted_run_id, page.persistence_error = result.run_id, None
    page.history_error, page.from_history = None, True
    page.durable_thread_id, page.checkpoint_id, page.durable_status = None, None, None
    page.durable_attempts, page.waiting = [], False
    return True
