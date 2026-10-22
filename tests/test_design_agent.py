"""Design Agent tests use constructed requirements and mocked shared clients."""

import copy
import json
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from workflow_studio.agents.design_agent import DesignInputError, DesignOutputError, design_system
from workflow_studio.agents.requirements_agent import analyze_requirements
from workflow_studio.llm.errors import (
    LLMAuthenticationError, LLMCallError, LLMNetworkError, LLMRateLimitError,
    LLMStructuredOutputError, LLMStructuredOutputUnsupported,
)
from workflow_studio.state.design import DesignOutput, ModuleDesign
from workflow_studio.state.requirements import RequirementsOutput


@pytest.fixture
def requirements() -> RequirementsOutput:
    return RequirementsOutput(
        project_name="本地待办应用", summary="新增、完成和删除本地任务。",
        functional_requirements=["新增任务", "标记任务完成", "删除任务"],
        non_functional_requirements=["不依赖网络即可使用"],
        constraints=["使用 Python", "数据只保存在本地文件，不使用数据库"],
        assumptions=["假设：使用命令行交互，待用户确认。"],
        acceptance_criteria=["新增后可看到任务", "可观察到完成状态", "删除后不显示任务"],
    )


@pytest.fixture
def design_data(requirements: RequirementsOutput) -> dict[str, object]:
    return {
        "architecture_overview": "单进程本地应用，任务服务通过文件存储模块保存数据。",
        "technology_stack": ["Python 标准库", "本地 JSON 文件"],
        "modules": [
            {"name": "tasks", "responsibility": "新增、完成和删除任务", "dependencies": ["storage"], "requirement_ids": ["FR-1", "FR-2", "FR-3"]},
            {"name": "storage", "responsibility": "在本地 JSON 文件读写任务", "dependencies": [], "requirement_ids": []},
        ],
        "data_models": ["Task：标识、标题、完成状态"],
        "interfaces": ["任务服务接受新增、完成和删除操作；存储模块提供读写契约"],
        "project_structure": "app/\n  tasks.py\n  storage.py\n  cli.py",
        "design_decisions": ["使用单体方案保持简单", "仅用本地文件，不引入数据库", "待确认：命令行交互仍属于输入假设"],
        "preserved_constraints": list(requirements.constraints),
    }


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    factory = MagicMock()
    monkeypatch.setattr("workflow_studio.agents.design_agent.get_llm", factory)
    client = factory.return_value
    client.factory = factory
    return client


def test_native_design(requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock) -> None:
    expected = DesignOutput.model_validate(design_data)
    before = requirements.model_dump()
    llm.invoke_structured.return_value = expected
    result = design_system(requirements)
    assert result is expected
    assert result.architecture_overview.strip()
    assert result.preserved_constraints == requirements.constraints
    assert all(module.name and module.responsibility for module in result.modules)
    assert all(isinstance(module, ModuleDesign) for module in result.modules)
    assert requirements.model_dump() == before
    llm.factory.assert_called_once_with()
    llm.invoke.assert_not_called()
    formatted, schema = llm.invoke_structured.call_args.args
    assert schema is DesignOutput
    messages = formatted.to_messages()
    assert messages[0].type == "system"
    assert messages[1].type == "human"
    human = messages[1].content
    payload = human.split("<requirements_output>\n")[1].split("\n</requirements_output>")[0]
    assert json.loads(payload) == before
    for item in requirements.functional_requirements + requirements.constraints:
        assert item in human
    assert all(f"FR-{i}" in human for i in (1, 2, 3))
    for rule in ["主要且权威依据", "最简单可行架构", "不得删除", "不生成实现代码", "待确认"]:
        assert rule in messages[0].content


