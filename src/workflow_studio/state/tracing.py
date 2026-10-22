"""Derive execution views from immutable events, without touching graph state."""

from datetime import datetime
from dataclasses import dataclass

from workflow_studio.state.workflow import WorkflowEvent, WorkflowStatus


@dataclass(frozen=True)
class RunTrace:
    run_id: str | None
    status: WorkflowStatus
    model_names: tuple[str, ...]
    started_at: datetime | None
    ended_at: datetime | None


def summarize_run(events: list[WorkflowEvent]) -> RunTrace:
    models = tuple(dict.fromkeys(event.model_name for event in events if event.model_name))
    started = next((event.started_at or event.timestamp for event in events if event.event_type == "started"), None)
    terminal = next((event for event in reversed(events) if event.status in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES, WorkflowStatus.FAILED, WorkflowStatus.STOPPED)), None)
    return RunTrace(
        run_id=events[0].run_id if events else None,
        status=events[-1].status if events else WorkflowStatus.PENDING,
        model_names=models, started_at=started,
        ended_at=(terminal.ended_at or terminal.timestamp) if terminal else None,
    )


def agent_events(events: list[WorkflowEvent], agent_name: str) -> list[WorkflowEvent]:
    return [event for event in events if event.agent_name == agent_name]
