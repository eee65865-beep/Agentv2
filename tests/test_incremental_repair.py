"""Atomic file patches and real workflow integration; model/containers mocked."""

import json
from hashlib import sha256
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from workflow_studio.agents.coding_agent import CodingInputError, CodingOutputError, generate_code
from workflow_studio.agents.incremental import repair_context
from workflow_studio.budget import BudgetTracker, RunBudget, RunStopped, budget_scope, current_budget
from workflow_studio.exporters.documents import workflow_report
from workflow_studio.llm.errors import LLMNetworkError, LLMStructuredOutputUnsupported, LLMTruncatedOutputError
from workflow_studio.llm.repairs import collect_repairs
from workflow_studio.state.coding import CodeFile, CodingOutput, MAX_OUTPUT_BYTES
from workflow_studio.state.patch import CodePatch, RepairPolicy, PatchError, apply_code_patch
from workflow_studio.state.review import ReviewAssessmentV2
from workflow_studio.state.review_policy import bind_review
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.workflow import validate_state
from workflow_studio.storage import RunStore
from workflow_studio.storage.checkpoints import CheckpointStore
from workflow_studio.validation.docker import Capture
from workflow_studio.validation.evaluation import candidate_result
from workflow_studio.validation.repair import regression_cases, repair_and_validate
from workflow_studio.validation.service import validate_artifact, validation_quality
from workflow_studio.validation.store import ValidationStore
from workflow_studio.validation.suites import get_suite
from workflow_studio.workflows import run_workflow
from workflow_studio.workflows.durable import create_durable, execute_durable

REQUEST = "使用 Python 标准库实现整数加法工具。"


@pytest.fixture
def inputs():
    code = CodingOutput(files=[CodeFile(path="main.py", language="python", content="def add(a,b):\n    return a-b\n", description="加法"),
                               CodeFile(path="README.md", language="markdown", content="保持原文\n" * 2000, description="完整文档")],
                        instructions="手工执行", dependencies=[], test_suggestions=["2+3=5"], limitations=["未验证"])
    result = candidate_result(code, get_suite("add-cli"))
    review = bind_review(ReviewAssessmentV2(contract_version=2, passed=False, score=40, suggestions=["修复加法"], summary="静态审查，未执行。", issue_details=[
        {"issue_id": "RV-1", "category": "implementation", "severity": "major", "message": "加法写为减法", "file": "main.py", "line_start": 2, "line_end": 2,
         "evidence_quote": "return a-b", "requirement_ids": ["FR-1"], "acceptance_ids": [], "expected": "返回和", "observed": "返回差", "suggested_owner": "coding"}]), result.requirements, result.design, code)
    return result, review


def patch_for(result, review=None, report=None, *, policy=None, content="def add(a,b):\n    return a+b\n"):
    policy = policy or RepairPolicy.replacements_only(result.code)
    context = repair_context(result.requirements, result.design, result.code, review, report, policy)
    file = result.code.files[0].model_copy(update={"content": content})
    patch = CodePatch.model_validate({"contract_version": 1, **{key: context[key] for key in ("base_artifact_digest", "base_context_digest", "feedback_digest")},
        "changes": [{"operation": "replace", "path": file.path, "file": file.model_dump(), "reason": "修复加法", "issue_ids": [context["allowed_issue_ids"][0]]}]})
    return patch, context


def apply(base, patch, context, policy=None):
    return apply_code_patch(base, patch, context_digest=context["base_context_digest"], feedback_digest=context["feedback_digest"], allowed_issue_ids=context["allowed_issue_ids"], policy=policy)


