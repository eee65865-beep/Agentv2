"""Web tests use actual LangGraph with mocked Agents; no paid API calls."""

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest
from streamlit.testing.v1 import AppTest

from workflow_studio.config import Settings
from workflow_studio.exporters.documents import code_zip, design_markdown, requirements_markdown, workflow_report
from workflow_studio.state import CodeFile, CodingOutput, DesignOutput, ModuleDesign, RequirementsOutput
from workflow_studio.web.app import SESSION_KEY
from workflow_studio.web.configuration import get_configuration
from workflow_studio.web.controller import PageState, agent_statuses, collect_updates, start_task
from workflow_studio.web.examples import EXAMPLES
from workflow_studio.workflows import stream_workflow_events
from workflow_studio.workflows.runner import WorkflowStreamPacket

REQUEST = "使用 Python 标准库开发一个两个整数相加的命令行工具。"
APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    req = RequirementsOutput(project_name="加法工具", summary="计算整数之和", functional_requirements=["两个整数相加"], non_functional_requirements=[], constraints=["仅 Python 标准库"], assumptions=[], acceptance_criteria=["2+3=5"])
    design = DesignOutput(architecture_overview="单模块命令行应用", technology_stack=["Python"], modules=[ModuleDesign(name="calculator", responsibility="整数加法", dependencies=[], requirement_ids=["FR-1"])], data_models=[], interfaces=["add(a, b)"], project_structure="calculator.py", design_decisions=["保持简单"], preserved_constraints=req.constraints)
    code = CodingOutput(files=[CodeFile(path="calculator.py", language="python", content="def add(a: int, b: int) -> int:\n    return a + b\n", description="加法函数")], instructions="手动运行 Python", dependencies=[], test_suggestions=["验证 2+3=5"], limitations=["未执行"])
    result = {}
    for key, method, output in [("requirements", "analyze_requirements", req), ("design", "design_system", design), ("coding", "generate_code", code)]:
        result[key] = MagicMock(return_value=output)
        monkeypatch.setattr(f"workflow_studio.workflows.nodes.{method}", result[key])
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="test-only-secret", deepseek_model="mock-model"))
    monkeypatch.setattr("workflow_studio.workflows.nodes.load_settings", lambda: Settings(deepseek_model="mock-model"))
    return result


def finish(page: PageState) -> None:
    assert page.task is not None and page.task.thread is not None
    page.task.thread.join(timeout=10)
    assert not page.task.thread.is_alive()
    collect_updates(page)


def submit(at: AppTest) -> AppTest:
    at.text_area(key="request_input").input(REQUEST).run()
    at.button(key="start_workflow").click().run()
    page = at.session_state[SESSION_KEY]
    assert page.task is not None and page.task.thread is not None
    page.task.thread.join(timeout=10)
    assert not page.task.thread.is_alive()
    at.run()
    assert not at.exception
    return at


def test_service_stream_started_before_agent_returns(agents: dict[str, MagicMock]) -> None:
    stream = stream_workflow_events(REQUEST)
    initial = next(stream)
    assert initial.kind == "state" and initial.state["status"] == "pending"
    started = next(stream)
    assert started.kind == "event" and started.event.event_type == "started"
    packets = [initial, started, *stream]
    assert [p.event.event_type for p in packets if p.event] == ["started", "completed"] * 3
    assert packets[-1].state["status"] == "completed"


def test_controller_success_deduplicates_and_keeps_runs_isolated(agents: dict[str, MagicMock]) -> None:
    first, second = PageState(), PageState()
    for page in (first, second):
        assert start_task(page, REQUEST, True, "mock-model")
        finish(page)
        assert page.result.status == "completed"
        assert len(page.events) == 6
        assert set(agent_statuses(page).values()) == {"已完成"}
        for _ in range(3):
            collect_updates(page)
        assert len(page.events) == 6
    assert first.result.run_id != second.result.run_id
    assert agents["requirements"].call_count == 2


@pytest.mark.parametrize("text,configured", [("", True), ("短", True), (REQUEST, False)])
def test_controller_rejects_input_without_starting(text: str, configured: bool, agents: dict[str, MagicMock]) -> None:
    page = PageState()
    assert not start_task(page, text, configured, "mock-model")
    assert page.error and page.task is None
    agents["requirements"].assert_not_called()


