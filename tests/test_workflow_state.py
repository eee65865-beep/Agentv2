"""State behavior through real LangGraph channels; no production Graph or LLM."""

from datetime import datetime, timedelta, timezone
from typing import get_type_hints
from uuid import UUID, uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from workflow_studio.state.coding import CodeFile, CodingOutput
from workflow_studio.state.design import DesignOutput, ModuleDesign
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.workflow import (
    EventConflictError, WorkflowEvent, WorkflowState, WorkflowStateError,
    WorkflowStatus, WorkflowUpdate, create_initial_state, failure_update,
    merge_events, validate_state, validate_update,
)


@pytest.fixture
def requirements() -> RequirementsOutput:
    return RequirementsOutput(
        project_name="Calculator", summary="Add integers locally", functional_requirements=["Add two integers"],
        non_functional_requirements=[], constraints=["Python standard library only"], assumptions=[],
        acceptance_criteria=["2 + 3 = 5"],
    )


@pytest.fixture
def design(requirements: RequirementsOutput) -> DesignOutput:
    return DesignOutput(
        architecture_overview="Single local module", technology_stack=["Python"],
        modules=[ModuleDesign(name="calculator", responsibility="Add integers", dependencies=[], requirement_ids=["FR-1"])],
        data_models=[], interfaces=["Two integers in, one integer out"], project_structure="calculator.py",
        design_decisions=["Keep it simple"], preserved_constraints=list(requirements.constraints),
    )


@pytest.fixture
def code() -> CodingOutput:
    return CodingOutput(
        files=[CodeFile(path="calculator.py", language="python", content="def add(a: int, b: int) -> int:\n    return a + b\n", description="Add integers")],
        instructions="Run manually", dependencies=[], test_suggestions=["Check 2 + 3"], limitations=["Not executed"],
    )


def event(state: WorkflowState, message: str = "Step done") -> WorkflowEvent:
    return WorkflowEvent(run_id=state["run_id"], agent_name="requirements", event_type="info", message=message, status=WorkflowStatus.RUNNING)


def test_initialization() -> None:
    state = create_initial_state("Build a local calculator")
    assert UUID(state["run_id"]).version == 4
    assert state["user_request"] == "Build a local calculator"
    assert state["status"] == "pending"
    assert state["current_agent"] == ""
    assert state["error"] is None
    assert state["events"] == []
    assert state["requirements"] is state["design"] is state["code"] is None
    assert "events" in get_type_hints(WorkflowState, include_extras=True)


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_request_rejected(text: str) -> None:
    with pytest.raises(WorkflowStateError):
        create_initial_state(text)


def test_partial_update_is_copied_and_does_not_mutate(requirements: RequirementsOutput) -> None:
    state = create_initial_state("Build a calculator")
    payload = requirements.model_dump()
    incoming = {"requirements": payload, "status": WorkflowStatus.RUNNING, "current_agent": "requirements", "events": [event(state)]}
    update = validate_update(state, incoming)
    assert set(update) == set(incoming)
    assert "run_id" not in update
    assert state["requirements"] is None
    assert state["events"] == []
    payload["constraints"].append("caller mutation")
    assert update["requirements"]["constraints"] == ["Python standard library only"]


