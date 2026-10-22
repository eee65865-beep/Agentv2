"""Static quality gate integration: real graph/storage/UI, mocked model only."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from workflow_studio.agents.coding_agent import CodingInputError, generate_code
from workflow_studio.agents.review_agent import ReviewOutputError, review_code
from workflow_studio.config import Settings
from workflow_studio.errors import CorruptRunError
from workflow_studio.exporters.documents import workflow_report
from workflow_studio.llm.errors import LLMStructuredOutputUnsupported
from workflow_studio.state import CodeFile, CodingOutput, DesignOutput, ModuleDesign, RequirementsOutput
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.review import ReviewAssessmentV2, ReviewOutput, ReviewRecord
from workflow_studio.state.review_policy import ReviewBindingError, bind_review, evaluate_review, validate_review_binding
from workflow_studio.storage import RunStore
from workflow_studio.web.app import SESSION_KEY
from workflow_studio.workflows import run_workflow, stream_workflow

REQUEST = "实现两个整数相加的 Python 工具，只使用标准库。"


@pytest.fixture
def inputs():
    req = RequirementsOutput(
        project_name="加法", summary="整数加法", functional_requirements=["整数相加"],
        non_functional_requirements=[], constraints=["只使用标准库"], assumptions=[], acceptance_criteria=["2+3=5"],
        contract_version=2,
        requirement_details=[{"id": "FR-7", "description": "整数相加", "source_quotes": ["两个整数相加"]}],
        acceptance_details=[{"id": "AC-3", "description": "2+3=5", "requirement_ids": ["FR-7"]}],
    )
    design = DesignOutput(
        architecture_overview="单模块", technology_stack=["Python"],
        modules=[ModuleDesign(name="calculator", responsibility="加法", dependencies=[], requirement_ids=["FR-7"])],
        data_models=[], interfaces=["add(a,b)"], project_structure="calculator.py", design_decisions=["保持简单"], preserved_constraints=req.constraints,
    )
    code = CodingOutput(
        files=[CodeFile(path="calculator.py", language="python", content="def add(a, b):\n    return a - b\n", description="加法")],
        instructions="手工运行", dependencies=[], test_suggestions=["验证 2+3=5"], limitations=["未执行测试"],
    )
    return req, design, code


@pytest.fixture
def assessed_data():
    return {
        "contract_version": 2, "passed": False, "score": 40,
        "issue_details": [{
            "issue_id": "RV-1", "category": "implementation", "severity": "major", "message": "加法实现为减法",
            "file": "calculator.py", "line_start": 2, "line_end": 2, "evidence_quote": "return a - b",
            "requirement_ids": ["FR-7"], "acceptance_ids": ["AC-3"], "expected": "返回和", "observed": "返回差",
            "suggested_owner": "coding",
        }],
        "suggestions": ["使用加号"], "summary": "静态审查，未执行测试。",
    }


def bound(data, inputs):
    return bind_review(ReviewAssessmentV2.model_validate(data), *inputs)


@pytest.mark.parametrize("severity", ["blocker", "major"])
@pytest.mark.parametrize("score", [0, 100])
def test_model_approval_and_score_cannot_override_blocking_issues(inputs, assessed_data, severity, score):
    assessed_data.update(passed=True, score=score)
    assessed_data["issue_details"][0]["severity"] = severity
    decision = evaluate_review(bound(assessed_data, inputs))
    assert not decision.passed and decision.action == "code_revision"
    assert decision.blocking_issue_ids == ("RV-1",)
    assert decision.verification_status == "not_run"


@pytest.mark.parametrize("severity", ["minor", "suggestion"])
def test_nonblocking_advice_does_not_override_model_approval(inputs, assessed_data, severity):
    assessed_data["passed"] = True
    assessed_data["issue_details"][0]["severity"] = severity
    assert evaluate_review(bound(assessed_data, inputs)).passed
    assessed_data["passed"] = False
    assert not evaluate_review(bound(assessed_data, inputs)).passed


@pytest.mark.parametrize("category,owner", [("requirement", "coding"), ("design", "coding"), ("implementation", "design"), ("test", "requirements")])
def test_manual_routing_cannot_be_downgraded_to_coding(inputs, assessed_data, category, owner):
    assessed_data["issue_details"][0].update(category=category, suggested_owner=owner)
    assert evaluate_review(bound(assessed_data, inputs)).action == "manual_required"


@pytest.mark.parametrize("problem", ["duplicate_id", "unknown_fr", "unknown_ac", "duplicate_ref", "unknown_file", "unsafe_file", "line_outside", "line_reversed", "line_partial", "line_without_file", "quote_without_file", "fake_quote", "quote_outside_range"])
def test_rejects_invalid_issue_references_and_locations(inputs, assessed_data, problem):
    issue = assessed_data["issue_details"][0]
    if problem == "duplicate_id":
        assessed_data["issue_details"].append(deepcopy(issue))
    elif problem == "unknown_fr":
        issue["requirement_ids"] = ["FR-1"]
    elif problem == "unknown_ac":
        issue["acceptance_ids"] = ["AC-99"]
    elif problem == "duplicate_ref":
        issue["requirement_ids"] *= 2
    elif problem == "unknown_file":
        issue["file"] = "missing.py"
    elif problem == "unsafe_file":
        issue["file"] = "../secret.py"
    elif problem == "line_outside":
        issue.update(line_start=100, line_end=101)
    elif problem == "line_reversed":
        issue.update(line_start=2, line_end=1)
    elif problem == "line_partial":
        issue["line_end"] = None
    elif problem == "line_without_file":
        issue.update(file=None, evidence_quote=None)
    elif problem == "quote_without_file":
        issue.update(file=None, line_start=None, line_end=None)
    elif problem == "fake_quote":
        issue["evidence_quote"] = "private-invented-code"
    elif problem == "quote_outside_range":
        issue.update(line_start=1, line_end=1)
    with pytest.raises((ValidationError, ReviewBindingError)) as error:
        bound(assessed_data, inputs)
    assert "private-invented-code" not in str(error.value)


def test_missing_file_can_be_described_without_inventing_location(inputs, assessed_data):
    assessed_data["issue_details"][0].update(file=None, line_start=None, line_end=None, evidence_quote=None)
    assert not evaluate_review(bound(assessed_data, inputs)).passed


def test_crlf_quotes_keep_original_line_endings(inputs, assessed_data):
    inputs[2].files[0].content = "def add(a, b):\r\n    return a - b\r\n"
    assessed_data["issue_details"][0].update(line_start=1, line_end=2, evidence_quote="def add(a, b):\r\n    return a - b")
    assert bound(assessed_data, inputs).artifact_digest


@pytest.mark.parametrize("change", ["code", "requirements", "design"])
def test_rejects_stale_artifact_or_context(inputs, assessed_data, change):
    review = bound(assessed_data, inputs)
    if change == "code":
        inputs[2].files[0].content = "def add(a, b):\n    return a + b\n"
    elif change == "requirements":
        inputs[0].summary = "更新的需求"
    else:
        inputs[1].architecture_overview = "更新的设计"
    with pytest.raises(ReviewBindingError):
        validate_review_binding(review, *inputs)
    if change == "code":
        with pytest.raises(ValidationError):
            ReviewRecord(run_id="00000000-0000-0000-0000-000000000001", revision_count=0, review=review, code=inputs[2])


@pytest.mark.parametrize("field", ["artifact_digest", "context_digest", "policy_version", "decision", "verification_status"])
def test_model_cannot_supply_trusted_binding_or_execution_evidence(assessed_data, field):
    assessed_data[field] = "forged"
    with pytest.raises(ValidationError):
        ReviewAssessmentV2.model_validate(assessed_data)


@pytest.mark.parametrize("value", [True, "2", 2.0, 1])
def test_generation_version_is_strict(assessed_data, value):
    assessed_data["contract_version"] = value
    with pytest.raises(ValidationError):
        ReviewAssessmentV2.model_validate(assessed_data)


def test_new_generation_cannot_fall_back_to_legacy_verdict(inputs, monkeypatch):
    legacy = ReviewOutput(passed=True, score=100, issues=[], suggestions=[], summary="静态审查")
    client = MagicMock()
    client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    client.invoke.return_value = AIMessage(content=legacy.model_dump_json())
    monkeypatch.setattr("workflow_studio.agents.review_agent.get_llm", lambda: client)
    with pytest.raises(ReviewOutputError):
        review_code(*inputs, max_repair_attempts=0)
    assert client.invoke.call_count == 1


@pytest.mark.parametrize("native", [True, False])
def test_invalid_reference_is_repaired_with_safe_feedback(inputs, assessed_data, monkeypatch, native):
    invalid = deepcopy(assessed_data)
    invalid["issue_details"][0]["evidence_quote"] = "private-invented-code"
    client = MagicMock()
    if native:
        client.invoke_structured.return_value = ReviewAssessmentV2.model_validate(invalid)
        client.invoke.return_value = AIMessage(content=json.dumps(assessed_data))
    else:
        client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
        client.invoke.side_effect = [AIMessage(content=json.dumps(invalid)), AIMessage(content=json.dumps(assessed_data))]
    monkeypatch.setattr("workflow_studio.agents.review_agent.get_llm", lambda: client)
    result = review_code(*inputs)
    assert result == bound(assessed_data, inputs)
    assert client.invoke.call_count == (1 if native else 2)
    feedback = client.invoke.call_args.args[0].to_messages()[2].content
    assert "review_quote_not_in_referenced_code" in feedback
    assert "private-invented-code" not in feedback


@pytest.mark.parametrize("native", [True, False])
def test_size_limit_applies_to_native_and_json_paths(inputs, assessed_data, monkeypatch, native):
    issue = assessed_data["issue_details"][0]
    assessed_data["issue_details"] = [{**issue, "issue_id": f"RV-{index}", "message": "大" * 1000} for index in range(1, 20)]
    assessment = ReviewAssessmentV2.model_validate(assessed_data)
    client = MagicMock()
    if native:
        client.invoke_structured.return_value = assessment
    else:
        client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    client.invoke.return_value = AIMessage(content=assessment.model_dump_json())
    monkeypatch.setattr("workflow_studio.agents.review_agent.get_llm", lambda: client)
    with pytest.raises(ReviewOutputError):
        review_code(*inputs, max_repair_attempts=0)
    assert client.invoke.call_count == 1


def test_coding_uses_gate_not_model_approval_and_checks_binding(inputs, assessed_data, monkeypatch):
    assessed_data["passed"] = True
    review = bound(assessed_data, inputs)
    client = MagicMock()
    from workflow_studio.agents.incremental import repair_context
    from workflow_studio.state.patch import CodePatch, RepairPolicy
    req, design, code = inputs
    fixed = code.model_copy(deep=True)
    fixed.files[0].content = fixed.files[0].content.replace("return a - b", "return a + b")
    context = repair_context(req, design, code, review, None, RepairPolicy.replacements_only(code))
    client.invoke_structured.return_value = CodePatch.model_validate({**{key: context[key] for key in ("base_artifact_digest", "base_context_digest", "feedback_digest")}, "contract_version": 1,
        "changes": [{"operation": "replace", "path": fixed.files[0].path, "file": fixed.files[0].model_dump(), "reason": "修复示例", "issue_ids": ["RV-1"]}]})
    monkeypatch.setattr("workflow_studio.agents.coding_agent.get_llm", lambda: client)
    assert generate_code(inputs[0], inputs[1], review=review, previous_code=inputs[2]) == fixed
    prompt = client.invoke_structured.call_args.args[0].to_messages()[1].content
    assert "RV-1" in prompt and "major" in prompt and "return a - b" in prompt
    inputs[2].instructions = "新说明使绑定失效"
    with pytest.raises(CodingInputError):
        generate_code(inputs[0], inputs[1], review=review, previous_code=inputs[2])
    assert client.invoke_structured.call_count == 1


@pytest.fixture
def agents(inputs, monkeypatch):
    mocks = {}
    for key, method, value in [("requirements", "analyze_requirements", inputs[0]), ("design", "design_system", inputs[1]), ("coding", "generate_code", inputs[2]), ("review", "review_code", None)]:
        mocks[key] = MagicMock(return_value=value)
        monkeypatch.setattr(f"workflow_studio.workflows.nodes.{method}", mocks[key])
    return mocks


def test_graph_repairs_blocker_despite_high_model_approval(inputs, assessed_data, agents):
    assessed_data.update(passed=True, score=100)
    rejected = bound(assessed_data, inputs)
    fixed = inputs[2].model_copy(deep=True)
    fixed.files[0].content = "def add(a, b):\n    return a + b\n"
    clean = dict(assessed_data, issue_details=[])
    accepted = bound(clean, (inputs[0], inputs[1], fixed))
    agents["coding"].side_effect = [inputs[2], fixed]
    agents["review"].side_effect = [rejected, accepted]
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "completed" and result.revision_count == 1
    assert result.review_history[0].review.passed  # Model approved, program refused.
    assert not evaluate_review(result.review_history[0].review).passed
    assert evaluate_review(result.review).passed
    assert agents["coding"].call_args_list[1].kwargs["review"] == rejected


@pytest.mark.parametrize("budget", [0, 1, 3])
def test_graph_budget_cannot_be_bypassed_by_model_approval(inputs, assessed_data, agents, budget):
    assessed_data.update(passed=True, score=100)
    agents["review"].return_value = bound(assessed_data, inputs)
    result = run_workflow(REQUEST, review_enabled=True, max_revisions=budget)
    assert result.status == "completed_with_issues" and result.error is None
    assert result.revision_count == budget and agents["coding"].call_count == budget + 1


def test_manual_issue_stops_without_spending_code_revision_budget(inputs, assessed_data, agents, monkeypatch):
    assessed_data["issue_details"][0].update(category="design", suggested_owner="coding")
    review = bound(assessed_data, inputs)
    agents["review"].return_value = review
    states = list(stream_workflow(REQUEST, review_enabled=True, max_revisions=3))
    result = WorkflowResult.from_state(states[-1])
    assert result.status == "completed_with_issues" and result.error is None
    assert result.revision_count == 0 and result.review_status == "failed"
    agents["coding"].assert_called_once()
    client = MagicMock()
    monkeypatch.setattr("workflow_studio.agents.coding_agent.get_llm", lambda: client)
    with pytest.raises(CodingInputError):
        generate_code(inputs[0], inputs[1], review=review, previous_code=inputs[2])
    client.invoke_structured.assert_not_called()


def test_stale_review_is_execution_failure_not_valid_rejection(inputs, assessed_data, agents):
    review = bound(assessed_data, inputs)
    inputs[2].files[0].content = "def add(a, b):\n    return a + b\n"
    agents["review"].return_value = review
    result = run_workflow(REQUEST, review_enabled=True)
    assert result.status == "failed" and result.review_status == "error"
    assert result.review_history == [] and result.review is None
    agents["coding"].assert_called_once()


def test_persistence_exports_binding_and_rejects_context_tampering(inputs, assessed_data, agents, tmp_path):
    assessed_data["passed"] = True
    agents["review"].return_value = bound(assessed_data, inputs)
    result = run_workflow(REQUEST, review_enabled=True, max_revisions=0)
    store = RunStore(tmp_path / "history")
    directory = store.save(result, REQUEST, "mock", 0)
    assert store.load(result.run_id).result == result
    report = workflow_report(result, REQUEST, "mock", 0)
    assert "模型判断：通过" in report and "程序静态门禁：未通过" in report
    assert "RV-1 · major · implementation" in report
    assert "真实测试：未执行" in report
    assert result.review.artifact_digest in report
    path = directory / "design.json"
    design = json.loads(path.read_text(encoding="utf-8"))
    design["architecture_overview"] = "另一份设计"
    body = json.dumps(design).encode("utf-8")
    path.write_bytes(body)
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"]["design.json"] = sha256(body).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CorruptRunError):
        store.load(result.run_id)


def test_v1_history_keeps_original_bytes_and_does_not_invent_severity(agents, tmp_path):
    agents["review"].return_value = ReviewOutput(passed=True, score=90, issues=[], suggestions=[], summary="旧版静态审查")
    result = run_workflow(REQUEST, review_enabled=True)
    store = RunStore(tmp_path / "history")
    directory = store.save(result, REQUEST, "mock", 0)
    path = directory / "review.json"
    archive = json.loads(path.read_text(encoding="utf-8"))
    added = {"contract_version", "issue_details", "artifact_digest", "context_digest", "policy_version"}
    for review in [archive["latest"], *(item["review"] for item in archive["history"])]:
        for field in added:
            review.pop(field)
    body = json.dumps(archive).encode("utf-8")
    path.write_bytes(body)
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"]["review.json"] = sha256(body).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    before = metadata_path.read_bytes()
    loaded = store.load(result.run_id).result
    assert loaded == result and loaded.review.issue_details == []
    assert loaded.review.artifact_digest is None
    assert evaluate_review(loaded.review).policy_version == "legacy-static"
    assert store.save(loaded, REQUEST, "mock", 0) == directory
    assert path.read_bytes() == body and metadata_path.read_bytes() == before


def test_forged_terminal_status_cannot_override_static_gate(inputs, assessed_data, agents):
    assessed_data.update(passed=True, score=100)
    agents["review"].return_value = bound(assessed_data, inputs)
    result = run_workflow(REQUEST, review_enabled=True, max_revisions=0)
    forged = result.model_dump()
    forged.update(status="completed", review_status="passed")
    with pytest.raises(ValidationError, match="review_status_does_not_match_verdict"):
        WorkflowResult.model_validate(forged)


def test_clean_native_review_gets_program_binding_without_claiming_tests(inputs, assessed_data, monkeypatch):
    assessed_data.update(passed=True, score=0, issue_details=[])
    client = MagicMock()
    client.invoke_structured.return_value = ReviewAssessmentV2.model_validate(assessed_data)
    monkeypatch.setattr("workflow_studio.agents.review_agent.get_llm", lambda: client)
    review = review_code(*inputs)
    assert review == bound(assessed_data, inputs)
    assert evaluate_review(review).passed
    assert evaluate_review(review).verification_status == "not_run"
    _, schema_type = client.invoke_structured.call_args.args
    schema = schema_type.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert "artifact_digest" not in schema["properties"]
    client.invoke.assert_not_called()


@pytest.mark.parametrize("manual", [True, False])
def test_web_distinguishes_model_opinion_from_gate(inputs, assessed_data, agents, monkeypatch, manual):
    assessed_data.update(passed=True, score=100)
    if manual:
        assessed_data["issue_details"][0].update(category="requirement", suggested_owner="coding")
    agents["review"].return_value = bound(assessed_data, inputs)
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="test-only", deepseek_model="mock"))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10).run()
    at.checkbox(key="review_enabled").check().run()
    at.number_input(key="max_revisions").set_value(3 if manual else 0).run()
    at.text_area(key="request_input").input(REQUEST).run()
    at.button(key="start_workflow").click().run()
    page = at.session_state[SESSION_KEY]
    page.task.thread.join(timeout=10)
    assert not page.task.thread.is_alive()
    at.run()
    assert not at.exception and page.result.status == "completed_with_issues"
    markdown = "\n".join(item.value for item in at.markdown)
    assert "模型判断：通过" in markdown and "程序静态门禁：未通过" in markdown
    assert "RV-1" in markdown and "major" in markdown
    assert not any("通过" in item.value for item in at.success)
    if manual:
        assert any("需求或设计问题" in item.value for item in at.warning)
        assert not any("已达到修复次数上限" in item.value for item in at.warning)
    at.run()
    agents["review"].assert_called_once()