def test_busy_guard_and_real_live_status(monkeypatch: pytest.MonkeyPatch, agents: dict[str, MagicMock]) -> None:
    entered, release = Event(), Event()
    output = agents["requirements"].return_value
    def blocked(_: str) -> RequirementsOutput:
        entered.set()
        assert release.wait(timeout=10)
        return output
    agents["requirements"].side_effect = blocked
    page = PageState()
    try:
        assert start_task(page, REQUEST, True, "mock-model")
        assert entered.wait(timeout=10)
        # Synchronize with the stream consumer, not only the Agent thread.
        packets = [page.task.queue.get(timeout=10), page.task.queue.get(timeout=10)]
        for packet in packets:
            page.task.queue.put(packet)
        collect_updates(page)
        assert page.running
        assert agent_statuses(page) == {"requirements_agent": "运行中", "design_agent": "等待中", "coding_agent": "等待中"}
        assert not start_task(page, REQUEST, True, "mock-model")
        assert agents["requirements"].call_count == 1
    finally:
        release.set()
        finish(page)
    assert page.result.status == "completed"


def test_failure_preserves_partial_result_and_never_completes(agents: dict[str, MagicMock]) -> None:
    agents["design"].side_effect = RuntimeError("private-provider-body")
    page = PageState()
    assert start_task(page, REQUEST, True, "mock-model")
    finish(page)
    assert page.result.status == "failed"
    assert page.result.requirements is not None and page.result.code is None
    assert agent_statuses(page) == {"requirements_agent": "已完成", "design_agent": "执行失败", "coding_agent": "等待中"}
    assert page.error and "private-provider-body" not in page.error
    agents["coding"].assert_not_called()


def test_unexpected_worker_failure_is_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_: str, **kwargs) -> Iterator[WorkflowStreamPacket]:
        raise RuntimeError("private-secret")
        yield  # pragma: no cover
    monkeypatch.setattr("workflow_studio.web.controller.stream_workflow_events", broken)
    page = PageState()
    assert start_task(page, REQUEST, True, "mock-model")
    finish(page)
    assert page.result.status == "failed" and page.error
    assert "private-secret" not in page.error


def test_page_initial_render_and_examples(agents: dict[str, MagicMock]) -> None:
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    assert at.title[0].value == "Workflow Studio"
    assert not at.tabs and not at.metric
    assert any("今天想构建什么" in item.value for item in at.markdown)
    assert at.text_area(key="request_input").label == "开发需求"
    for choice in list(EXAMPLES)[1:]:
        at.selectbox(key="example_choice").select(choice).run()
        assert at.text_area(key="request_input").value == EXAMPLES[choice]
    assert not at.exception
    agents["requirements"].assert_not_called()


def test_page_submit_results_downloads_and_rerun_safety(agents: dict[str, MagicMock]) -> None:
    at = submit(AppTest.from_file(str(APP), default_timeout=10).run())
    page = at.session_state[SESSION_KEY]
    assert page.result.status == "completed"
    assert [metric.value for metric in at.metric] == ["已完成"] * 3
    assert any("def add" in element.value for element in at.code)
    assert len(at.dataframe[0].value) == 6
    assert set(page.exports) == {"requirements", "design", "code", "report"}
    assert len(at.get("download_button")) == 4
    assert not at.button(key="start_workflow").disabled
    assert "test-only-secret" not in str(at)
    for _ in range(3):
        at.run()
    at.selectbox(key="example_choice").select("简单 Todo 任务管理器").run()
    for mock in agents.values():
        mock.assert_called_once()
    assert not at.exception


def test_page_shows_errors_and_does_not_retry(agents: dict[str, MagicMock]) -> None:
    agents["design"].side_effect = RuntimeError("private-provider-body")
    at = submit(AppTest.from_file(str(APP), default_timeout=10).run())
    assert at.error and "工作流执行失败" in at.error[0].value
    assert "private-provider-body" not in at.error[0].value
    assert [metric.value for metric in at.metric] == ["已完成", "执行失败", "等待中"]
    page = at.session_state[SESSION_KEY]
    assert set(page.exports) == {"requirements", "report"}
    at.run()
    agents["design"].assert_called_once()
    agents["coding"].assert_not_called()


def test_page_rejects_short_request(agents: dict[str, MagicMock]) -> None:
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    at.button(key="start_workflow").click().run()
    assert at.error and "至少" in at.error[0].value
    agents["requirements"].assert_not_called()