def test_sequential_reads_and_real_langgraph_merges(requirements: RequirementsOutput, design: DesignOutput, code: CodingOutput) -> None:
    seen: list[str] = []

    def producer(state: WorkflowState) -> WorkflowUpdate:
        return validate_update(state, {"requirements": requirements.model_dump(), "status": "running", "current_agent": "requirements", "events": [event(state)]})

    def consumer(state: WorkflowState) -> WorkflowUpdate:
        req = RequirementsOutput.model_validate(state["requirements"])
        seen.append(req.project_name)
        assert state["design"] is None
        return validate_update(state, {"design": design.model_dump(), "current_agent": "design", "events": [event(state, "Design read requirements")]})

    def finish(state: WorkflowState) -> WorkflowUpdate:
        assert RequirementsOutput.model_validate(state["requirements"]).project_name == "Calculator"
        assert DesignOutput.model_validate(state["design"]).preserved_constraints == requirements.constraints
        return validate_update(state, {"code": code.model_dump(), "status": "completed", "current_agent": "coding", "events": [event(state, "Final output stored")]})

    builder = StateGraph(WorkflowState)
    builder.add_node("producer", producer)
    builder.add_node("consumer", consumer)
    builder.add_node("finish", finish)
    builder.add_edge(START, "producer")
    builder.add_edge("producer", "consumer")
    builder.add_edge("consumer", "finish")
    builder.add_edge("finish", END)
    graph = builder.compile()
    first = graph.invoke(create_initial_state("Build calculator A"))
    second = graph.invoke(create_initial_state("Build calculator B"))
    assert seen == ["Calculator", "Calculator"]
    assert first["status"] == WorkflowStatus.COMPLETED
    assert CodingOutput.model_validate(first["code"]).files[0].path == "calculator.py"
    assert len(first["events"]) == len(second["events"]) == 3
    assert first["run_id"] != second["run_id"]
    assert all(item.run_id == first["run_id"] for item in first["events"])
    first["requirements"]["constraints"].append("mutation in run A")
    first["events"].clear()
    assert len(second["events"]) == 3
    assert second["requirements"]["constraints"] == requirements.constraints


def test_run_containers_are_isolated() -> None:
    first, second = create_initial_state("Same request"), create_initial_state("Same request")
    assert first["run_id"] != second["run_id"]
    first["events"].append(event(first))
    assert second["events"] == []


def test_event_merge_and_replay() -> None:
    state = create_initial_state("Build calculator")
    first, second = event(state, "First"), event(state, "Second")
    left, right = [first], [second, first]
    merged = merge_events(left, right)
    assert [item.event_id for item in merged] == [first.event_id, second.event_id]
    assert left == [first] and right == [second, first]
    replay = first.model_copy(update={"timestamp": first.timestamp + timedelta(seconds=10)})
    assert merge_events(merged, [replay])[0].timestamp == first.timestamp
    assert len(merge_events(merged, [replay])) == 2


def test_conflicting_event_id_rejected() -> None:
    first = event(create_initial_state("Build calculator"))
    conflicting = first.model_copy(update={"message": "Different logical event"})
    with pytest.raises(EventConflictError):
        merge_events([first], [conflicting])


def test_cross_run_event_rejected() -> None:
    first, second = create_initial_state("First request"), create_initial_state("Second request")
    with pytest.raises(WorkflowStateError):
        validate_update(first, {"events": [event(second)]})
    with pytest.raises(WorkflowStateError):
        merge_events([event(first)], [event(second)])


def test_event_round_trip_and_timestamp() -> None:
    item = event(create_initial_state("Build calculator"))
    assert item.timestamp.utcoffset() == timedelta(0)
    assert WorkflowEvent.model_validate_json(item.model_dump_json()) == item
    with pytest.raises(ValidationError):
        item.message = "not mutable"
    with pytest.raises(ValidationError):
        WorkflowEvent(run_id=item.run_id, agent_name="requirements", event_type="info", message="ok", status="running", timestamp=datetime(2026, 10, 8))


def test_parallel_event_channels() -> None:
    state = create_initial_state("Build calculator")
    first, second = event(state, "A"), event(state, "B")

    def branch_a(current: WorkflowState) -> WorkflowUpdate:
        return validate_update(current, {"events": [first]})

    def branch_b(current: WorkflowState) -> WorkflowUpdate:
        return validate_update(current, {"events": [second, first]})

    builder = StateGraph(WorkflowState)
    builder.add_node("a", branch_a)
    builder.add_node("b", branch_b)
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    builder.add_edge("a", END)
    builder.add_edge("b", END)
    result = builder.compile().invoke(state)
    assert len(result["events"]) == 2
    assert {item.event_id for item in result["events"]} == {first.event_id, second.event_id}