@pytest.fixture
def client(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("workflow_studio.agents.coding_agent.get_llm", factory)
    factory.return_value.factory = factory
    return factory.return_value


def test_replace_preserves_unmentioned_files_metadata_and_source(inputs):
    result, review = inputs
    before = result.model_dump_json()
    patch, context = patch_for(result, review)
    fixed = apply(result.code, patch, context)
    assert fixed.files[0].content.endswith("return a+b\n")
    assert fixed.files[1] == result.code.files[1]
    assert fixed.files[1].content.encode() == result.code.files[1].content.encode()
    assert fixed.instructions == result.code.instructions and fixed.dependencies == result.code.dependencies
    assert result.model_dump_json() == before


@pytest.mark.parametrize("field", ["base_artifact_digest", "base_context_digest", "feedback_digest"])
def test_rejects_wrong_version_binding_without_changing_base(inputs, field):
    result, review = inputs
    patch, context = patch_for(result, review)
    before = result.code.model_dump_json()
    with pytest.raises(PatchError):
        apply(result.code, patch.model_copy(update={field: "0" * 64}), context)
    assert result.code.model_dump_json() == before


@pytest.mark.parametrize("problem", ["escape", "absolute", "sensitive", "case_duplicate", "path_mismatch", "syntax", "version_bool", "extra_metadata", "empty_changes"])
def test_patch_schema_rejects_unsafe_or_undeclared_changes(inputs, problem):
    result, review = inputs
    data = patch_for(result, review)[0].model_dump()
    item = data["changes"][0]
    if problem in ("escape", "absolute", "sensitive"):
        item["path"] = {"escape": "../main.py", "absolute": "C:/main.py", "sensitive": ".env"}[problem]
        item["file"]["path"] = item["path"]
    elif problem == "case_duplicate":
        second = json.loads(json.dumps(item))
        second["path"] = second["file"]["path"] = "MAIN.py"
        data["changes"].append(second)
    elif problem == "path_mismatch":
        item["file"]["path"] = "other.py"
    elif problem == "syntax":
        item["file"]["content"] = "def truncated("
    elif problem == "version_bool":
        data["contract_version"] = True
    elif problem == "extra_metadata":
        data["dependencies"] = ["requests"]
    else:
        data["changes"] = []
    with pytest.raises(ValidationError):
        CodePatch.model_validate(data)


@pytest.mark.parametrize("problem", ["unknown_issue", "case_rename", "no_change", "create", "delete", "restricted"])
def test_application_rejects_unknown_scope_and_noops(inputs, problem):
    result, review = inputs
    patch, context = patch_for(result, review)
    data = patch.model_dump()
    change = data["changes"][0]
    policy = None
    if problem == "unknown_issue":
        change["issue_ids"] = ["RV-99"]
    elif problem == "case_rename":
        change["path"] = change["file"]["path"] = "MAIN.py"
    elif problem == "no_change":
        change["file"] = result.code.files[0].model_dump()
    elif problem == "create":
        change["operation"] = "create"
        change["path"] = change["file"]["path"] = "helper.py"
    elif problem == "delete":
        change.update(operation="delete", file=None)
    else:
        policy = RepairPolicy(replace_paths=("README.md",))
    with pytest.raises(PatchError):
        apply(result.code, CodePatch.model_validate(data), context, policy)


def test_explicit_create_delete_and_final_file_collision_validation(inputs):
    result, review = inputs
    policy = RepairPolicy(replace_paths=("main.py",), create_paths=("helper.py",), delete_paths=("README.md",))
    patch, context = patch_for(result, review, policy=policy)
    data = patch.model_dump()
    data["changes"] += [
        {"operation": "create", "path": "helper.py", "file": {"path": "helper.py", "language": "python", "content": "VALUE = 1\n", "description": "辅助"}, "reason": "新增辅助", "issue_ids": ["RV-1"]},
        {"operation": "delete", "path": "README.md", "file": None, "reason": "显式授权删除", "issue_ids": ["RV-1"]},
    ]
    assert [file.path for file in apply(result.code, CodePatch.model_validate(data), context, policy).files] == ["main.py", "helper.py"]
    policy = RepairPolicy(replace_paths=("main.py",), create_paths=("main.py/helper.py",))
    data["changes"] = [data["changes"][1]]
    data["changes"][0]["path"] = data["changes"][0]["file"]["path"] = "main.py/helper.py"
    with pytest.raises(ValidationError):
        apply(result.code, CodePatch.model_validate(data), context, policy)


@pytest.mark.parametrize("limit", ["count", "size", "empty"])
def test_full_merged_artifact_limits_are_rechecked_atomically(inputs, limit):
    result, review = inputs
    if limit == "empty":
        policy = RepairPolicy(replace_paths=(), delete_paths=tuple(file.path for file in result.code.files))
        patch, context = patch_for(result, review, policy=policy)
        data = patch.model_dump()
        data["changes"] = [{"operation": "delete", "path": file.path, "file": None, "reason": "显式删除", "issue_ids": ["RV-1"]} for file in result.code.files]
        base = result.code
    else:
        extras = [CodeFile(path=f"data{index}.txt", language="text", content="x" * (65500 if limit == "size" else 1), description="不变数据") for index in range(2 if limit == "size" else 19)]
        base = result.code.model_copy(update={"files": [result.code.files[0], *extras]})
        create_paths = ("new0.txt", "new1.txt") if limit == "size" else ("new.txt",)
        policy = RepairPolicy(replace_paths=(), create_paths=create_paths)
        # Review binding is irrelevant to this pure patch test; context is explicitly supplied.
        patch, context = patch_for(result, review)
        from workflow_studio.state.review_policy import artifact_digest
        data = patch.model_dump()
        data["base_artifact_digest"] = artifact_digest(base)
        data["changes"] = [{"operation": "create", "path": path, "file": {"path": path, "language": "text", "content": "y" * (65500 if limit == "size" else 1), "description": "新增数据"}, "reason": "新增", "issue_ids": ["RV-1"]} for path in create_paths]
    before = base.model_dump_json()
    with pytest.raises(ValidationError):
        apply(base, CodePatch.model_validate(data), context, policy)
    assert base.model_dump_json() == before


def test_native_repair_receipt_and_output_efficiency(inputs, client):
    result, review = inputs
    client.invoke_structured.return_value = patch_for(result, review)[0]
    with collect_repairs() as receipts:
        fixed = generate_code(result.requirements, result.design, previous_code=result.code, review=review)
    assert len(receipts) == 1 and receipts[0].preserved_file_count == 1
    assert receipts[0].normalized_patch_bytes < receipts[0].merged_artifact_bytes / 5
    assert receipts[0].format_failures == 0 and fixed.files[1] == result.code.files[1]
    formatted, schema = client.invoke_structured.call_args.args
    assert schema is CodePatch and "保持原文" in str(formatted)


def test_native_wrong_binding_uses_same_bounded_json_repair(inputs, client):
    result, review = inputs
    patch = patch_for(result, review)[0]
    client.invoke_structured.return_value = patch.model_copy(update={"feedback_digest": "0" * 64})
    client.invoke.return_value = AIMessage(content=patch.model_dump_json())
    tracker = BudgetTracker(RunBudget())
    with budget_scope(tracker), collect_repairs() as receipts:
        fixed = generate_code(result.requirements, result.design, previous_code=result.code, review=review)
    assert fixed.files[0].content.endswith("return a+b\n")
    assert receipts[0].format_failures == 1 and tracker.snapshot().format_repairs == 1
    assert client.invoke.call_count == 1


@pytest.mark.parametrize("failure", ["wrong_binding", "json", "oversized", "full_output"])
def test_invalid_patch_is_repaired_without_echoing_untrusted_response(inputs, client, failure):
    result, review = inputs
    good = patch_for(result, review)[0]
    client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("schema unavailable")
    bad = {"wrong_binding": good.model_copy(update={"base_artifact_digest": "0" * 64}).model_dump_json(),
           "json": "private-invalid-patch", "oversized": "private" * (MAX_OUTPUT_BYTES // 3), "full_output": result.code.model_dump_json()}[failure]
    client.invoke.side_effect = [AIMessage(content=bad), AIMessage(content=good.model_dump_json())]
    tracker = BudgetTracker(RunBudget())
    with budget_scope(tracker), collect_repairs() as receipts:
        fixed = generate_code(result.requirements, result.design, previous_code=result.code, review=review)
    assert fixed.files[1] == result.code.files[1] and receipts[0].format_failures == 1
    assert tracker.snapshot().logical_calls == 3 and tracker.snapshot().format_repairs == 1
    assert "private-invalid-patch" not in str(client.invoke.call_args.args[0])
    assert current_budget() is None


@pytest.mark.parametrize("error", [LLMNetworkError, LLMTruncatedOutputError])
def test_transport_and_truncation_are_not_format_retries(inputs, client, error):
    result, review = inputs
    client.invoke_structured.side_effect = error("private provider response")
    with pytest.raises(error):
        generate_code(result.requirements, result.design, previous_code=result.code, review=review)
    client.invoke.assert_not_called()


def test_bounded_format_failures_and_global_budget(inputs, client):
    result, review = inputs
    client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    client.invoke.return_value = AIMessage(content="invalid private patch")
    with pytest.raises(CodingOutputError):
        generate_code(result.requirements, result.design, previous_code=result.code, review=review, max_repair_attempts=1)
    assert client.invoke.call_count == 2
    tracker = BudgetTracker(RunBudget(max_logical_calls=1))
    with budget_scope(tracker), pytest.raises(RunStopped):
        generate_code(result.requirements, result.design, previous_code=result.code, review=review)
    assert tracker.snapshot().logical_calls == 1


def test_invalid_caller_policy_is_rejected_before_client_creation(inputs, client):
    result, review = inputs
    policy = RepairPolicy(replace_paths=("unknown.py",))
    with pytest.raises(CodingInputError):
        generate_code(result.requirements, result.design, previous_code=result.code, review=review, repair_policy=policy)
    client.factory.assert_not_called()


def setup_graph(inputs, client, monkeypatch):
    original, review = inputs
    req, design, base = original.requirements, original.design, original.code
    patch, context = patch_for(original, review)
    fixed = apply(base, patch, context)
    client.invoke_structured.side_effect = [base, patch]
    monkeypatch.setattr("workflow_studio.workflows.nodes.analyze_requirements", lambda *a, **k: req)
    monkeypatch.setattr("workflow_studio.workflows.nodes.design_system", lambda *a, **k: design)
    passed = bind_review(ReviewAssessmentV2(contract_version=2, passed=True, score=95, issue_details=[], suggestions=[], summary="静态检查；未运行。"), req, design, fixed)
    monkeypatch.setattr("workflow_studio.workflows.nodes.review_code", MagicMock(side_effect=[review, passed]))
    return fixed


def test_real_graph_roundtrip_receipt_export_and_tamper_rejection(inputs, client, monkeypatch, tmp_path):
    fixed = setup_graph(inputs, client, monkeypatch)
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "completed" and result.code == fixed
    receipts = [event for event in result.events if event.repair_receipt is not None]
    assert len(receipts) == 1 and result.budget.logical_calls == 2
    assert "增量修复" in workflow_report(result, REQUEST, "mock", 1)
    store = RunStore(tmp_path / "outputs")
    path = store.save(result, request=REQUEST, model_name="mock", elapsed=1)
    before = {file.name: file.read_bytes() for file in path.iterdir()}
    assert store.load(result.run_id).result == result
    store.save(result, request=REQUEST, model_name="mock", elapsed=1)
    assert before == {file.name: file.read_bytes() for file in path.iterdir()}
    data = result.model_dump()
    event = next(item for item in data["events"] if item["repair_receipt"])
    event["repair_receipt"]["result_artifact_digest"] = "0" * 64
    with pytest.raises(ValidationError):
        WorkflowResult.model_validate(data)


def test_legacy_events_without_receipt_remain_byte_identical_on_save(inputs, client, monkeypatch, tmp_path):
    setup_graph(inputs, client, monkeypatch)
    result = run_workflow(REQUEST)
    store = RunStore(tmp_path / "legacy")
    folder = store.save(result, request=REQUEST, model_name="mock", elapsed=1)
    events = json.loads((folder / "events.json").read_text(encoding="utf-8"))
    for event in events:
        event.pop("repair_receipt", None)
    payload = json.dumps(events, ensure_ascii=False, indent=2).encode()
    (folder / "events.json").write_bytes(payload)
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    metadata["sha256"]["events.json"] = sha256(payload).hexdigest()
    (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    original = {path.name: path.read_bytes() for path in folder.iterdir()}
    saved = store.load(result.run_id)
    assert all(event.repair_receipt is None for event in saved.result.events)
    store.save(saved.result, request=REQUEST, model_name="mock", elapsed=1)
    assert original == {path.name: path.read_bytes() for path in folder.iterdir()}


def test_durable_graph_serializes_patch_receipt(inputs, client, monkeypatch, tmp_path):
    fixed = setup_graph(inputs, client, monkeypatch)
    store = CheckpointStore(tmp_path / "checkpoint")
    try:
        initial = create_durable(store, REQUEST, review_enabled=True)
        view = execute_durable(store, initial.thread_id, expected_checkpoint_id=initial.checkpoint_id)
        assert view.status == "completed" and view.result.code == fixed
        assert any(event.repair_receipt for event in view.result.events)
    finally:
        store.close()


def test_stop_after_successful_patch_retains_new_code_and_receipt(inputs, client, monkeypatch):
    fixed = setup_graph(inputs, client, monkeypatch)
    base, review = inputs
    patch = patch_for(base, review)[0]
    cancel = Event()
    def respond(prompt, schema):
        if schema is CodingOutput:
            return base.code
        cancel.set()
        return patch
    client.invoke_structured.side_effect = respond
    result = run_workflow(REQUEST, review_enabled=True, cancel_event=cancel)
    assert result.status == "stopped" and result.code == fixed
    assert result.budget.stop_reason == "cancelled"
    event = next(event for event in result.events if event.repair_receipt is not None)
    assert event.event_type == "stopped" and event.revision_count == 1


class Backend:
    environment = "python@sha256:" + "a" * 64

    def __init__(self, statuses):
        self.statuses = statuses
        self.index = -1

    def prepare(self, *args, **kwargs):
        self.index += 1

    def run_case(self, suite, case, *args, **kwargs):
        failed = self.statuses[min(self.index, len(self.statuses) - 1)]
        return Capture(0, b"{}" if case.id in failed else case.expected.encode()), True


def test_validation_failed_allows_repair_and_invalidates_old_evidence(inputs, client):
    result, _ = inputs
    report = validate_artifact(result, "add-cli", backend=Backend([{"positive"}]))
    client.invoke_structured.return_value = patch_for(result, report=report)[0]
    fixed = generate_code(result.requirements, result.design, previous_code=result.code, validation_report=report)
    assert validation_quality(result.model_copy(update={"code": fixed}), report) == "stale"
    client.factory.reset_mock()
    with pytest.raises(CodingInputError):
        generate_code(result.requirements, result.design, previous_code=fixed, validation_report=report)
    client.factory.assert_not_called()


def test_validation_repair_loop_keeps_parent_and_runs_fresh_regression(inputs, client, tmp_path):
    result, _ = inputs
    backend = Backend([{"positive"}, set()])
    def respond(formatted, schema):
        payload = str(formatted.to_messages()[1].content)
        context = json.loads(payload.split("<repair_context>")[1].split("</repair_context>")[0])
        report = context["feedback"]["validation_report"]
        from workflow_studio.validation.models import ValidationReport
        return patch_for(result, report=ValidationReport.model_validate(report))[0]
    client.invoke_structured.side_effect = respond
    before = result.model_dump_json()
    run = repair_and_validate(result, "add-cli", backend=backend, store=ValidationStore(tmp_path / "reports"))
    assert run.status == "passed" and len(run.attempts) == 2
    assert run.attempts[1].parent_run_id == result.run_id and run.attempts[1].result.run_id != result.run_id
    assert run.attempts[1].regressions == () and run.attempts[1].receipt is not None
    assert run.attempts[1].result.max_revisions == 0 and run.attempts[1].result.budget.node_executions == 3
    assert run.metrics["comparable_rounds"] == 1 and run.budget.logical_calls == 1
    assert result.model_dump_json() == before


def test_regression_metric_rejects_missing_or_different_environment(inputs):
    result, _ = inputs
    before = validate_artifact(result, "add-cli", backend=Backend([{"positive"}]))
    after = validate_artifact(result, "add-cli", backend=Backend([{"negative"}]))
    assert regression_cases(before, after) == ("negative",)
    assert regression_cases(before, after.model_copy(update={"environment": "other"})) is None
    assert regression_cases(before, after.model_copy(update={"cases": after.cases[:1]})) is None


def test_loop_no_backend_or_zero_budget_does_not_call_model(inputs, client, tmp_path):
    from workflow_studio.validation.docker import DockerBackend
    result, _ = inputs
    run = repair_and_validate(result, "add-cli", backend=DockerBackend(image=""), store=ValidationStore(tmp_path / "reports"))
    assert run.status == "not_run" and len(run.attempts) == 1
    run = repair_and_validate(result, "add-cli", backend=Backend([{"positive"}]), budget=RunBudget(max_logical_calls=0), store=ValidationStore(tmp_path / "reports"))
    assert run.status == "stopped" and run.budget.logical_calls == 0
    client.factory.assert_not_called()


def test_loop_reports_regression_and_respects_business_round_limit(inputs, client, tmp_path):
    result, _ = inputs
    backend = Backend([{"positive"}, {"negative"}])
    def respond(prompt, schema):
        from workflow_studio.validation.models import ValidationReport
        context = json.loads(prompt.to_messages()[1].content.split("<repair_context>")[1].split("</repair_context>")[0])
        return patch_for(result, report=ValidationReport.model_validate(context["feedback"]["validation_report"]))[0]
    client.invoke_structured.side_effect = respond
    run = repair_and_validate(result, "add-cli", max_repair_rounds=3, backend=backend, store=ValidationStore(tmp_path / "reports"))
    assert run.status == "regression" and run.attempts[-1].regressions == ("negative",)
    assert run.metrics["regression_rounds"] == 1 and client.invoke_structured.call_count == 1


def test_loop_inherits_budget_and_preserves_unvalidated_candidate(inputs, client, tmp_path):
    result, _ = inputs
    backend = Backend([{"positive"}])
    initial = validate_artifact(result, "add-cli", backend=Backend([{"positive"}]))
    # Native response is built from each actual report to bind its unique ID/times.
    def respond(prompt, schema):
        from workflow_studio.validation.models import ValidationReport
        context = json.loads(prompt.to_messages()[1].content.split("<repair_context>")[1].split("</repair_context>")[0])
        return patch_for(result, report=ValidationReport.model_validate(context["feedback"]["validation_report"]))[0]
    client.invoke_structured.side_effect = respond
    run = repair_and_validate(result, "add-cli", backend=backend, budget=RunBudget(max_node_executions=2), store=ValidationStore(tmp_path / "reports"))
    assert run.status == "stopped" and len(run.attempts) == 1
    assert run.pending_candidate is not None and run.pending_receipt is not None
    assert run.pending_candidate.status == "stopped" and validation_quality(run.pending_candidate, initial) == "stale"
    tracker = BudgetTracker(RunBudget(max_logical_calls=0))
    inherited = result.model_copy(update={"budget": tracker.snapshot()})
    client.factory.reset_mock()
    stopped = repair_and_validate(inherited, "add-cli", backend=Backend([{"positive"}]), store=ValidationStore(tmp_path / "reports"))
    assert stopped.status == "stopped" and stopped.budget.limits.max_logical_calls == 0
    client.factory.assert_not_called()
    with pytest.raises(ValueError, match="override"):
        repair_and_validate(inherited, "add-cli", budget=RunBudget())


def test_page_shows_incremental_change_details(inputs, client, monkeypatch):
    from workflow_studio.web.app import SESSION_KEY
    from workflow_studio.web.controller import PageState
    setup_graph(inputs, client, monkeypatch)
    result = run_workflow(REQUEST, review_enabled=True)
    state = {**result.model_dump(), "user_request": REQUEST, "current_agent": "review_agent", "awaiting_questions": [], "design_feedback": None}
    state["events"] = result.events
    state["review_history"] = result.review_history
    page = PageState(result=result, snapshot=validate_state(state), events=list(result.events), review_enabled=True, request=REQUEST)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10)
    at.session_state[SESSION_KEY] = page
    at.run()
    at.selectbox(key="trace_agent_choice").select("coding_agent").run()
    assert not at.exception
    assert any("规范化补丁" in item.value for item in at.caption)


def test_pending_candidate_keeps_spent_validation_budget_on_backend_exception(inputs, client, tmp_path):
    result, _ = inputs
    class BrokenBackend(Backend):
        def prepare(self, *args, **kwargs):
            super().prepare(*args, **kwargs)
            if self.index == 1:
                raise RuntimeError("backend crashed after repair")
    def respond(prompt, schema):
        from workflow_studio.validation.models import ValidationReport
        context = json.loads(prompt.to_messages()[1].content.split("<repair_context>")[1].split("</repair_context>")[0])
        return patch_for(result, report=ValidationReport.model_validate(context["feedback"]["validation_report"]))[0]
    client.invoke_structured.side_effect = respond
    before = result.model_dump_json()
    run = repair_and_validate(result, "add-cli", backend=BrokenBackend([{"positive"}]), store=ValidationStore(tmp_path / "reports"))
    assert run.status == "error" and run.error_type == "RuntimeError"
    assert run.pending_candidate is not None and run.pending_receipt is not None
    assert run.pending_candidate.budget == run.budget
    assert run.pending_candidate.budget.node_executions == 3
    assert result.model_dump_json() == before