def test_page_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings())
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    assert at.warning[0].value == "API 配置：未配置"
    assert at.button(key="start_workflow").disabled


def test_bad_configuration_never_echoes_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid() -> Settings:
        return Settings(deepseek_timeout="private-secret")
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", invalid)
    view = get_configuration()
    assert not view.configured and view.error
    assert "private-secret" not in str(view)


def test_download_documents_and_zip(agents: dict[str, MagicMock]) -> None:
    page = PageState()
    start_task(page, REQUEST, True, "mock-model")
    finish(page)
    result = page.result
    assert "仅 Python 标准库" in requirements_markdown(result.requirements)
    assert "FR-1" in design_markdown(result.design)
    report = workflow_report(result, REQUEST, "mock-model", 1.5)
    assert result.run_id in report and REQUEST in report and "1.50" in report
    with ZipFile(BytesIO(code_zip(result.code))) as archive:
        assert archive.namelist() == ["calculator.py"]
        assert archive.read("calculator.py").decode("utf-8") == result.code.files[0].content


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "C:/absolute.py", "nested/../../escape.py"])
def test_zip_revalidates_mutated_paths(path: str, agents: dict[str, MagicMock]) -> None:
    code = agents["coding"].return_value.model_copy(deep=True)
    code.files[0].path = path
    with pytest.raises(ValueError):
        code_zip(code)


def test_zip_rejects_duplicate_and_empty_files(agents: dict[str, MagicMock]) -> None:
    code = agents["coding"].return_value.model_copy(deep=True)
    code.files.append(code.files[0].model_copy())
    with pytest.raises(ValueError):
        code_zip(code)
    code.files.pop()
    code.files[0].content = ""
    with pytest.raises(ValueError):
        code_zip(code)


def test_page_disables_submit_while_actual_agent_runs(agents: dict[str, MagicMock]) -> None:
    entered, release = Event(), Event()
    output = agents["requirements"].return_value
    def blocked(_: str) -> RequirementsOutput:
        entered.set()
        assert release.wait(timeout=10)
        return output
    agents["requirements"].side_effect = blocked
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    try:
        at.text_area(key="request_input").input(REQUEST).run()
        at.button(key="start_workflow").click().run()
        assert entered.wait(timeout=10)
        at.run()
        assert at.button(key="start_workflow").disabled
        assert at.text_area(key="request_input").disabled
        assert at.selectbox(key="example_choice").disabled
        assert agents["requirements"].call_count == 1
    finally:
        release.set()
        page = at.session_state[SESSION_KEY]
        if page.task and page.task.thread:
            page.task.thread.join(timeout=10)
    at.run()
    assert not at.exception
    assert at.session_state[SESSION_KEY].result.status == "completed"


def test_trace_metadata_and_read_write_contract(agents: dict[str, MagicMock]) -> None:
    from workflow_studio.state.tracing import summarize_run
    from workflow_studio.state.workflow import WorkflowEvent
    from workflow_studio.workflows import run_workflow
    result = run_workflow(REQUEST)
    assert result.status == "completed" and len(result.events) == 6
    for index, fields in enumerate([("user_request",), ("requirements",), ("requirements", "design")]):
        started, completed = result.events[index * 2:index * 2 + 2]
        assert started.event_type == "started" and completed.event_type == "completed"
        assert started.started_at == completed.started_at
        assert started.ended_at is None and started.elapsed_seconds is None
        assert completed.ended_at >= completed.started_at
        assert completed.elapsed_seconds >= 0
        assert completed.input_fields == started.input_fields == fields
        output = ("requirements", "design", "code")[index]
        assert completed.output_fields == (output, "status", "budget", "current_agent", "events")
        assert completed.model_name == "mock-model"
        assert completed.token_usage.total_tokens is None
        assert WorkflowEvent.model_validate_json(completed.model_dump_json()) == completed
    trace = summarize_run(result.events)
    assert trace.status == result.status and trace.run_id == result.run_id
    assert trace.started_at == result.events[0].started_at
    assert trace.ended_at == result.events[-1].ended_at
    assert trace.model_names == ("mock-model",)