def test_only_incoming_events_returned_in_partial_update() -> None:
    state = create_initial_state("Build calculator")
    first, second = event(state, "A"), event(state, "B")
    state["events"] = [first]
    update = validate_update(state, {"events": [second]})
    assert update["events"] == [second]
    assert merge_events(state["events"], update["events"]) == [first, second]


@pytest.mark.parametrize("field", ["requirements", "design", "code"])
def test_invalid_payload_rejected(field: str) -> None:
    state = create_initial_state("Build calculator")
    with pytest.raises(WorkflowStateError):
        validate_update(state, {field: {"unexpected": "value"}})


def test_raw_node_payload_still_validated_by_channel() -> None:
    def invalid_node(state: WorkflowState) -> dict[str, object]:
        return {"requirements": {"unexpected": "value"}}
    builder = StateGraph(WorkflowState)
    builder.add_node("invalid", invalid_node)
    builder.add_edge(START, "invalid")
    builder.add_edge("invalid", END)
    with pytest.raises(WorkflowStateError):
        builder.compile().invoke(create_initial_state("Build calculator"))


@pytest.mark.parametrize("field", ["run_id", "user_request", "DEEPSEEK_API_KEY", "api_key"])
def test_unknown_or_immutable_update_field(field: str) -> None:
    with pytest.raises(WorkflowStateError):
        validate_update(create_initial_state("Build calculator"), {field: "must-not-appear-in-errors"})


def test_raw_graph_cannot_change_identity() -> None:
    def bad(state: WorkflowState) -> dict[str, object]:
        return {"run_id": str(uuid4())}
    builder = StateGraph(WorkflowState)
    builder.add_node("bad", bad)
    builder.add_edge(START, "bad")
    builder.add_edge("bad", END)
    with pytest.raises(WorkflowStateError):
        builder.compile().invoke(create_initial_state("Build calculator"))


def test_failure_update_does_not_disclose_raw_exception() -> None:
    state = create_initial_state("Build calculator")
    update = failure_update(state, "requirements", RuntimeError("test-secret-must-not-be-recorded"))
    assert update["status"] == WorkflowStatus.FAILED
    assert update["current_agent"] == "requirements"
    assert update["error"] == "Agent execution failed (RuntimeError)."
    assert update["events"][0].event_type == "failed"
    assert "test-secret-must-not-be-recorded" not in str(update)
    assert state["status"] == WorkflowStatus.PENDING


@pytest.mark.parametrize("update", [
    {"status": "unknown"}, {"status": "completed"}, {"status": "failed"},
    {"error": "error without failure status"},
])
def test_invalid_status_or_error_state(update: dict[str, object]) -> None:
    with pytest.raises(WorkflowStateError):
        validate_update(create_initial_state("Build calculator"), update)


def test_terminal_status_cannot_restart() -> None:
    initial = create_initial_state("Build calculator")
    failed = validate_state({**initial, **failure_update(initial, "requirements", RuntimeError("private"))})
    with pytest.raises(WorkflowStateError):
        validate_update(failed, {"status": "running", "error": None})


def test_design_cannot_skip_requirements(design: DesignOutput) -> None:
    with pytest.raises(WorkflowStateError):
        validate_update(create_initial_state("Build calculator"), {"design": design.model_dump()})


def test_mismatched_design_constraints(requirements: RequirementsOutput, design: DesignOutput) -> None:
    initial = create_initial_state("Build calculator")
    update = validate_update(initial, {"requirements": requirements.model_dump()})
    state = validate_state({**initial, **update})
    design.preserved_constraints.clear()
    with pytest.raises(WorkflowStateError):
        validate_update(state, {"design": design.model_dump()})


def test_code_cannot_skip_previous_stages(code: CodingOutput) -> None:
    with pytest.raises(WorkflowStateError):
        validate_update(create_initial_state("Build calculator"), {"code": code.model_dump()})


def test_full_state_rejects_secret_config_keys() -> None:
    state = create_initial_state("Build calculator")
    with pytest.raises(WorkflowStateError) as info:
        validate_state({**state, "DEEPSEEK_API_KEY": "test-secret"})
    assert "test-secret" not in str(info.value)