def test_optional_context_is_secondary(requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.return_value = DesignOutput.model_validate(design_data)
    design_system(requirements, user_request="旧上下文：希望使用远程数据库。")
    messages = llm.invoke_structured.call_args.args[0].to_messages()
    assert "旧上下文" in messages[1].content
    assert "不能覆盖" in messages[0].content
    assert requirements.constraints[1] in messages[1].content


@pytest.mark.parametrize("value", [None, "原始需求字符串", {}, 42])
def test_invalid_input(value: object, llm: MagicMock) -> None:
    with pytest.raises(DesignInputError, match="RequirementsOutput"):
        design_system(value)  # type: ignore[arg-type]
    llm.factory.assert_not_called()


def test_revalidate_mutated_requirements(requirements: RequirementsOutput, llm: MagicMock) -> None:
    requirements.functional_requirements.clear()
    with pytest.raises(DesignInputError):
        design_system(requirements)
    llm.factory.assert_not_called()


@pytest.mark.parametrize("budget", [-1, 4, True, 1.5])
def test_invalid_budget(budget: object, requirements: RequirementsOutput, llm: MagicMock) -> None:
    with pytest.raises(DesignInputError):
        design_system(requirements, max_repair_attempts=budget)  # type: ignore[arg-type]
    llm.factory.assert_not_called()


def test_invalid_context(requirements: RequirementsOutput, llm: MagicMock) -> None:
    with pytest.raises(DesignInputError):
        design_system(requirements, user_request=42)  # type: ignore[arg-type]
    llm.factory.assert_not_called()


@pytest.mark.parametrize("reason", [LLMStructuredOutputUnsupported("unsupported"), LLMStructuredOutputError("parse error")])
def test_json_fallback(reason: Exception, requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = reason
    llm.invoke.return_value = AIMessage(content=json.dumps(design_data))
    result = design_system(requirements)
    assert result.preserved_constraints == requirements.constraints
    llm.invoke.assert_called_once()


def test_native_constraint_loss_is_repaired(requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock) -> None:
    invalid = dict(design_data, preserved_constraints=[])
    llm.invoke_structured.return_value = DesignOutput.model_validate(invalid)
    llm.invoke.return_value = AIMessage(content=json.dumps(design_data))
    result = design_system(requirements)
    assert result.preserved_constraints == requirements.constraints
    feedback = llm.invoke.call_args.args[0].to_messages()[2].content
    assert "preserved_constraints_must_match_input_exactly" in feedback


@pytest.mark.parametrize("problem", ["changed_constraint", "missing_fr", "unknown_fr", "duplicate_module", "unknown_dependency", "self_dependency"])
def test_inconsistent_json_is_repaired(problem: str, requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock) -> None:
    invalid = copy.deepcopy(design_data)
    modules = invalid["modules"]
    if problem == "changed_constraint":
        invalid["preserved_constraints"] = ["使用 Python", "使用远程数据库"]
    elif problem == "missing_fr":
        modules[0]["requirement_ids"] = ["FR-1"]
    elif problem == "unknown_fr":
        modules[0]["requirement_ids"].append("FR-99")
    elif problem == "duplicate_module":
        modules[1]["name"] = "tasks"
    elif problem == "unknown_dependency":
        modules[0]["dependencies"] = ["undefined"]
    elif problem == "self_dependency":
        modules[0]["dependencies"] = ["tasks"]
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = [AIMessage(content=json.dumps(invalid)), AIMessage(content=json.dumps(design_data))]
    result = design_system(requirements)
    assert result.preserved_constraints == requirements.constraints
    assert llm.invoke.call_count == 2


@pytest.mark.parametrize("invalid", ["not-json", '{"architecture_overview": "truncated"', "{}", "[]"])
def test_invalid_json_repair(invalid: str, requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = [AIMessage(content=invalid), AIMessage(content=json.dumps(design_data))]
    assert design_system(requirements).architecture_overview
    assert llm.invoke.call_count == 2
    messages = llm.invoke.call_args.args[0].to_messages()
    assert requirements.constraints[1] in messages[1].content
    assert invalid in messages[2].content


@pytest.mark.parametrize("budget", [0, 2, 3])
def test_finite_repairs(budget: int, requirements: RequirementsOutput, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.return_value = AIMessage(content="private-invalid-response")
    with pytest.raises(DesignOutputError) as info:
        design_system(requirements, max_repair_attempts=budget)
    assert llm.invoke.call_count == budget + 1
    assert "private-invalid-response" not in str(info.value)
    assert info.value.__suppress_context__


def test_constraint_loss_cannot_succeed(requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock) -> None:
    invalid = dict(design_data, preserved_constraints=[])
    llm.invoke_structured.return_value = DesignOutput.model_validate(invalid)
    llm.invoke.return_value = AIMessage(content=json.dumps(invalid))
    with pytest.raises(DesignOutputError):
        design_system(requirements)
    assert llm.invoke.call_count == 3


@pytest.mark.parametrize("error_type", [LLMNetworkError, LLMAuthenticationError, LLMRateLimitError, LLMCallError])
def test_model_failure_propagates(error_type: type[Exception], requirements: RequirementsOutput, llm: MagicMock) -> None:
    failure = error_type("Model failed")
    llm.invoke_structured.side_effect = failure
    with pytest.raises(error_type) as info:
        design_system(requirements)
    assert info.value is failure
    llm.invoke.assert_not_called()


def test_fallback_model_failure_not_repaired(requirements: RequirementsOutput, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = LLMNetworkError("Network failed")
    with pytest.raises(LLMNetworkError):
        design_system(requirements)
    llm.invoke.assert_called_once()


def test_schema_fields_and_modules(design_data: dict[str, object]) -> None:
    for schema_type in (DesignOutput, ModuleDesign):
        schema = schema_type.model_json_schema()
        assert schema["additionalProperties"] is False
        assert all(field["description"] for field in schema["properties"].values())
        assert len(schema["required"]) == len(schema["properties"])
    assert DesignOutput.model_validate(design_data).modules[0].name == "tasks"


@pytest.mark.parametrize(("field", "value"), [("architecture_overview", " "), ("modules", []), ("technology_stack", []), ("project_structure", 42)])
def test_invalid_schema(field: str, value: object, design_data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DesignOutput.model_validate(dict(design_data, **{field: value}))


def test_requirements_to_design_dependency(requirements: RequirementsOutput, design_data: dict[str, object], llm: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    requirements_llm = MagicMock()
    requirements_llm.invoke_structured.return_value = requirements
    monkeypatch.setattr("workflow_studio.agents.requirements_agent.get_llm", lambda: requirements_llm)
    llm.invoke_structured.return_value = DesignOutput.model_validate(design_data)
    analyzed = analyze_requirements("开发本地 Python 待办应用，支持新增、完成和删除任务，不使用数据库。")
    result = design_system(analyzed)
    assert result.preserved_constraints == analyzed.constraints
    human = llm.invoke_structured.call_args.args[0].to_messages()[1].content
    payload = human.split("<requirements_output>\n")[1].split("\n</requirements_output>")[0]
    assert json.loads(payload) == analyzed.model_dump()