def test_node_collects_actual_client_usage_and_discards_private_values(agents: dict[str, MagicMock]) -> None:
    from unittest.mock import patch
    from langchain_core.messages import AIMessage
    from workflow_studio.llm.deepseek_client import get_llm
    from workflow_studio.workflows import run_workflow
    from workflow_studio.state.telemetry import TokenUsage
    def call_model(_: str) -> RequirementsOutput:
        llm = get_llm(Settings(deepseek_api_key="private-key", deepseek_model="mock-model"))
        llm.invoke("private-user-prompt")
        llm.invoke("private-repair-prompt")
        return agents["requirements"].return_value
    agents["requirements"].side_effect = call_model
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as sdk:
        sdk.return_value.invoke.return_value = AIMessage(content="private-model-response", response_metadata={"secret": "private-provider-secret"}, usage_metadata={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12})
        result = run_workflow(REQUEST)
    assert result.status == "completed"
    terminal = result.events[1]
    assert len(terminal.llm_calls) == 2
    assert terminal.token_usage == TokenUsage(input_tokens=14, output_tokens=10, total_tokens=24)
    serialized = " ".join(event.model_dump_json() for event in result.events)
    for secret in ["private-key", "private-user-prompt", "private-repair-prompt", "private-model-response", "private-provider-secret", REQUEST]:
        assert secret not in serialized
    assert result.events[3].llm_calls == ()


def test_timeline_agent_details_and_failure_reason(agents: dict[str, MagicMock]) -> None:
    agents["design"].side_effect = RuntimeError("secret-password-provider-detail")
    at = submit(AppTest.from_file(str(APP), default_timeout=10).run())
    at.selectbox(key="trace_agent_choice").select("design_agent").run()
    assert not at.exception
    page = at.session_state[SESSION_KEY]
    failed = page.events[-1]
    assert failed.error_type == "RuntimeError" and failed.elapsed_seconds >= 0
    assert failed.input_fields == ("requirements",)
    assert failed.output_fields == ("status", "current_agent", "error", "events", "budget")
    assert any("失败原因" in warning.value and "RuntimeError" in warning.value for warning in at.warning)
    assert any("开始时间" in caption.value for caption in at.caption)
    assert any("结束时间" in caption.value for caption in at.caption)
    timeline = at.dataframe[0].value
    assert timeline.iloc[-1]["Agent"] == "Design Agent"
    assert timeline.iloc[-1]["节点状态"] == "执行失败"
    assert timeline.iloc[-1]["Workflow 状态"] == "执行失败"
    assert timeline.iloc[-1]["Token 总数"] == "不可用"
    assert "secret-password-provider-detail" not in str(at)
    assert "RuntimeError" in page.exports["report"].decode("utf-8")
    at.run()
    assert len(at.session_state[SESSION_KEY].events) == 4
    agents["coding"].assert_not_called()


def test_page_renders_reported_token_usage(agents: dict[str, MagicMock]) -> None:
    from workflow_studio.llm.telemetry import record_llm_call
    from workflow_studio.state.telemetry import LLMCallRecord, TokenUsage
    def with_usage(_: str) -> RequirementsOutput:
        record_llm_call(LLMCallRecord(model_name="mock-model", elapsed_seconds=0.25, status="success", usage=TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12)))
        return agents["requirements"].return_value
    agents["requirements"].side_effect = with_usage
    at = submit(AppTest.from_file(str(APP), default_timeout=10).run())
    assert at.dataframe[0].value.iloc[1]["Token 总数"] == "12"
    assert at.dataframe[1].value.iloc[0]["输入 Token"] == "10"
    assert at.session_state[SESSION_KEY].events[1].token_usage.total_tokens == 12
    assert not at.exception


def test_stream_end_without_terminal_state_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from workflow_studio.state.workflow import create_initial_state
    def incomplete(request: str, **kwargs) -> Iterator[WorkflowStreamPacket]:
        yield WorkflowStreamPacket(kind="state", state=create_initial_state(request))
    monkeypatch.setattr("workflow_studio.web.controller.stream_workflow_events", incomplete)
    page = PageState()
    start_task(page, REQUEST, True, "mock-model")
    finish(page)
    assert page.result.status == "failed"
    assert page.events[-1].event_type == "failed"
    assert page.error


