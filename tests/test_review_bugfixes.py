"""Reproductions found during the whole-project correctness review."""

from io import BytesIO
import json
import sqlite3
import tarfile
from unittest.mock import MagicMock, patch
import warnings
import asyncio

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from workflow_studio.agents.requirements_agent import analyze_requirements, RequirementsOutputError
from workflow_studio.agents.design_agent import design_system
from workflow_studio.llm.errors import LLMStructuredOutputUnsupported, LLMTruncatedOutputError
from workflow_studio.state.requirements import RequirementsOutputV2, ClarificationAnswer, effective_request
from workflow_studio.state.coding import CodeFile, CodingOutput
from workflow_studio.state.design import DesignOutput, ModuleDesign
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.workflow import create_initial_state, validate_state, WorkflowStateError
from workflow_studio.storage import RunStore
from workflow_studio.storage.checkpoints import CheckpointStore
from workflow_studio.validation.docker import DockerBackend, Capture
from workflow_studio.validation.service import validate_artifact, validate_report_binding
from workflow_studio.web.controller import PageState, WorkerFinished, _finish
from workflow_studio.workflows.durable import create_durable, stream_durable, inspect_durable
from workflow_studio.budget import BudgetPersistenceError, current_budget, model_call

REQUEST = "使用 Python 标准库实现整数加法。"


@pytest.fixture
def artifacts():
    req = RequirementsOutputV2(project_name="加法", summary="整数加法", functional_requirements=["整数加法"], non_functional_requirements=[], constraints=["Python 标准库"], assumptions=[], acceptance_criteria=["2+3=5"], contract_version=2, open_questions=[],
        requirement_details=[{"id": "FR-1", "description": "整数加法", "source_quotes": ["整数加法"]}], acceptance_details=[{"id": "AC-1", "description": "2+3=5", "requirement_ids": ["FR-1"]}])
    design = DesignOutput(architecture_overview="单模块", technology_stack=["Python"], modules=[ModuleDesign(name="main", responsibility="整数加法", dependencies=[], requirement_ids=["FR-1"])], data_models=[], interfaces=[], project_structure="main.py", design_decisions=["标准库"], preserved_constraints=req.constraints)
    code = CodingOutput(files=[CodeFile(path="main.py", language="python", content="print('{}')", description="加法")], instructions="手工运行", dependencies=[], test_suggestions=[], limitations=[])
    return req, design, code


def result_for(artifacts):
    req, design, code = artifacts
    initial = create_initial_state(REQUEST)
    return WorkflowResult(run_id=initial["run_id"], status="completed", requirements=req, design=design, code=code, events=[], error=None)


def test_clarification_question_is_not_a_confirmed_requirement_source(artifacts):
    req, design, code = artifacts
    req = req.model_copy(deep=True)
    req.requirement_details[0].source_quotes = ["必须使用远程数据库"]
    answer = ClarificationAnswer(question_id="Q-1", question="是否必须使用远程数据库？", answer="否，只做本地加法")
    state = {**create_initial_state(REQUEST), "requirements": req.model_dump(), "clarifications": [answer.model_dump()]}
    with pytest.raises(WorkflowStateError):
        validate_state(state)


def test_real_answer_remains_a_valid_source(artifacts):
    req = artifacts[0].model_copy(deep=True)
    req.requirement_details[0].source_quotes = ["支持负数"]
    answer = ClarificationAnswer(question_id="Q-1", question="负数？", answer="支持负数")
    state = {**create_initial_state(REQUEST), "requirements": req.model_dump(), "clarifications": [answer.model_dump()]}
    assert validate_state(state)["requirements"] is not None


def test_native_requirements_are_returned_as_revalidated_nested_models(artifacts, monkeypatch):
    req = artifacts[0]
    constructed = RequirementsOutputV2.model_construct(**req.model_dump())
    client = MagicMock()
    client.invoke_structured.return_value = constructed
    monkeypatch.setattr("workflow_studio.agents.requirements_agent.get_llm", lambda: client)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        output = analyze_requirements(REQUEST)
    assert output.requirement_details[0].source_quotes == ["整数加法"]


