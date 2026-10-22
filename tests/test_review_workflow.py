"""Controlled Mock Agents execute production conditional LangGraph; no API."""

import asyncio
from unittest.mock import MagicMock

import pytest
from streamlit.testing.v1 import AppTest

from workflow_studio.state import RequirementsOutput, DesignOutput, ModuleDesign, CodingOutput, CodeFile
from workflow_studio.state.review import ReviewOutput, ReviewStatus, merge_review_history
from workflow_studio.state.workflow import WorkflowStateError, create_initial_state, validate_state
from workflow_studio.workflows import build_workflow, run_workflow, arun_workflow, stream_workflow, stream_workflow_events
from workflow_studio.workflows.routing import route_after_review
from workflow_studio.workflows.runner import WorkflowInputError
from workflow_studio.web.controller import PageState

REQUEST = "实现仅使用 Python 标准库的整数加法工具，提供运行说明。"


def verdict(passed: bool) -> ReviewOutput:
    return ReviewOutput(passed=passed, score=95 if passed else 40, issues=[] if passed else ["calculator.py 的 add 实现减法，没有覆盖加法需求"], suggestions=[] if passed else ["将减号改为加号，保留原接口"], summary="LLM 静态审查意见；没有运行测试，不能保证代码完全正确。")


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    req = RequirementsOutput(project_name="Calculator", summary="Integer addition", functional_requirements=["Add two integers"], non_functional_requirements=[], constraints=["Python standard library"], assumptions=[], acceptance_criteria=["2 + 3 = 5"])
    design = DesignOutput(architecture_overview="Single module", technology_stack=["Python"], modules=[ModuleDesign(name="calculator", responsibility="Add integers", dependencies=[], requirement_ids=["FR-1"])], data_models=[], interfaces=["add(a, b)"], project_structure="calculator.py", design_decisions=["Keep simple"], preserved_constraints=req.constraints)
    code = CodingOutput(files=[CodeFile(path="calculator.py", language="python", content="def add(a: int, b: int) -> int:\n    return a + b\n", description="Addition")], instructions="手工调用 add；未运行代码", dependencies=[], test_suggestions=["2+3=5"], limitations=["未执行测试"])
    mocks = {}
    for name, method, output in [("requirements", "analyze_requirements", req), ("design", "design_system", design), ("coding", "generate_code", code), ("review", "review_code", verdict(True))]:
        mocks[name] = MagicMock(return_value=output)
        monkeypatch.setattr(f"workflow_studio.workflows.nodes.{method}", mocks[name])
    return mocks


def test_standard_mode_keeps_exact_three_node_path(agents: dict[str, MagicMock]) -> None:
    result = run_workflow(REQUEST)
    assert result.status == "completed" and not result.review_enabled
    assert result.review_status == "disabled" and result.review is None
    assert result.revision_count == 0 and result.review_history == []
    assert [e.agent_name for e in result.events[::2]] == ["requirements_agent", "design_agent", "coding_agent"]
    assert "review_agent" not in build_workflow().get_graph().nodes
    agents["review"].assert_not_called()


def test_review_consumes_all_upstream_outputs_once(agents: dict[str, MagicMock]) -> None:
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "completed" and result.review_status == ReviewStatus.PASSED
    assert result.review.passed and result.revision_count == 0
    assert len(result.review_history) == 1 and len(result.events) == 8
    agents["review"].assert_called_once_with(agents["requirements"].return_value, agents["design"].return_value, agents["coding"].return_value)
    assert result.events[5].status == "running"
    assert result.events[7].status == "completed"


def test_failure_repair_pass_and_previous_code_history(agents: dict[str, MagicMock]) -> None:
    good = agents["coding"].return_value
    bad = good.model_copy(deep=True)
    bad.files[0].content = "def add(a: int, b: int) -> int:\n    return a - b\n"
    rejected, approved = verdict(False), verdict(True)
    agents["coding"].side_effect = [bad, good]
    agents["review"].side_effect = [rejected, approved]
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "completed" and result.review.passed
    assert result.revision_count == 1 and len(result.events) == 12
    assert [item.revision_count for item in result.review_history] == [0, 1]
    assert [item.review.passed for item in result.review_history] == [False, True]
    assert result.review_history[0].code == bad and result.review_history[1].code == good
    assert result.code == good
    assert agents["coding"].call_args_list[0].kwargs == {}
    assert agents["coding"].call_args_list[1].kwargs == {"review": rejected, "previous_code": bad}
    assert agents["review"].call_args_list[0].args[2] == bad
    assert agents["review"].call_args_list[1].args[2] == good
    repair_event = result.events[8]
    assert repair_event.revision_count == 1 and repair_event.input_fields == ("requirements", "design", "code", "review")
    assert result.events[-1].revision_count == 1
    assert len(merge_review_history(result.review_history, result.review_history)) == 2