@pytest.mark.parametrize("error_type,expected", [
    ("RuntimeError", "内部异常"), ("LLMAuthenticationError", "认证"),
    ("LLMRateLimitError", "限流"), ("LLMTimeoutError", "超时"),
    ("DesignOutputError", "一致性"), ("LLMTruncatedOutputError", "截断"),
])
def test_safe_failure_reason_categories(error_type: str, expected: str) -> None:
    from workflow_studio.web.controller import friendly_error
    assert expected in friendly_error(error_type)


def test_thinking_toggle_submit_and_redraw(agents: dict[str, MagicMock]) -> None:
    at = AppTest.from_file(str(APP)).run()
    assert not at.toggle(key="thinking_enabled").value
    at.toggle(key="thinking_enabled").set_value(True).run()
    assert all(agent.call_count == 0 for agent in agents.values())
    submit(at)
    assert at.session_state[SESSION_KEY].thinking_enabled is True
    before = [agent.call_count for agent in agents.values()]
    at.run()
    assert [agent.call_count for agent in agents.values()] == before
    at.toggle(key="thinking_enabled").set_value(False).run()
    assert [agent.call_count for agent in agents.values()] == before


def test_concurrent_thinking_modes_reach_all_graph_nodes(
    agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Barrier
    from workflow_studio.llm.deepseek_client import get_llm
    from workflow_studio.llm.options import current_thinking_mode
    barrier = Barrier(2)
    seen: list[tuple[str | None, str]] = []
    monkeypatch.setattr("workflow_studio.llm.deepseek_client.DeepSeekClient", lambda settings: settings)
    for name, agent in agents.items():
        output = agent.return_value
        def respond(*args: object, stage: str = name, value: object = output, **kwargs: object) -> object:
            if stage == "requirements":
                barrier.wait(timeout=10)
            seen.append((current_thinking_mode(), get_llm(Settings()).deepseek_thinking))
            return value
        agent.side_effect = respond
    first, second = PageState(), PageState()
    assert start_task(first, REQUEST, True, "mock-model", thinking_enabled=True)
    assert start_task(second, REQUEST, True, "mock-model", thinking_enabled=False)
    finish(first)
    finish(second)
    assert first.result.status == second.result.status == "completed"
    assert seen.count(("enabled", "enabled")) == 3
    assert seen.count(("disabled", "disabled")) == 3
    assert current_thinking_mode() is None
    assert get_llm(Settings()).deepseek_thinking == "disabled"


def test_thinking_toggle_disabled_while_running(agents: dict[str, MagicMock]) -> None:
    at = AppTest.from_file(str(APP)).run()
    page = at.session_state[SESSION_KEY]
    page.running = True
    at.run()
    assert at.toggle(key="thinking_enabled").disabled
    assert all(agent.call_count == 0 for agent in agents.values())


def test_thinking_toggle_initializes_from_configuration(
    agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(
        deepseek_api_key="mock-only", deepseek_thinking="enabled",
    ))
    at = AppTest.from_file(str(APP)).run()
    assert at.toggle(key="thinking_enabled").value is True
    at.toggle(key="thinking_enabled").set_value(False).run()
    assert at.toggle(key="thinking_enabled").value is False
    submit(at)
    assert at.session_state[SESSION_KEY].thinking_enabled is False


def test_thinking_override_reaches_review_and_revision(
    agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_studio.llm.options import current_thinking_mode
    from workflow_studio.state.review import ReviewOutput
    calls: list[str | None] = []
    original = agents["coding"].return_value
    def coding(*args: object, **kwargs: object) -> CodingOutput:
        calls.append(current_thinking_mode())
        return original
    agents["coding"].side_effect = coding
    reviews = iter([
        ReviewOutput(passed=False, score=40, issues=["补充说明"], suggestions=[], summary="仅静态审查"),
        ReviewOutput(passed=True, score=90, issues=[], suggestions=[], summary="仅静态审查，未运行测试"),
    ])
    def review(*args: object, **kwargs: object) -> ReviewOutput:
        calls.append(current_thinking_mode())
        return next(reviews)
    monkeypatch.setattr("workflow_studio.workflows.nodes.review_code", review)
    page = PageState()
    assert start_task(page, REQUEST, True, "mock-model", review_enabled=True, thinking_enabled=True)
    finish(page)
    assert page.result.status == "completed"
    assert page.result.revision_count == 1
    assert calls == ["enabled"] * 4
    assert current_thinking_mode() is None