def test_agent_rejects_question_only_source_before_publishing(artifacts, monkeypatch):
    req = artifacts[0].model_copy(deep=True)
    req.requirement_details[0].source_quotes = ["必须使用远程数据库"]
    answer = ClarificationAnswer(question_id="Q-1", question="是否必须使用远程数据库？", answer="否，只做本地加法")
    client = MagicMock()
    client.invoke_structured.return_value = req
    client.invoke.return_value = AIMessage(content=req.model_dump_json())
    monkeypatch.setattr("workflow_studio.agents.requirements_agent.get_llm", lambda: client)
    with pytest.raises(RequirementsOutputError):
        analyze_requirements(effective_request(REQUEST, [answer]), source_texts=(REQUEST, answer.answer), max_repair_attempts=0)


@pytest.mark.parametrize("agent", ["requirements", "design"])
def test_native_validation_exception_can_fall_back_without_unbound_variable(artifacts, monkeypatch, agent):
    req, design, _ = artifacts
    output = req if agent == "requirements" else design
    client = MagicMock()
    client.invoke_structured.side_effect = ValidationError.from_exception_data("native", [])
    client.invoke.return_value = AIMessage(content=output.model_dump_json())
    monkeypatch.setattr(f"workflow_studio.agents.{agent}_agent.get_llm", lambda: client)
    actual = analyze_requirements(REQUEST) if agent == "requirements" else design_system(req)
    assert actual == output


def test_native_design_nested_data_is_revalidated(artifacts, monkeypatch):
    req, design, _ = artifacts
    client = MagicMock()
    client.invoke_structured.return_value = DesignOutput.model_construct(**design.model_dump())
    monkeypatch.setattr("workflow_studio.agents.design_agent.get_llm", lambda: client)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        assert design_system(req) == design


@pytest.mark.parametrize("agent", ["requirements", "design"])
def test_parseable_truncated_agent_json_is_rejected(artifacts, monkeypatch, agent):
    req, design, _ = artifacts
    client = MagicMock()
    client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    client.invoke.return_value = AIMessage(content=(req if agent == "requirements" else design).model_dump_json(), response_metadata={"finish_reason": "length"})
    monkeypatch.setattr(f"workflow_studio.agents.{agent}_agent.get_llm", lambda: client)
    with pytest.raises(LLMTruncatedOutputError):
        analyze_requirements(REQUEST) if agent == "requirements" else design_system(req)
    assert client.invoke.call_count == 1


def test_clarified_result_without_optional_budget_can_roundtrip(artifacts, tmp_path):
    result = result_for(artifacts)
    result.clarifications = [ClarificationAnswer(question_id="Q-1", question="支持负数？", answer="是")]
    store = RunStore(tmp_path / "outputs")
    store.save(result, REQUEST, "mock", 1)
    saved = store.load(result.run_id)
    assert saved.metadata.schema_version == 3 and saved.result.model_dump() == result.model_dump()
    assert store.save(result, REQUEST, "mock", 1) == store.root / result.run_id


def test_durable_database_error_is_not_an_archived_workflow_failure(tmp_path):
    store = CheckpointStore(tmp_path / "cp")
    try:
        view = create_durable(store, REQUEST)
        graph = MagicMock()
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk full")
            yield
        graph.stream = broken
        # Keep real inspection; inject the error only into execution's graph.
        import workflow_studio.workflows.durable as durable
        original = durable._graph
        calls = 0
        def select(*args):
            nonlocal calls
            calls += 1
            return original(*args) if calls == 1 else graph
        with patch.object(durable, "_graph", side_effect=select):
            with pytest.raises(sqlite3.OperationalError):
                list(stream_durable(store, view.thread_id, expected_checkpoint_id=view.checkpoint_id))
        reopened = inspect_durable(store, view.thread_id)
        assert reopened.result is None
    finally:
        store.close()


