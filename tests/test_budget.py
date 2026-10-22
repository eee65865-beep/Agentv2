"""Offline admission, SDK retry, partial-result and cooperative stop contracts."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from workflow_studio.budget import BudgetSnapshot, BudgetTracker, RunBudget, RunStopped, budget_scope, current_budget, invoke_model, model_call
from workflow_studio.config import Settings
from workflow_studio.exporters.documents import workflow_report
from workflow_studio.llm.deepseek_client import get_llm
from workflow_studio.llm.errors import LLMStructuredOutputUnsupported
from workflow_studio.llm.retry_policy import DeepSeekRetryClient
from workflow_studio.agents.requirements_agent import analyze_requirements
from workflow_studio.state import CodeFile, CodingOutput, DesignOutput, ModuleDesign, RequirementsOutput, WorkflowResult
from workflow_studio.state.review import ReviewAssessmentV2
from workflow_studio.state.review_policy import bind_review
from workflow_studio.state.workflow import WorkflowStateError, create_initial_state, validate_state, validate_update
from workflow_studio.storage import RunStore
from workflow_studio.web.app import SESSION_KEY
from workflow_studio.web.controller import request_stop
from workflow_studio.workflows import arun_workflow, run_workflow, stream_workflow, stream_workflow_events
from workflow_studio.workflows.runner import WorkflowInputError

REQUEST = "实现标准库 Python 整数加法工具，提供运行说明。"


@pytest.mark.parametrize("field,value", [
    ("max_logical_calls", -1), ("max_logical_calls", True), ("max_http_attempts", "3"),
    ("max_format_repairs", 1.5), ("max_node_executions", False), ("max_elapsed_seconds", 0),
    ("max_elapsed_seconds", True), ("max_elapsed_seconds", float("nan")), ("max_elapsed_seconds", float("inf")),
    ("max_total_tokens", -1), ("tokens_reserved_per_call", 0), ("max_repeated_reviews", 1),
])
def test_limits_reject_invalid_values(field, value):
    with pytest.raises(ValidationError):
        RunBudget(**{field: value})


def test_reservation_counts_known_and_unknown_without_zero_filling():
    tracker = BudgetTracker(RunBudget(max_total_tokens=25, tokens_reserved_per_call=10))
    client = MagicMock()
    client.invoke.side_effect = [AIMessage(content="ok", usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}), AIMessage(content="unknown"), AIMessage(content="unknown")]
    with budget_scope(tracker):
        invoke_model(client, "p")
        invoke_model(client, "p")
        invoke_model(client, "p")
        with pytest.raises(RunStopped, match="tokens"):
            invoke_model(client, "p")
    usage = tracker.snapshot()
    assert usage.logical_calls == 3 and usage.known_tokens == 5
    assert usage.unknown_usage_calls == 2 and usage.unknown_tokens_reserved == 20
    assert usage.in_flight_calls == 0
    assert client.invoke.call_count == 3


def test_shared_client_and_agent_wrapper_do_not_double_count():
    tracker = BudgetTracker(RunBudget(max_logical_calls=1))
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as sdk:
        sdk.return_value.invoke.return_value = AIMessage(content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        client = get_llm(Settings(deepseek_api_key="test-only"))
        with budget_scope(tracker):
            assert invoke_model(client, "p").content == "ok"
            with pytest.raises(RunStopped):
                client.invoke("p")
        assert sdk.return_value.invoke.call_count == 1
    assert tracker.snapshot().logical_calls == 1 and tracker.snapshot().known_tokens == 2


def test_format_limit_stops_before_extra_model_call(monkeypatch):
    client = MagicMock()
    client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    client.invoke.return_value = AIMessage(content="private-invalid-response")
    monkeypatch.setattr("workflow_studio.agents.requirements_agent.get_llm", lambda: client)
    tracker = BudgetTracker(RunBudget(max_format_repairs=0))
    with budget_scope(tracker), pytest.raises(RunStopped, match="format_repairs"):
        analyze_requirements(REQUEST)
    assert tracker.snapshot().logical_calls == 2  # Native attempt + JSON fallback.
    assert tracker.snapshot().format_repairs == 0
    assert client.invoke.call_count == 1


def test_real_sdk_retries_consume_http_budget_and_preserve_stop_type(monkeypatch):
    monkeypatch.setattr("openai._base_client.time.sleep", lambda _: None)
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx2.Response(429, json={"error": {"message": "private-body"}})
    tracker = BudgetTracker(RunBudget(max_http_attempts=2))
    with DeepSeekRetryClient(api_key="test-only", max_retries=5, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as sdk:
        with budget_scope(tracker), pytest.raises(RunStopped, match="http_attempts"):
            sdk.chat.completions.create(model="mock", messages=[])
    assert len(attempts) == tracker.snapshot().http_attempts == 2


def test_http_timeout_is_clamped_to_remaining_run_time():
    clock = [0.0]
    tracker = BudgetTracker(RunBudget(max_elapsed_seconds=10), clock=lambda: clock[0])
    clock[0] = 7.0
    with DeepSeekRetryClient(api_key="test-only", max_retries=0) as client:
        request = httpx2.Request("POST", "https://example.invalid", extensions={"timeout": {"connect": 60.0, "read": None, "write": 1.0, "pool": 20.0}})
        with budget_scope(tracker):
            client._prepare_request(request)
    assert request.extensions["timeout"] == {"connect": 3.0, "read": 3.0, "write": 1.0, "pool": 3.0}


def test_context_isolation_nested_errors_and_thread_admission():
    first, second = BudgetTracker(RunBudget(max_logical_calls=1)), BudgetTracker(RunBudget())
    assert current_budget() is None
    with budget_scope(first):
        with pytest.raises(RuntimeError):
            with budget_scope(second):
                assert current_budget() is second
                raise RuntimeError("test")
        assert current_budget() is first
    def worker(_):
        try:
            with budget_scope(first), model_call():
                return True
        except RunStopped:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(worker, range(4))) == 1
    assert current_budget() is None and second.snapshot().logical_calls == 0


@pytest.fixture
def outputs():
    req = RequirementsOutput(project_name="加法", summary="加法", functional_requirements=["相加"], non_functional_requirements=[], constraints=["标准库"], assumptions=[], acceptance_criteria=["2+3=5"])
    design = DesignOutput(architecture_overview="单模块", technology_stack=["Python"], modules=[ModuleDesign(name="calculator", responsibility="加法", dependencies=[], requirement_ids=["FR-1"])], data_models=[], interfaces=[], project_structure="calculator.py", design_decisions=["简单"], preserved_constraints=req.constraints)
    code = CodingOutput(files=[CodeFile(path="calculator.py", language="python", content="def add(a,b):\n    return a + b\n", description="加法")], instructions="手工运行", dependencies=[], test_suggestions=[], limitations=["未执行测试"])
    return req, design, code


@pytest.fixture
def agents(outputs, monkeypatch):
    mocks = {}
    for name, method, output in zip(["requirements", "design", "coding"], ["analyze_requirements", "design_system", "generate_code"], outputs):
        client = MagicMock()
        client.invoke_structured.return_value = output
        def action(*args, client=client, output=output, **kwargs):
            return invoke_model(client, "test", schema=type(output))
        mocks[name] = MagicMock(side_effect=action)
        monkeypatch.setattr(f"workflow_studio.workflows.nodes.{method}", mocks[name])
    return mocks


@pytest.mark.parametrize("api", [run_workflow, stream_workflow, stream_workflow_events])
def test_public_apis_stop_with_partial_results_and_no_context_leak(agents, api):
    result = api(REQUEST, budget=RunBudget(max_logical_calls=1))
    if api is not run_workflow:
        packets = []
        for packet in result:
            assert current_budget() is None
            packets.append(packet)
        result = WorkflowResult.from_state(packets[-1] if api is stream_workflow else packets[-1].state)
    assert result.status == "stopped" and result.error is None
    assert result.requirements is not None and result.design is result.code is None
    assert result.budget.stop_reason == "logical_calls" and result.budget.logical_calls == 1
    assert result.events[-1].event_type == "stopped"
    agents["coding"].assert_not_called()
    assert current_budget() is None


def test_async_uses_same_budget_and_resets_context(agents):
    result = asyncio.run(arun_workflow(REQUEST, budget=RunBudget(max_node_executions=1)))
    assert result.status == "stopped" and result.budget.node_executions == 1
    assert result.budget.stop_reason == "node_executions" and result.requirements is not None
    agents["design"].assert_not_called()
    assert current_budget() is None


def test_closing_stream_does_not_leak_budget(agents):
    stream = stream_workflow(REQUEST)
    next(stream)
    assert current_budget() is None
    stream.close()
    assert current_budget() is None


def test_cancel_before_start_does_not_execute_agent(agents):
    cancel = Event()
    cancel.set()
    result = run_workflow(REQUEST, cancel_event=cancel)
    assert result.status == "stopped" and result.budget.stop_reason == "cancelled"
    assert result.budget.node_executions == result.budget.logical_calls == 0
    assert result.requirements is None
    for agent in agents.values():
        agent.assert_not_called()


def test_deadline_after_valid_output_keeps_that_output(agents, outputs, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("workflow_studio.budget.monotonic", lambda: clock[0])
    def delayed(_):
        clock[0] = 2.0
        return outputs[0]
    agents["requirements"].side_effect = delayed
    result = run_workflow(REQUEST, budget=RunBudget(max_elapsed_seconds=1))
    assert result.status == "stopped" and result.budget.stop_reason == "deadline"
    assert result.requirements == outputs[0]
    agents["design"].assert_not_called()


def test_known_usage_can_exceed_reservation_but_stops_next_boundary(agents, outputs):
    def measured(_):
        client = MagicMock()
        client.invoke.return_value = AIMessage(content="ok", usage_metadata={"input_tokens": 8, "output_tokens": 8, "total_tokens": 16})
        invoke_model(client, "p")
        return outputs[0]
    agents["requirements"].side_effect = measured
    result = run_workflow(REQUEST, budget=RunBudget(max_total_tokens=10, tokens_reserved_per_call=10))
    assert result.status == "stopped" and result.budget.stop_reason == "tokens"
    assert result.budget.known_tokens == 16 and result.requirements == outputs[0]
    agents["design"].assert_not_called()


def test_budget_config_immutable_and_counters_monotonic():
    initial = create_initial_state(REQUEST)
    budget = BudgetTracker(RunBudget()).snapshot().model_dump(mode="json")
    current = validate_state({**initial, "budget": budget})
    budget["logical_calls"] = 1
    current = validate_state({**current, **validate_update(current, {"budget": budget})})
    for forged in (None, {**budget, "logical_calls": 0}, {**budget, "limits": RunBudget(max_logical_calls=99).model_dump()}):
        with pytest.raises(WorkflowStateError):
            validate_update(current, {"budget": forged})
    with pytest.raises(ValidationError):
        BudgetSnapshot.model_validate({**budget, "unknown_usage_calls": 1, "unknown_tokens_reserved": 0})


def test_stop_requires_explicit_reason_and_cannot_fake_success(agents):
    result = run_workflow(REQUEST, budget=RunBudget(max_node_executions=0))
    data = result.model_dump()
    data["budget"]["stop_reason"] = None
    with pytest.raises(ValidationError):
        WorkflowResult.model_validate(data)
    data["status"] = "completed"
    with pytest.raises(ValidationError):
        WorkflowResult.model_validate(data)


def test_new_run_is_independent_after_budget_stop(agents):
    stopped = run_workflow(REQUEST, budget=RunBudget(max_logical_calls=0))
    complete = run_workflow(REQUEST)
    assert stopped.status == "stopped" and complete.status == "completed"
    assert complete.budget.stop_reason is None and complete.budget.logical_calls == 3


def test_invalid_public_budget_is_rejected_before_agents(agents):
    with pytest.raises(WorkflowInputError):
        run_workflow(REQUEST, budget={"max_logical_calls": 1})
    with pytest.raises(WorkflowInputError):
        run_workflow(REQUEST, cancel_event=True)
    with pytest.raises(WorkflowInputError):
        run_workflow(REQUEST, budget=RunBudget.model_construct(max_logical_calls=-1))
    agents["requirements"].assert_not_called()


def test_stopped_storage_roundtrip_and_report(agents, tmp_path):
    result = run_workflow(REQUEST, budget=RunBudget(max_logical_calls=1))
    store = RunStore(tmp_path / "history")
    folder = store.save(result, REQUEST, "mock", 0)
    loaded = store.load(result.run_id)
    assert loaded.result == result and loaded.metadata.schema_version == 2
    assert loaded.metadata.budget == result.budget
    assert "模型逻辑调用预算不足" in workflow_report(result, REQUEST, "mock", 0)
    assert "已停止" in workflow_report(result, REQUEST, "mock", 0)
    assert store.save(result, REQUEST, "mock", 0) == folder


def test_legacy_metadata_without_budget_keeps_original_bytes(agents, tmp_path):
    result = run_workflow(REQUEST).model_copy(update={"budget": None})
    # Simulate an old envelope without reconstructing usage from optional events.
    store = RunStore(tmp_path / "history")
    folder = store.save(result, REQUEST, "mock", 0)
    path = folder / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.pop("budget")
    body = json.dumps(metadata).encode("utf-8")
    path.write_bytes(body)
    loaded = store.load(result.run_id).result
    assert loaded.budget is None and loaded == result
    assert store.save(loaded, REQUEST, "mock", 0) == folder
    assert path.read_bytes() == body


def test_repeated_review_stops_by_exact_details_not_issue_id(agents, outputs, monkeypatch):
    assessed = ReviewAssessmentV2(contract_version=2, passed=False, score=10, issue_details=[{
        "issue_id": "RV-1", "category": "implementation", "severity": "major", "message": "边界问题",
        "file": "calculator.py", "line_start": None, "line_end": None, "evidence_quote": "return a + b",
        "requirement_ids": ["FR-1"], "acceptance_ids": [], "expected": "满足边界", "observed": "仍需处理", "suggested_owner": "coding",
    }], suggestions=[], summary="静态意见，未执行测试")
    first = bind_review(assessed, *outputs)
    assessed.issue_details[0].issue_id = "RV-99"
    second = bind_review(assessed, *outputs)
    review = MagicMock(side_effect=[first, second])
    monkeypatch.setattr("workflow_studio.workflows.nodes.review_code", review)
    result = run_workflow(REQUEST, review_enabled=True, max_revisions=3, budget=RunBudget(max_repeated_reviews=2))
    assert result.status == "stopped" and result.budget.stop_reason == "repeated_review"
    assert result.budget.repeated_review_count == 2 and result.revision_count == 1
    assert len(result.review_history) == 2 and result.review == second
    assert agents["coding"].call_count == 2


def test_web_budget_stop_and_downloads(agents, monkeypatch):
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="test-only", deepseek_model="mock"))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    at.number_input(key="max_logical_calls").set_value(1).run()
    at.text_area(key="request_input").input(REQUEST).run()
    at.button(key="start_workflow").click().run()
    page = at.session_state[SESSION_KEY]
    page.task.thread.join(timeout=10)
    at.run()
    assert not at.exception and page.result.status == "stopped" and page.error is None
    assert any("模型逻辑调用预算不足" in item.value for item in at.warning)
    assert "requirements" in page.exports and "report" in page.exports
    assert page.persisted_run_id == page.result.run_id
    at.run()
    assert agents["requirements"].call_count == 1


def test_stop_button_waits_for_inflight_call_and_preserves_output(agents, outputs, monkeypatch):
    entered, release = Event(), Event()
    def delayed(_):
        entered.set()
        assert release.wait(timeout=10)
        return outputs[0]
    agents["requirements"].side_effect = delayed
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="test-only", deepseek_model="mock"))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    try:
        at.text_area(key="request_input").input(REQUEST).run()
        at.button(key="start_workflow").click().run()
        assert entered.wait(timeout=5)
        at.button(key="stop_workflow").click().run()
        page = at.session_state[SESSION_KEY]
        assert page.running and page.task.cancel_event.is_set()
        assert at.button(key="stop_workflow").disabled
    finally:
        release.set()
        page = at.session_state[SESSION_KEY]
        page.task.thread.join(timeout=10)
    at.run()
    assert not at.exception and page.result.status == "stopped"
    assert page.result.requirements == outputs[0] and page.result.budget.stop_reason == "cancelled"
    agents["design"].assert_not_called()
    assert not request_stop(page)


@pytest.mark.parametrize("structured", [True, False])
def test_shared_client_preserves_http_stop_instead_of_translating_it(structured):
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as sdk:
        client = get_llm(Settings(deepseek_api_key="test-only"))
        tracker = BudgetTracker(RunBudget())
        error = RunStopped("http_attempts")
        if structured:
            sdk.return_value.with_structured_output.return_value.invoke.side_effect = error
        else:
            sdk.return_value.invoke.side_effect = error
        with budget_scope(tracker), pytest.raises(RunStopped) as info:
            client.invoke_structured("p", RequirementsOutput) if structured else client.invoke("p")
        assert info.value is error
        assert tracker.snapshot().in_flight_calls == 0


def test_stopped_review_routes_cannot_resume_coding(agents, outputs, monkeypatch):
    from workflow_studio.state.review import ReviewOutput
    from workflow_studio.workflows.routing import route_after_coding, route_after_review
    rejected = ReviewOutput(passed=False, score=10, issues=["需要修复"], suggestions=[], summary="未执行")
    monkeypatch.setattr("workflow_studio.workflows.nodes.review_code", lambda *_: rejected)
    states = list(stream_workflow(REQUEST, review_enabled=True, max_revisions=3, budget=RunBudget(max_node_executions=4)))
    state = states[-1]
    assert state["status"] == "stopped" and state["review_status"] == "needs_revision"
    assert state["revision_count"] == 0  # Denied node did not spend a repair attempt.
    assert route_after_coding(state) == route_after_review(state) == "__end__"
