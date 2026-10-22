"""Mock Agent tests execute the production compiled LangGraph end to end."""

import asyncio
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from workflow_studio.state import (
    CodeFile, CodingOutput, DesignOutput, ModuleDesign, RequirementsOutput,
    WorkflowResult, WorkflowStatus, create_initial_state,
)
from workflow_studio.workflows import arun_workflow, build_workflow, run_workflow, stream_workflow
from workflow_studio.workflows.nodes import WorkflowNodeError, design_agent_node
from workflow_studio.workflows.runner import WorkflowInputError
from workflow_studio.workflows.integration_test import main as integration_main

REQUEST = "Build a local standard-library Python integer addition tool"


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    req = RequirementsOutput(
        project_name="Calculator", summary="Add integers", functional_requirements=["Add two integers"],
        non_functional_requirements=[], constraints=["Python standard library"], assumptions=[], acceptance_criteria=["2 + 3 = 5"],
    )
    design = DesignOutput(
        architecture_overview="Single module", technology_stack=["Python"],
        modules=[ModuleDesign(name="calculator", responsibility="Add integers", dependencies=[], requirement_ids=["FR-1"])],
        data_models=[], interfaces=["Two integers in, integer out"], project_structure="calculator.py", design_decisions=["Simple local design"], preserved_constraints=list(req.constraints),
    )
    code = CodingOutput(
        files=[CodeFile(path="calculator.py", language="python", content="def add(a: int, b: int) -> int:\n    return a + b\n", description="Addition")],
        instructions="Run manually", dependencies=[], test_suggestions=["Check addition"], limitations=["Not executed"],
    )
    mocks = {"requirements": MagicMock(return_value=req), "design": MagicMock(return_value=design), "coding": MagicMock(return_value=code)}
    for name, target in [("requirements", "analyze_requirements"), ("design", "design_system"), ("coding", "generate_code")]:
        monkeypatch.setattr(f"workflow_studio.workflows.nodes.{target}", mocks[name])
    return mocks


def test_graph_structure(agents: dict[str, MagicMock]) -> None:
    diagram = build_workflow().get_graph()
    assert {"requirements_agent", "design_agent", "coding_agent"} <= set(diagram.nodes)
    assert {(edge.source, edge.target) for edge in diagram.edges} == {
        ("__start__", "requirements_agent"), ("requirements_agent", "design_agent"),
        ("design_agent", "coding_agent"), ("coding_agent", "__end__"),
    }


def test_order_inputs_and_final_result(agents: dict[str, MagicMock]) -> None:
    parent = MagicMock()
    for name, mock in agents.items():
        parent.attach_mock(mock, name)
    result = run_workflow(REQUEST)
    assert [call[0] for call in parent.mock_calls] == ["requirements", "design", "coding"]
    agents["requirements"].assert_called_once_with(REQUEST)
    agents["design"].assert_called_once_with(agents["requirements"].return_value)
    agents["coding"].assert_called_once_with(agents["requirements"].return_value, agents["design"].return_value)
    assert isinstance(result, WorkflowResult)
    assert result.status == WorkflowStatus.COMPLETED
    assert result.error is None
    assert result.requirements is not None and result.design is not None and result.code is not None
    assert len(result.events) == 6
    assert [event.event_type for event in result.events] == ["started", "completed"] * 3
    assert [event.agent_name for event in result.events[::2]] == ["requirements_agent", "design_agent", "coding_agent"]
    assert result.events[-1].status == WorkflowStatus.COMPLETED
    assert WorkflowResult.model_validate_json(result.model_dump_json()).status == WorkflowStatus.COMPLETED


@pytest.mark.parametrize("stage", ["requirements", "design", "coding"])
def test_failure_stops_downstream_and_never_completes(stage: str, agents: dict[str, MagicMock]) -> None:
    agents[stage].side_effect = RuntimeError("private-provider-body-or-key")
    result = run_workflow(REQUEST)
    assert result.status == WorkflowStatus.FAILED
    assert result.code is None
    assert result.events[-1].event_type == "failed"
    assert result.events[-1].agent_name == f"{stage}_agent"
    assert result.error == "Agent execution failed (RuntimeError)."
    assert "private-provider-body-or-key" not in result.model_dump_json()
    names = ["requirements", "design", "coding"]
    index = names.index(stage)
    assert len(result.events) == index * 2 + 2
    for name in names[index + 1:]:
        agents[name].assert_not_called()
    if stage != "requirements":
        assert result.requirements is not None
    if stage == "coding":
        assert result.design is not None


def test_raw_compiled_graph_aborts_on_failure(agents: dict[str, MagicMock]) -> None:
    agents["design"].side_effect = RuntimeError("private")
    with pytest.raises(WorkflowNodeError) as info:
        build_workflow().invoke(create_initial_state(REQUEST))
    assert info.value.state["status"] == WorkflowStatus.FAILED
    assert info.value.state["requirements"] is not None
    agents["coding"].assert_not_called()


