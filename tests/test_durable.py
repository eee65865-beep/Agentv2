"""Persistent LangGraph interrupts, process crashes and explicit retry attempts."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from workflow_studio.budget import BudgetPersistenceError, RunBudget, current_budget, model_call
from workflow_studio.config import Settings
from workflow_studio.exporters.documents import workflow_report
from workflow_studio.llm.deepseek_client import get_llm
from workflow_studio.llm.options import current_model_profile, current_thinking_mode, model_profile
from workflow_studio.state import CodeFile, CodingOutput, DesignOutput, ModuleDesign, RequirementsOutput
from workflow_studio.state.requirements import OpenQuestion, RequirementsOutputV2, preserve_requirement_ids
from workflow_studio.state.review import ReviewAssessmentV2, ReviewOutput
from workflow_studio.state.review_policy import bind_review, review_context_digest
from workflow_studio.storage import RunStore
from workflow_studio.storage.checkpoints import CheckpointConflictError, CheckpointError, CheckpointStore
from workflow_studio.web.app import SESSION_KEY
from workflow_studio.web.controller import PageState, collect_updates, open_durable_task, resume_task, start_task
from workflow_studio.workflows.durable import aexecute_durable, create_durable, execute_durable, fork_durable_attempt, inspect_durable, list_durable, stream_durable

REQUEST = "使用 Python 标准库提供整数加法。"


@pytest.fixture
def store(tmp_path):
    value = CheckpointStore(tmp_path / "cp")
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def outputs():
    req = RequirementsOutputV2(
        project_name="加法", summary="整数相加", functional_requirements=["整数加法"], non_functional_requirements=[],
        constraints=["Python 标准库"], assumptions=[], acceptance_criteria=["2+3=5"], contract_version=2,
        requirement_details=[{"id": "FR-7", "description": "整数加法", "source_quotes": ["整数加法"]}],
        acceptance_details=[{"id": "AC-3", "description": "2+3=5", "requirement_ids": ["FR-7"]}], open_questions=[],
    )
    design = DesignOutput(architecture_overview="单模块", technology_stack=["Python"], modules=[ModuleDesign(name="app", responsibility="加法", dependencies=[], requirement_ids=["FR-7"])], data_models=[], interfaces=[], project_structure="app.py", design_decisions=["简单"], preserved_constraints=req.constraints)
    code = CodingOutput(files=[CodeFile(path="app.py", language="python", content="def add(a,b):\n    return a+b\n", description="加法")], instructions="手工运行", dependencies=[], test_suggestions=[], limitations=["未执行测试"])
    return req, design, code


@pytest.fixture
def agents(outputs, monkeypatch):
    result = {}
    for name, method, value in zip(["requirements", "design", "coding"], ["analyze_requirements", "design_system", "generate_code"], outputs):
        def action(*args, value=value, **kwargs):
            with model_call():
                return value
        result[name] = MagicMock(side_effect=action)
        monkeypatch.setattr(f"workflow_studio.workflows.nodes.{method}", result[name])
    monkeypatch.setattr("workflow_studio.workflows.durable.load_settings", lambda: Settings(deepseek_api_key="private-never-save", deepseek_model="saved-model"))
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="private-never-save", deepseek_model="saved-model"))
    return result


def question(req):
    value = req.model_copy(deep=True)
    value.open_questions = [OpenQuestion(id="Q-1", question="是否需要处理负数？", blocking=True, requirement_ids=["FR-7"])]
    return value


def execute(store, view, **kwargs):
    return execute_durable(store, view.thread_id, expected_checkpoint_id=view.checkpoint_id, **kwargs)


def wait_page(page):
    page.task.thread.join(timeout=10)
    assert not page.task.thread.is_alive()
    collect_updates(page)


def test_wait_reopen_and_resume_native_interrupt(store, outputs, agents):
    original = question(outputs[0])
    agents["requirements"].side_effect = [original, outputs[0]]
    view = execute(store, create_durable(store, REQUEST))
    assert view.status == "waiting" and view.result is None
    assert view.state["status"] == "waiting"
    assert view.state["design"] is view.state["code"] is None
    agents["design"].assert_not_called()
    identifier, cp = view.thread_id, view.checkpoint_id
    reopened = CheckpointStore(store.root)
    try:
        view = inspect_durable(reopened, identifier)
        assert view.status == "waiting" and view.checkpoint_id == cp
        view = execute(reopened, view, answers={"Q-1": "需要处理负数"})
        assert view.status == "completed" and view.result is not None
        assert view.state["clarifications"][0]["answer"] == "需要处理负数"
        assert view.budget.clarification_rounds == 1
        assert agents["requirements"].call_args.kwargs["previous_requirements"].model_dump() == original.model_dump()
        assert "用户回答：需要处理负数" in agents["requirements"].call_args.args[0]
    finally:
        reopened.close()
    assert current_budget() is current_model_profile() is current_thinking_mode() is None


def test_resume_after_clarification_prepared_before_interrupt_was_persisted(store, outputs, agents):
    agents["requirements"].side_effect = [question(outputs[0]), outputs[0]]
    view = create_durable(store, REQUEST)
    stream = stream_durable(store, view.thread_id, expected_checkpoint_id=view.checkpoint_id)
    for packet in stream:
        if packet.state is not None and packet.state["status"] == "waiting":
            break
    stream.close()
    reopened = inspect_durable(store, view.thread_id)
    done = execute(store, reopened, answers={"Q-1": "需要处理负数"})
    assert done.status == "completed"
    assert done.state["clarifications"][0]["answer"] == "需要处理负数"
    assert done.budget.clarification_rounds == 1


def test_budget_journal_failure_after_model_call_remains_recoverable(store, agents, monkeypatch):
    import workflow_studio.workflows.durable as durable
    view = create_durable(store, REQUEST)
    original = durable._write
    failed = False
    def write(store, table, record):
        nonlocal failed
        if table == "attempts" and record.budget.logical_calls == 1 and record.budget.in_flight_calls == 0 and not failed:
            failed = True
            raise OSError("transient journal failure")
        return original(store, table, record)
    monkeypatch.setattr(durable, "_write", write)
    with pytest.raises(BudgetPersistenceError):
        execute(store, view)
    assert failed
    pending = inspect_durable(store, view.thread_id)
    assert pending.result is None and pending.budget.logical_calls == 1
    done = execute(store, pending)
    assert done.status == "completed" and done.budget.logical_calls == 4


@pytest.mark.parametrize("answer", [None, {}, {"Q-2": "不相关"}, {"Q-1": " "}, {"Q-1": "x" * 4001}, {"Q-1": 1}])
def test_invalid_answers_do_not_consume_interrupt_or_call_model(store, outputs, agents, answer):
    agents["requirements"].side_effect = [question(outputs[0]), outputs[0]]
    view = execute(store, create_durable(store, REQUEST))
    before = view.budget
    with pytest.raises(ValueError):
        execute(store, view, answers=answer)
    after = inspect_durable(store, view.thread_id)
    assert after.status == "waiting" and after.checkpoint_id == view.checkpoint_id
    assert after.budget == before and agents["requirements"].call_count == 1


def test_duplicate_resume_and_concurrent_owner_are_rejected(store, agents):
    view = create_durable(store, REQUEST)
    other = CheckpointStore(store.root)
    try:
        with store.claim(view.thread_id), pytest.raises(CheckpointConflictError):
            execute(other, view)
    finally:
        other.close()
    done = execute(store, view)
    assert done.status == "completed"
    with pytest.raises(CheckpointConflictError):
        execute(store, view)
    agents["requirements"].assert_called_once()


def test_failed_node_retries_in_new_attempt_without_repeating_upstream(store, agents, tmp_path):
    agents["coding"].side_effect = RuntimeError("private-provider-body")
    failed = execute(store, create_durable(store, REQUEST))
    assert failed.status == "failed" and failed.state["design"] is not None
    archive = RunStore(tmp_path / "archive")
    folder = archive.save(failed.result, REQUEST, "saved-model", 1)
    original = (folder / "metadata.json").read_bytes()
    code = CodingOutput(files=[CodeFile(path="app.py", language="python", content="x=1", description="测试")], instructions="未执行", dependencies=[], test_suggestions=[], limitations=[])
    agents["coding"].side_effect = None
    agents["coding"].return_value = code
    child = fork_durable_attempt(store, failed.thread_id, expected_checkpoint_id=failed.checkpoint_id)
    assert child.run_id != failed.run_id and child.thread_id == failed.thread_id
    assert child.budget.node_executions >= failed.budget.node_executions
    done = execute(store, child)
    assert done.status == "completed" and len(done.attempts) == 2
    assert done.attempts[0]["status"] == "failed" and done.attempts[1]["parent_attempt"] == failed.attempt_id
    agents["requirements"].assert_called_once()
    agents["design"].assert_called_once()
    assert agents["coding"].call_count == 2
    archive.save(done.result, REQUEST, "saved-model", 1)
    assert (folder / "metadata.json").read_bytes() == original


def review(outputs, *, category="design"):
    assessment = ReviewAssessmentV2(contract_version=2, passed=False, score=40, issue_details=[{
        "issue_id": "RV-1", "category": category, "severity": "major", "message": "接口约定需要补充",
        "file": None, "line_start": None, "line_end": None, "evidence_quote": None,
        "requirement_ids": ["FR-7"], "acceptance_ids": ["AC-3"], "expected": "明确边界", "observed": "边界未定义",
        "suggested_owner": "requirements" if category == "requirement" else "design",
    }], suggestions=["核对约定"], summary="静态意见，未执行测试")
    return bind_review(assessment, *outputs)


@pytest.mark.parametrize("category", ["design", "requirement"])
def test_replan_invalidates_downstream_and_preserves_parent_review(store, outputs, agents, monkeypatch, category):
    rejected = review(outputs, category=category)
    monkeypatch.setattr("workflow_studio.workflows.nodes.review_code", lambda *_: rejected)
    parent = execute(store, create_durable(store, REQUEST, review_enabled=True))
    assert parent.status == "completed_with_issues" and parent.state["review_history"]
    child = fork_durable_attempt(store, parent.thread_id, expected_checkpoint_id=parent.checkpoint_id, replan=True)
    assert child.state["design"] is child.state["code"] is None
    assert child.state["review_history"] == []
    with store.lock:
        payload = store.db.execute("SELECT payload FROM attempts WHERE id=?", (parent.attempt_id,)).fetchone()[0]
    saved = json.loads(payload)
    assert saved["final_state"]["review_history"][0]["review"]["artifact_digest"] == rejected.artifact_digest
    if category == "requirement":
        child = execute(store, child)
        assert child.status == "waiting"
        agents["requirements"].side_effect = None
        agents["requirements"].return_value = outputs[0]
        child = execute(store, child, answers={child.state["awaiting_questions"][0]["id"]: "补充后的边界约定"})
    else:
        child = execute(store, child)
        assert agents["requirements"].call_count == 1
    assert "需在原需求约束内修订" in agents["design"].call_args.kwargs["user_request"]
    assert child.budget.node_executions > parent.budget.node_executions


def test_stopped_budget_cannot_be_reset_by_retry(store, agents):
    stopped = execute(store, create_durable(store, REQUEST, budget=RunBudget(max_node_executions=1)))
    assert stopped.status == "stopped"
    with pytest.raises(CheckpointConflictError):
        fork_durable_attempt(store, stopped.thread_id, expected_checkpoint_id=stopped.checkpoint_id)
    assert inspect_durable(store, stopped.thread_id).budget.stop_reason == "node_executions"


def test_zero_clarification_budget_stops_instead_of_waiting(store, outputs, agents):
    agents["requirements"].side_effect = None
    agents["requirements"].return_value = question(outputs[0])
    view = execute(store, create_durable(store, REQUEST, budget=RunBudget(max_clarification_rounds=0)))
    assert view.status == "stopped" and view.budget.stop_reason == "clarifications"
    agents["design"].assert_not_called()


def test_unknown_workflow_version_refuses_resume(store, agents):
    view = create_durable(store, REQUEST)
    with store.lock, store.db:
        row = store.db.execute("SELECT payload FROM sessions WHERE id=?", (view.thread_id,)).fetchone()[0]
        data = json.loads(row)
        data["workflow_version"] = "unknown-future"
        store.db.execute("UPDATE sessions SET payload=? WHERE id=?", (json.dumps(data), view.thread_id))
    with pytest.raises(CheckpointError, match="unsupported_workflow_version"):
        execute(store, view)
    agents["requirements"].assert_not_called()


def test_profile_is_pinned_and_secret_is_not_persisted(store, agents):
    view = create_durable(store, REQUEST, thinking_enabled=True)
    session = json.loads(store.db.execute("SELECT payload FROM sessions WHERE id=?", (view.thread_id,)).fetchone()[0])
    assert "deepseek_api_key" not in session["profile"] and "private-never-save" not in json.dumps(session)
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as sdk, model_profile(session["profile"]):
        get_llm(Settings(deepseek_api_key="current-secret", deepseek_model="changed-model"))
        assert sdk.call_args.kwargs["model"] == "saved-model"
    assert current_model_profile() is None


def test_serialization_refuses_pickle_and_session_lock_is_released(store, agents):
    for payload in (b"pickle\0malicious", b"missing-delimiter", b"msgpack\0\xc1"):
        with pytest.raises(CheckpointError):
            store._decode(payload)
    view = create_durable(store, REQUEST)
    with pytest.raises(RuntimeError):
        with store.claim(view.thread_id):
            raise RuntimeError("test")
    with store.claim(view.thread_id):
        pass


def test_async_execution_uses_same_persistent_rules(store, agents):
    view = create_durable(store, REQUEST)
    done = asyncio.run(aexecute_durable(store, view.thread_id, expected_checkpoint_id=view.checkpoint_id))
    assert done.status == "completed" and done.result is not None


def test_stream_does_not_leak_budget_or_model_options(store, agents):
    view = create_durable(store, REQUEST, thinking_enabled=True)
    for _ in stream_durable(store, view.thread_id, expected_checkpoint_id=view.checkpoint_id):
        assert current_budget() is current_model_profile() is current_thinking_mode() is None
    assert inspect_durable(store, view.thread_id).status == "completed"


def test_answer_provenance_survives_terminal_archive(store, outputs, agents, tmp_path):
    initial = question(outputs[0])
    revised = outputs[0].model_copy(deep=True)
    revised.requirement_details[0].source_quotes = ["明确支持负数"]
    agents["requirements"].side_effect = [initial, revised]
    waiting = execute(store, create_durable(store, REQUEST))
    done = execute(store, waiting, answers={"Q-1": "明确支持负数"})
    archive = RunStore(tmp_path / "archive")
    archive.save(done.result, REQUEST, "saved-model", 1)
    loaded = archive.load(done.run_id)
    assert loaded.metadata.schema_version == 3
    assert loaded.result == done.result and loaded.result.clarifications[0].answer == "明确支持负数"
    assert "用户澄清记录" in workflow_report(loaded.result, REQUEST, "saved-model", 1)


def test_stable_ids_survive_new_model_numbering(outputs):
    previous = outputs[0]
    data = previous.model_dump()
    data["requirement_details"][0]["id"] = "FR-1"
    data["acceptance_details"][0].update(id="AC-1", requirement_ids=["FR-1"])
    data["open_questions"] = [{"id": "Q-1", "question": "边界？", "blocking": True, "requirement_ids": ["FR-1"]}]
    revised = preserve_requirement_ids(RequirementsOutputV2.model_validate(data), previous)
    assert revised.requirement_details[0].id == "FR-7"
    assert revised.acceptance_details[0].id == "AC-3"
    assert revised.open_questions[0].requirement_ids == ["FR-7"]


def test_empty_questions_preserve_legacy_review_digest(outputs):
    req, design, code = outputs
    old_req = req.model_dump(mode="json")
    old_req.pop("open_questions")
    payload = {"requirements": old_req, "design": design.model_dump(mode="json"), "code": code.model_dump(mode="json")}
    digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert review_context_digest(*outputs) == digest


def test_clarification_question_contract_rejects_duplicate_or_unknown_ids(outputs):
    data = outputs[0].model_dump()
    q = {"id": "Q-1", "question": "边界？", "blocking": True, "requirement_ids": ["FR-99"]}
    with pytest.raises(ValidationError):
        RequirementsOutput.model_validate({**data, "open_questions": [q]})
    q["requirement_ids"] = []
    with pytest.raises(ValidationError):
        RequirementsOutput.model_validate({**data, "open_questions": [q, q]})


def test_page_can_reopen_waiting_task_and_resume_after_new_session(outputs, agents):
    agents["requirements"].side_effect = [question(outputs[0]), outputs[0]]
    page = PageState()
    assert start_task(page, REQUEST, True, "saved-model", durable=True)
    wait_page(page)
    assert page.waiting and not page.running and page.result is None and page.error is None
    reopened = PageState()
    assert open_durable_task(reopened, page.durable_thread_id)
    assert reopened.waiting and reopened.checkpoint_id == page.checkpoint_id
    assert resume_task(reopened, True, answers={"Q-1": "支持负数"})
    wait_page(reopened)
    assert reopened.result.status == "completed" and reopened.persisted_run_id == reopened.result.run_id
    assert not reopened.waiting


def test_web_waiting_form_and_resume_are_explicit(outputs, agents):
    agents["requirements"].side_effect = [question(outputs[0]), outputs[0]]
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10).run()
    at.checkbox(key="durable_enabled").check().run()
    at.text_area(key="request_input").input(REQUEST).run()
    at.button(key="start_workflow").click().run()
    page = at.session_state[SESSION_KEY]
    page.task.thread.join(timeout=10)
    at.run()
    assert not at.exception and page.waiting and page.result is None
    at.run()
    assert agents["requirements"].call_count == 1
    answer = next(item for item in at.text_area if "是否需要处理负数" in item.label)
    answer.input("支持负数")
    next(item for item in at.button if item.label == "提交澄清并继续").click().run()
    page.task.thread.join(timeout=10)
    at.run()
    assert not at.exception and page.result is not None, (page.error, page.running, page.waiting, page.snapshot)
    assert page.result.status == "completed"
    assert agents["requirements"].call_count == 2


def test_real_process_crash_resumes_committed_stages_and_charges_unknown_call(store, outputs, tmp_path, agents):
    req, design, code = outputs
    data = tmp_path / "fixture.json"
    data.write_text(json.dumps({"req": req.model_dump(), "design": design.model_dump()}), encoding="utf-8")
    identifier_file = tmp_path / "session.txt"
    script = r'''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from workflow_studio.budget import model_call
from workflow_studio.state import RequirementsOutput, DesignOutput
from workflow_studio.storage.checkpoints import CheckpointStore
from workflow_studio.workflows.durable import create_durable, execute_durable
root, fixture, output = map(Path, sys.argv[1:])
data=json.loads(fixture.read_text(encoding="utf-8"))
req=RequirementsOutput.model_validate(data["req"])
design=DesignOutput.model_validate(data["design"])
def crash(*args, **kwargs):
    with model_call():
        os._exit(23)
store=CheckpointStore(root)
with patch("workflow_studio.workflows.nodes.analyze_requirements",return_value=req), patch("workflow_studio.workflows.nodes.design_system",return_value=design), patch("workflow_studio.workflows.nodes.generate_code",side_effect=crash):
    view=create_durable(store,"使用 Python 标准库提供整数加法。")
    output.write_text(view.thread_id,encoding="utf-8")
    execute_durable(store,view.thread_id,expected_checkpoint_id=view.checkpoint_id)
'''
    process = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(store.root), str(data), str(identifier_file)], capture_output=True, text=True, timeout=30)
    assert process.returncode == 23, process.stderr
    identifier = identifier_file.read_text(encoding="utf-8")
    view = inspect_durable(store, identifier)
    assert view.status == "running" and view.state["design"] is not None and view.state["code"] is None
    assert view.budget.in_flight_calls == 1 and view.budget.logical_calls == 1
    agents["requirements"].side_effect = AssertionError("committed requirements must not repeat")
    agents["design"].side_effect = AssertionError("committed design must not repeat")
    done = execute(store, view)
    assert done.status == "completed" and done.attempt_id == view.attempt_id
    assert done.budget.unknown_usage_calls == 2  # Unsettled crash + resumed mocked model call.
    assert done.budget.in_flight_calls == 0 and done.budget.logical_calls == 2
    agents["requirements"].assert_not_called()
    agents["design"].assert_not_called()
    assert list_durable(store)[0].run_id == done.run_id
