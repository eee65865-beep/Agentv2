"""Checkpointed clarification nodes. Interrupts never run inside Agent wrappers."""

from datetime import datetime, timezone
from time import perf_counter

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from workflow_studio.budget import RunStopped, current_budget
from workflow_studio.state.requirements import ClarificationAnswer, OpenQuestion, RequirementsOutput
from workflow_studio.state.workflow import WorkflowEvent, WorkflowState, WorkflowStatus, validate_state, validate_update
from workflow_studio.workflows.nodes import (
    _emit, _stop_node, requirements_agent_node, design_agent_node, coding_agent_node, review_agent_node,
)
from workflow_studio.workflows.routing import route_after_coding, route_after_review


def validate_answers(questions, answers):
    if not isinstance(answers, dict) or set(answers) != {item.id for item in questions}:
        raise ValueError("answers_must_match_pending_questions")
    result = [ClarificationAnswer(question_id=item.id, question=item.question, answer=answers[item.id]) for item in questions]
    if sum(len(item.answer.encode("utf-8")) for item in result) > 32768:
        raise ValueError("clarification_answers_too_large")
    return result


def _control_update(state, agent_name, change):
    current = validate_state(state)
    tracker = current_budget()
    began, clock = datetime.now(timezone.utc), perf_counter()
    update = None
    try:
        if tracker is not None:
            tracker.reserve_node()
        update = change(current, tracker)
        if tracker is not None:
            tracker.check()
            update["budget"] = tracker.snapshot().model_dump(mode="json")
    except RunStopped as stop:
        _stop_node(current, agent_name, stop.reason, began, clock, specific=update)
    event = WorkflowEvent(
        run_id=current["run_id"], agent_name=agent_name,
        event_type="waiting" if update["status"] == WorkflowStatus.WAITING else "info",
        status=update["status"], message="等待用户澄清。" if update["status"] == WorkflowStatus.WAITING else "澄清检查已完成。",
        revision_count=current["revision_count"], started_at=began, ended_at=datetime.now(timezone.utc),
        elapsed_seconds=perf_counter() - clock, output_fields=(*update, "events"),
    )
    update["events"] = [event]
    checked = validate_update(current, update)
    _emit(event)
    return checked


def prepare_clarification(state):
    def change(current, tracker):
        req = RequirementsOutput.model_validate(current["requirements"])
        answered = {(item["question_id"], item["question"]) for item in current["clarifications"]}
        pending = [item for item in req.open_questions if item.blocking and (item.id, item.question) not in answered]
        if pending and tracker is not None and tracker.clarification_rounds >= tracker.limits.max_clarification_rounds:
            tracker._stop("clarifications")
        return {"status": WorkflowStatus.WAITING if pending else WorkflowStatus.RUNNING,
                "current_agent": "clarification_prepare", "awaiting_questions": [item.model_dump() for item in pending]}
    return _control_update(state, "clarification_prepare", change)


def clarification_gate(state):
    current = validate_state(state)
    questions = [OpenQuestion.model_validate(item) for item in current["awaiting_questions"]]
    # No model calls, budget charges, writes or broad exception handlers before interrupt.
    answers = interrupt({"kind": "requirements_clarification", "questions": [item.model_dump() for item in questions]})
    records = validate_answers(questions, answers)
    def change(current, tracker):
        if tracker is not None:
            tracker.consume_clarification()
        return {"status": WorkflowStatus.RUNNING, "current_agent": "clarification_gate", "awaiting_questions": [],
                "clarifications": current["clarifications"] + [item.model_dump() for item in records]}
    return _control_update(current, "clarification_gate", change)


ENTRY_NODES = {"requirements_agent", "clarification_prepare", "design_agent", "coding_agent", "review_agent"}


def build_durable_workflow(checkpointer, *, review_enabled=False, entry_node="requirements_agent"):
    if entry_node not in ENTRY_NODES or (entry_node == "review_agent" and not review_enabled):
        raise ValueError("invalid_durable_entry_node")
    graph = StateGraph(WorkflowState)
    for name, node in (
        ("requirements_agent", requirements_agent_node), ("clarification_prepare", prepare_clarification),
        ("clarification_gate", clarification_gate), ("design_agent", design_agent_node), ("coding_agent", coding_agent_node),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, entry_node)
    graph.add_edge("requirements_agent", "clarification_prepare")
    graph.add_conditional_edges("clarification_prepare", lambda state: "clarification_gate" if state["status"] == "waiting" else "design_agent")
    graph.add_edge("clarification_gate", "requirements_agent")
    graph.add_edge("design_agent", "coding_agent")
    if review_enabled:
        graph.add_node("review_agent", review_agent_node)
        graph.add_conditional_edges("coding_agent", route_after_coding, {"review_agent": "review_agent", "__end__": END})
        graph.add_conditional_edges("review_agent", route_after_review, {"coding_agent": "coding_agent", "__end__": END})
    else:
        graph.add_edge("coding_agent", END)
    return graph.compile(checkpointer=checkpointer)
