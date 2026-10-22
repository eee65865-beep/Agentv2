"""Requirements Agent tests use a mocked shared client, never a paid API."""

import json
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from workflow_studio.agents.requirements_agent import (
    RequirementsInputError, RequirementsOutputError, analyze_requirements,
)
from workflow_studio.llm.errors import (
    LLMAuthenticationError, LLMCallError, LLMNetworkError, LLMRateLimitError,
    LLMStructuredOutputError, LLMStructuredOutputUnsupported,
)
from workflow_studio.state.requirements import RequirementsOutput, RequirementsOutputV2

REQUEST = "开发一个待办事项应用，支持新增任务、标记完成和删除任务，数据只保存在本地。"


@pytest.fixture
def output_data() -> dict[str, object]:
    return {
        "project_name": "待办事项应用",
        "summary": "管理本地待办任务，支持新增、完成和删除。",
        "functional_requirements": ["新增任务", "标记任务为已完成", "删除任务"],
        "non_functional_requirements": [],
        "constraints": ["数据只保存在本地"],
        "assumptions": ["假设：项目名称暂定为待办事项应用，待用户确认。"],
        "acceptance_criteria": ["新增任务后能看到该任务", "标记完成后可观察到完成状态", "删除后不再显示任务"],
        "contract_version": 2,
        "open_questions": [],
        "requirement_details": [
            {"id": "FR-1", "description": "新增任务", "source_quotes": ["新增任务"]},
            {"id": "FR-2", "description": "标记任务为已完成", "source_quotes": ["标记完成"]},
            {"id": "FR-3", "description": "删除任务", "source_quotes": ["删除任务"]},
        ],
        "acceptance_details": [
            {"id": "AC-1", "description": "新增任务后能看到该任务", "requirement_ids": ["FR-1"]},
            {"id": "AC-2", "description": "标记完成后可观察到完成状态", "requirement_ids": ["FR-2"]},
            {"id": "AC-3", "description": "删除后不再显示任务", "requirement_ids": ["FR-3"]},
        ],
    }


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    factory = MagicMock()
    monkeypatch.setattr("workflow_studio.agents.requirements_agent.get_llm", factory)
    client = factory.return_value
    client.factory = factory
    return client


def test_native_structured_output(output_data: dict[str, object], llm: MagicMock) -> None:
    expected = RequirementsOutput.model_validate(output_data)
    llm.invoke_structured.return_value = expected
    assert analyze_requirements(REQUEST).model_dump() == expected.model_dump()
    llm.factory.assert_called_once_with()
    llm.invoke.assert_not_called()
    llm.invoke_structured.assert_called_once()
    prompt, schema = llm.invoke_structured.call_args.args
    assert schema is RequirementsOutputV2
    messages = prompt.to_messages()
    assert messages[0].type == "system"
    assert messages[1].type == "human"
    assert REQUEST in messages[1].content
    system = messages[0].content
    for instruction in ["不编写代码", "assumptions", "已确认事实", "constraints", "functional_requirements"]:
        assert instruction in system


@pytest.mark.parametrize("user_input", ["", " \n\t", "做个应用", None, 42])
def test_invalid_request_before_model(user_input: object, llm: MagicMock) -> None:
    with pytest.raises(RequirementsInputError):
        analyze_requirements(user_input)  # type: ignore[arg-type]
    llm.factory.assert_not_called()


@pytest.mark.parametrize("budget", [-1, 4, 1.5, True])
def test_invalid_repair_budget(budget: object, llm: MagicMock) -> None:
    with pytest.raises(RequirementsInputError):
        analyze_requirements(REQUEST, max_repair_attempts=budget)  # type: ignore[arg-type]
    llm.factory.assert_not_called()


@pytest.mark.parametrize("reason", [LLMStructuredOutputUnsupported("unsupported"), LLMStructuredOutputError("invalid schema")])
def test_json_fallback(reason: Exception, output_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = reason
    llm.invoke.return_value = AIMessage(content=json.dumps(output_data, ensure_ascii=False))
    result = analyze_requirements(REQUEST)
    assert result.project_name == output_data["project_name"]
    assert result.constraints == ["数据只保存在本地"]
    llm.invoke.assert_called_once()


@pytest.mark.parametrize("bad_text", ["not JSON", '{"project_name": "truncated"', '{}', '[]'])
def test_invalid_json_is_repaired(bad_text: str, output_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = [AIMessage(content=bad_text), AIMessage(content=json.dumps(output_data))]
    result = analyze_requirements(REQUEST)
    assert result.functional_requirements == output_data["functional_requirements"]
    assert llm.invoke.call_count == 2
    repair_messages = llm.invoke.call_args.args[0].to_messages()
    assert repair_messages[0].type == "system"
    assert REQUEST in repair_messages[1].content
    assert bad_text in repair_messages[2].content
    assert "校验反馈" in repair_messages[2].content


@pytest.mark.parametrize("budget", [0, 2, 3])
def test_repair_budget_is_finite(budget: int, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputError("invalid schema")
    llm.invoke.return_value = AIMessage(content="private-invalid-output")
    with pytest.raises(RequirementsOutputError) as info:
        analyze_requirements(REQUEST, max_repair_attempts=budget)
    assert llm.invoke_structured.call_count == 1
    assert llm.invoke.call_count == 1 + budget
    assert "private-invalid-output" not in str(info.value)
    assert info.value.__suppress_context__


def test_fenced_json(output_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.return_value = AIMessage(content="```json\n" + json.dumps(output_data) + "\n```")
    assert analyze_requirements(REQUEST).acceptance_criteria == output_data["acceptance_criteria"]


@pytest.mark.parametrize("error_type", [LLMNetworkError, LLMAuthenticationError, LLMRateLimitError, LLMCallError])
def test_native_model_failure_does_not_fallback(error_type: type[Exception], llm: MagicMock) -> None:
    failure = error_type("Model call failed")
    llm.invoke_structured.side_effect = failure
    with pytest.raises(error_type) as info:
        analyze_requirements(REQUEST)
    assert info.value is failure
    llm.invoke.assert_not_called()


def test_json_model_failure_is_not_repaired(llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = LLMNetworkError("Network failed")
    with pytest.raises(LLMNetworkError):
        analyze_requirements(REQUEST)
    assert llm.invoke.call_count == 1


def test_schema_validation_repair(output_data: dict[str, object], llm: MagicMock) -> None:
    invalid = dict(output_data, functional_requirements=[123], constraints="not a list")
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = [AIMessage(content=json.dumps(invalid)), AIMessage(content=json.dumps(output_data))]
    assert isinstance(analyze_requirements(REQUEST), RequirementsOutput)
    feedback = llm.invoke.call_args.args[0].to_messages()[2].content
    assert "functional_requirements.0: string_type" in feedback
    assert "constraints: list_type" in feedback


def test_schema_fields_are_described_and_required(output_data: dict[str, object]) -> None:
    schema = RequirementsOutput.model_json_schema()
    assert len(schema["required"]) == 7
    assert all(field["description"] for field in schema["properties"].values())
    assert schema["additionalProperties"] is False
    assert RequirementsOutput.model_validate(output_data).non_functional_requirements == []


@pytest.mark.parametrize(("field", "value"), [
    ("summary", " "), ("functional_requirements", []), ("constraints", [" "]),
    ("acceptance_criteria", []), ("project_name", 123),
])
def test_schema_rejects_invalid_values(field: str, value: object, output_data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RequirementsOutput.model_validate(dict(output_data, **{field: value}))