@pytest.mark.parametrize("budget", [0, 1, 2, 3])
def test_budget_exhausted_is_issues_not_pass_or_exception(budget: int, agents: dict[str, MagicMock]) -> None:
    agents["review"].return_value = verdict(False)
    result = run_workflow(REQUEST, review_enabled=True, max_revisions=budget)
    assert result.status == "completed_with_issues"
    assert result.review_status == ReviewStatus.FAILED and not result.review.passed
    assert result.error is None and result.revision_count == budget
    assert len(result.review_history) == budget + 1
    assert agents["coding"].call_count == agents["review"].call_count == budget + 1
    assert agents["requirements"].call_count == agents["design"].call_count == 1
    assert len(result.events) == 8 + budget * 4
    assert all(e.status != "completed" for e in result.events)
    assert result.events[-1].event_type == "completed"
    assert result.events[-1].status == "completed_with_issues"


@pytest.mark.parametrize("after_revision", [False, True])
def test_review_api_error_is_exception_not_verdict(after_revision: bool, agents: dict[str, MagicMock]) -> None:
    from workflow_studio.llm.errors import LLMNetworkError
    failure = LLMNetworkError("private-provider-response")
    agents["review"].side_effect = [verdict(False), failure] if after_revision else failure
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "failed" and result.review_status == ReviewStatus.ERROR
    assert result.events[-1].event_type == "failed" and result.events[-1].agent_name == "review_agent"
    assert "LLMNetworkError" in result.error and "private-provider-response" not in result.error
    assert result.revision_count == int(after_revision)
    assert len(result.review_history) == int(after_revision)
    assert agents["coding"].call_count == 1 + int(after_revision)
    assert result.review is None if not after_revision else not result.review.passed


def test_repair_api_failure_counts_attempt_and_preserves_review(agents: dict[str, MagicMock]) -> None:
    agents["review"].return_value = verdict(False)
    agents["coding"].side_effect = [agents["coding"].return_value, RuntimeError("private-error")]
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "failed" and result.revision_count == 1
    assert len(result.review_history) == 1 and not result.review.passed
    assert result.events[-1].agent_name == "coding_agent"
    assert result.events[-1].revision_count == 1
    agents["review"].assert_called_once()


def test_invalid_review_output_stops_without_retry_loop(agents: dict[str, MagicMock]) -> None:
    agents["review"].return_value = None
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "failed" and result.review_status == "error"
    assert result.review is None and not result.review_history
    agents["coding"].assert_called_once()


def test_review_stream_is_not_prematurely_completed(agents: dict[str, MagicMock]) -> None:
    agents["review"].side_effect = [verdict(False), verdict(True)]
    states = list(stream_workflow(REQUEST, review_enabled=True))
    assert [state["status"] for state in states] == ["pending", "running", "running", "running", "running", "running", "completed"]
    assert [len(state["review_history"]) for state in states] == [0, 0, 0, 0, 1, 1, 2]
    assert route_after_review(states[4]) == "coding_agent"
    assert route_after_review(states[-1]) == "__end__"
    assert states[4]["revision_count"] == 0 and states[5]["revision_count"] == 1
    with pytest.raises(WorkflowStateError):
        validate_state({**states[4], "status": "completed"})


def test_custom_event_ids_and_attempts_remain_distinct(agents: dict[str, MagicMock]) -> None:
    agents["review"].side_effect = [verdict(False), verdict(True)]
    packets = list(stream_workflow_events(REQUEST, review_enabled=True))
    events = [packet.event for packet in packets if packet.event]
    assert len(events) == len({event.event_id for event in events}) == 12
    assert [(e.agent_name, e.revision_count) for e in events[::2]] == [("requirements_agent", 0), ("design_agent", 0), ("coding_agent", 0), ("review_agent", 0), ("coding_agent", 1), ("review_agent", 1)]
    assert packets[-1].state["status"] == "completed"


def test_async_review_and_run_isolation(agents: dict[str, MagicMock]) -> None:
    result = asyncio.run(arun_workflow(REQUEST, review_enabled=True))
    graph = build_workflow(review_enabled=True)
    state = graph.invoke(create_initial_state(REQUEST, review_enabled=True))
    assert result.status == state["status"] == "completed"
    assert result.run_id != state["run_id"]
    assert result.review_history[0].record_id != state["review_history"][0].record_id
    assert result.review_history[0].run_id == result.run_id
    standard = run_workflow(REQUEST)
    assert standard.review_history == [] and standard.review is None


@pytest.mark.parametrize("enabled,budget", [(1, 1), (True, -1), (True, 4), (True, True), (True, 1.5)])
def test_invalid_options_rejected_before_agents(enabled: object, budget: object, agents: dict[str, MagicMock]) -> None:
    with pytest.raises(WorkflowInputError):
        run_workflow(REQUEST, review_enabled=enabled, max_revisions=budget)
    for mock in agents.values():
        mock.assert_not_called()


def test_conditional_graph_edges(agents: dict[str, MagicMock]) -> None:
    graph = build_workflow(review_enabled=True).get_graph()
    edges = {(edge.source, edge.target): edge for edge in graph.edges}
    assert edges[("coding_agent", "review_agent")].conditional
    assert edges[("review_agent", "coding_agent")].conditional
    assert edges[("review_agent", "__end__")].conditional