@pytest.mark.parametrize("error_type", ["OperationalError", "OSError", "CheckpointError"])
def test_durable_worker_failure_does_not_fabricate_a_terminal_result(error_type):
    page = PageState(request=REQUEST, durable_thread_id="saved-task", snapshot=create_initial_state(REQUEST), running=True)
    with patch("workflow_studio.web.persistence.save_page") as save:
        _finish(page, WorkerFinished(1, error_type))
    assert not page.running and page.error and page.result is None
    assert page.snapshot["status"] == "pending"
    save.assert_not_called()


def test_deeply_nested_candidate_storage_file_does_not_crash_validation():
    from workflow_studio.validation.suites import get_suite
    data = b"[" * 7000 + b"0" + b"]" * 7000
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo("todos.json")
        entry.size = len(data)
        archive.addfile(entry, BytesIO(data))
    assert DockerBackend._check_storage(buffer.getvalue(), get_suite("todo-storage").cases[0]) is False


class Backend:
    environment = "pinned-test-image"
    def prepare(self, *args):
        pass
    def run_case(self, suite, case, *args):
        return Capture(0, b"{}"), True


@pytest.mark.parametrize("corruption", ["unknown_case", "missing_case"])
def test_failed_report_case_binding_is_checked_too(artifacts, corruption):
    result = result_for(artifacts)
    report = validate_artifact(result, "add-cli", backend=Backend())
    if corruption == "unknown_case":
        report = report.model_copy(update={"cases": (report.cases[0].model_copy(update={"case_id": "invented-case"}), *report.cases[1:])})
    else:
        report = report.model_copy(update={"cases": report.cases[:1]})
    with pytest.raises(ValueError):
        validate_report_binding(report, *artifacts)


@pytest.mark.parametrize("entry", ["sync", "async", "stream", "events"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_graph_infrastructure_failure_keeps_consumed_budget(entry, cancelled, monkeypatch):
    import workflow_studio.workflows.runner as runner
    def crash(*args, **kwargs):
        with model_call():
            pass
        if cancelled:
            current_budget().cancel.set()
            current_budget().check()
        raise RuntimeError("graph transport failed")
    def stream(*args, **kwargs):
        crash()
        yield
    async def acrash(*args, **kwargs):
        crash()
    graph = MagicMock(invoke=crash, ainvoke=acrash, stream=stream)
    monkeypatch.setattr(runner, "build_workflow", lambda **kwargs: graph)
    if entry == "sync":
        result = runner.run_workflow(REQUEST)
    elif entry == "async":
        result = asyncio.run(runner.arun_workflow(REQUEST))
    elif entry == "stream":
        result = WorkflowResult.from_state(list(runner.stream_workflow(REQUEST))[-1])
    else:
        result = WorkflowResult.from_state(list(runner.stream_workflow_events(REQUEST))[-1].state)
    assert result.status == ("stopped" if cancelled else "failed") and result.budget.logical_calls == 1
    assert result.budget.unknown_usage_calls == 1 and result.budget.in_flight_calls == 0


@pytest.mark.parametrize("structured", [False, True])
def test_shared_client_does_not_translate_budget_persistence_failure(structured):
    from workflow_studio.llm.deepseek_client import get_llm
    from workflow_studio.config import Settings
    error = BudgetPersistenceError("budget_journal_write_failed")
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as sdk:
        client = get_llm(Settings(deepseek_api_key="test-only"))
        if structured:
            sdk.return_value.with_structured_output.return_value.invoke.side_effect = error
        else:
            sdk.return_value.invoke.side_effect = error
        with pytest.raises(BudgetPersistenceError) as info:
            client.invoke_structured("test", RequirementsOutputV2) if structured else client.invoke("test")
        assert info.value is error


def test_interrupted_report_with_known_case_prefix_remains_valid(artifacts):
    report = validate_artifact(result_for(artifacts), "add-cli", backend=Backend())
    report = report.model_copy(update={"status": "error", "reason": "runtime_error", "cases": report.cases[:1]})
    validate_report_binding(report, *artifacts)