@pytest.mark.parametrize("stage", ["requirements", "design", "coding"])
def test_invalid_agent_output_is_failure(stage: str, agents: dict[str, MagicMock]) -> None:
    agents[stage].return_value = None
    result = run_workflow(REQUEST)
    assert result.status == WorkflowStatus.FAILED
    assert result.code is None
    if stage != "coding":
        agents["coding"].assert_not_called()


def test_bad_design_constraints_stop_coding(agents: dict[str, MagicMock]) -> None:
    agents["design"].return_value.preserved_constraints.clear()
    assert run_workflow(REQUEST).status == WorkflowStatus.FAILED
    agents["coding"].assert_not_called()


def test_node_checks_missing_upstream_before_agent(agents: dict[str, MagicMock]) -> None:
    with pytest.raises(WorkflowNodeError) as info:
        design_agent_node(create_initial_state(REQUEST))
    assert info.value.state["status"] == WorkflowStatus.FAILED
    agents["design"].assert_not_called()


@pytest.mark.parametrize("text", ["", "   ", "short", None, 42])
def test_invalid_input_before_agents(text: object, agents: dict[str, MagicMock]) -> None:
    with pytest.raises(WorkflowInputError):
        run_workflow(text)  # type: ignore[arg-type]
    for mock in agents.values():
        mock.assert_not_called()


def test_failure_does_not_affect_next_run(agents: dict[str, MagicMock]) -> None:
    req = agents["requirements"].return_value
    agents["requirements"].side_effect = [RuntimeError("first fails"), req]
    failed = run_workflow(REQUEST)
    success = run_workflow(REQUEST)
    assert failed.run_id != success.run_id
    assert failed.status == WorkflowStatus.FAILED
    assert success.status == WorkflowStatus.COMPLETED
    assert success.error is None
    assert all(item.run_id == success.run_id for item in success.events)


def test_reused_compiled_graph_run_isolation(agents: dict[str, MagicMock]) -> None:
    graph = build_workflow()
    first = graph.invoke(create_initial_state(REQUEST))
    second = graph.invoke(create_initial_state(REQUEST))
    assert first["run_id"] != second["run_id"]
    first["requirements"]["constraints"].append("mutation")
    first["events"].clear()
    assert len(second["events"]) == 6
    assert second["requirements"]["constraints"] == ["Python standard library"]


def test_async_invoke(agents: dict[str, MagicMock]) -> None:
    result = asyncio.run(arun_workflow(REQUEST))
    assert result.status == WorkflowStatus.COMPLETED
    agents["coding"].assert_called_once()


def test_async_failure(agents: dict[str, MagicMock]) -> None:
    agents["design"].side_effect = RuntimeError("private")
    result = asyncio.run(arun_workflow(REQUEST))
    assert result.status == WorkflowStatus.FAILED
    agents["coding"].assert_not_called()


def test_stream_snapshots(agents: dict[str, MagicMock]) -> None:
    snapshots = list(stream_workflow(REQUEST))
    assert [state["status"] for state in snapshots] == ["pending", "running", "running", "completed"]
    assert [len(state["events"]) for state in snapshots] == [0, 2, 4, 6]
    assert snapshots[1]["design"] is None
    assert snapshots[2]["code"] is None


def test_stream_failure_is_terminal(agents: dict[str, MagicMock]) -> None:
    agents["design"].side_effect = RuntimeError("private")
    snapshots = list(stream_workflow(REQUEST))
    assert snapshots[-1]["status"] == WorkflowStatus.FAILED
    assert not any(state["status"] == WorkflowStatus.COMPLETED for state in snapshots)
    assert len(snapshots[-1]["events"]) == 4
    agents["coding"].assert_not_called()


def test_immediate_custom_event_stream(agents: dict[str, MagicMock]) -> None:
    chunks = list(build_workflow().stream(create_initial_state(REQUEST), stream_mode=["custom", "values"]))
    events = [data for mode, data in chunks if mode == "custom"]
    assert [item["event_type"] for item in events] == ["started", "completed"] * 3
    assert events[0]["agent_name"] == "requirements_agent"
    assert events[-1]["status"] == "completed"


def test_result_never_accepts_pending(agents: dict[str, MagicMock]) -> None:
    initial = create_initial_state(REQUEST)
    with pytest.raises(ValidationError):
        WorkflowResult.from_state(initial)


def test_real_integration_requires_flag(agents: dict[str, MagicMock]) -> None:
    with pytest.raises(SystemExit) as info:
        integration_main([])
    assert info.value.code == 2
    for mock in agents.values():
        mock.assert_not_called()


def test_integration_cli_mocked_success(agents: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]) -> None:
    assert integration_main(["--run"]) == 0
    assert "Integration test passed" in capsys.readouterr().out


def test_integration_cli_mocked_failure(agents: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]) -> None:
    agents["coding"].side_effect = RuntimeError("private")
    assert integration_main(["--run"]) == 1
    assert "status=failed" in capsys.readouterr().out