def test_verdict_not_score_controls_route(agents: dict[str, MagicMock]) -> None:
    agents["review"].return_value = verdict(False).model_copy(update={"score": 99})
    result = run_workflow(REQUEST, review_enabled=True, max_revisions=0)
    assert result.status == "completed_with_issues" and not result.review.passed


def test_review_history_rejects_tampering(agents: dict[str, MagicMock]) -> None:
    from pydantic import ValidationError
    from workflow_studio.state.result import WorkflowResult
    result = run_workflow(REQUEST, review_enabled=True)
    tampered = result.model_dump()
    tampered["review"]["passed"] = False
    tampered["review"]["issues"] = ["changed verdict"]
    with pytest.raises(ValidationError):
        WorkflowResult.model_validate(tampered)
    entry = result.review_history[0]
    from uuid import uuid4
    with pytest.raises(ValueError, match="duplicate_review_revision"):
        merge_review_history([entry], [entry.model_copy(update={"record_id": str(uuid4())})])


def _web(agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch) -> AppTest:
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from workflow_studio.config import Settings
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="mock-only-key", deepseek_model="mock-model"))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10).run()
    at.checkbox(key="review_enabled").check().run()
    return at


def _submit_web(at: AppTest) -> "PageState":
    from workflow_studio.web.app import SESSION_KEY
    at.text_area(key="request_input").input(REQUEST).run()
    at.button(key="start_workflow").click().run()
    page = at.session_state[SESSION_KEY]
    assert page.task and page.task.thread
    page.task.thread.join(timeout=10)
    assert not page.task.thread.is_alive()
    at.run()
    assert not at.exception
    return at.session_state[SESSION_KEY]


def test_web_review_repair_history_and_rerun_safety(agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    agents["review"].side_effect = [verdict(False), verdict(True)]
    at = _web(agents, monkeypatch)
    assert not at.number_input(key="max_revisions").disabled
    page = _submit_web(at)
    assert page.result.status == "completed" and len(page.result.review_history) == 2
    assert [tab.label for tab in at.tabs] == ["需求分析", "系统设计", "代码生成", "代码审查", "执行日志"]
    assert [metric.value for metric in at.metric] == ["已完成"] * 4
    assert any("没有执行生成代码或测试" in item.value for item in at.info)
    assert len(page.events) == 12
    for entry in page.result.review_history:
        at.checkbox(key=f"review_code_{entry.record_id}").check().run()
    at.selectbox(key="trace_agent_choice").select("review_agent").run()
    attempts = [item for item in at.selectbox if item.label == "选择执行轮次"]
    assert len(attempts) == 1
    assert len(attempts[0].options) == 2
    at.run()
    assert not at.exception
    assert agents["review"].call_count == agents["coding"].call_count == 2
    assert "代码审查" in page.exports["report"].decode("utf-8")
    assert "审查轮次 1" in page.exports["report"].decode("utf-8")


def test_web_exhaustion_is_warning_not_pass(agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    agents["review"].return_value = verdict(False)
    at = _web(agents, monkeypatch)
    at.number_input(key="max_revisions").set_value(0).run()
    page = _submit_web(at)
    assert page.result.status == "completed_with_issues" and page.error is None
    assert any("审查仍未通过" in item.value for item in at.warning)
    assert not any("审查通过" in item.value for item in at.success)
    assert not at.button(key="start_workflow").disabled
    agents["coding"].assert_called_once()
    assert "已完成但审查未通过" in page.exports["report"].decode("utf-8")


def test_web_review_exception_is_error(agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    from workflow_studio.llm.errors import LLMNetworkError
    agents["review"].side_effect = LLMNetworkError("private-key-provider-error")
    at = _web(agents, monkeypatch)
    page = _submit_web(at)
    assert page.result.status == "failed" and page.result.review_status == "error"
    assert [metric.value for metric in at.metric][-1] == "执行失败"
    assert any("网络" in item.value for item in at.error)
    assert "private-key-provider-error" not in str(at)
    agents["coding"].assert_called_once()


def test_web_mode_controls_disabled_during_review(agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Event
    from workflow_studio.web.app import SESSION_KEY
    entered, release = Event(), Event()
    def blocked(*args: object) -> ReviewOutput:
        entered.set()
        assert release.wait(timeout=10)
        return verdict(True)
    agents["review"].side_effect = blocked
    at = _web(agents, monkeypatch)
    try:
        at.text_area(key="request_input").input(REQUEST).run()
        at.button(key="start_workflow").click().run()
        assert entered.wait(timeout=10)
        at.run()
        assert at.checkbox(key="review_enabled").disabled
        assert at.number_input(key="max_revisions").disabled
        assert at.button(key="start_workflow").disabled
    finally:
        release.set()
        page = at.session_state[SESSION_KEY]
        page.task.thread.join(timeout=10)
    at.run()
    assert not at.exception
    agents["review"].assert_called_once()
