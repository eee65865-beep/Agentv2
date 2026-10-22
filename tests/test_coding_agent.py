"""Coding Agent uses fixed prior outputs and a mocked shared LLM factory."""

import json
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from workflow_studio.agents.coding_agent import CodingInputError, CodingOutputError, generate_code
from workflow_studio.llm.errors import (
    LLMAuthenticationError, LLMCallError, LLMNetworkError, LLMRateLimitError,
    LLMStructuredOutputError, LLMStructuredOutputUnsupported, LLMTruncatedOutputError,
)
from workflow_studio.state.coding import MAX_OUTPUT_BYTES, CodingOutput
from workflow_studio.state.design import DesignOutput, ModuleDesign
from workflow_studio.state.requirements import RequirementsOutput


@pytest.fixture
def requirements() -> RequirementsOutput:
    return RequirementsOutput(
        project_name="加法工具", summary="本地计算两个整数之和",
        functional_requirements=["计算两个整数之和"], non_functional_requirements=[],
        constraints=["使用 Python 标准库，不引入数据库"], assumptions=[],
        acceptance_criteria=["输入 2 与 3 返回 5"],
    )


@pytest.fixture
def design(requirements: RequirementsOutput) -> DesignOutput:
    return DesignOutput(
        architecture_overview="单模块本地函数及 unittest 测试", technology_stack=["Python", "unittest"],
        modules=[ModuleDesign(name="calculator", responsibility="计算两个整数之和", dependencies=[], requirement_ids=["FR-1"])],
        data_models=[], interfaces=["接收两个整数，返回整数之和"],
        project_structure="calculator.py\ntests/test_calculator.py", design_decisions=["使用标准库，保持简单"],
        preserved_constraints=list(requirements.constraints),
    )


@pytest.fixture
def output_data() -> dict[str, object]:
    return {
        "files": [
            {"path": "calculator.py", "language": "python", "content": "def add(a: int, b: int) -> int:\n    return a + b\n", "description": "calculator 模块，实现 FR-1"},
            {"path": "tests/test_calculator.py", "language": "python", "content": "import unittest\nfrom calculator import add\n\nclass CalculatorTest(unittest.TestCase):\n    def test_add(self) -> None:\n        self.assertEqual(add(2, 3), 5)\n", "description": "验收用例：2 与 3 返回 5"},
        ],
        "instructions": "在生成项目根目录手动执行 python -m unittest discover -s tests；此处未运行。",
        "dependencies": [], "test_suggestions": ["补充负数和零的测试"], "limitations": ["仅做静态校验，未执行代码"],
    }


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    factory = MagicMock()
    monkeypatch.setattr("workflow_studio.agents.coding_agent.get_llm", factory)
    client = factory.return_value
    client.factory = factory
    return client


def test_consumes_both_outputs(requirements: RequirementsOutput, design: DesignOutput, output_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.return_value = CodingOutput.model_validate(output_data)
    before_req, before_design = requirements.model_dump(), design.model_dump()
    result = generate_code(requirements, design)
    assert len(result.files) == 2
    assert any(file.path.startswith("tests/") for file in result.files)
    assert result.instructions
    llm.invoke.assert_not_called()
    llm.factory.assert_called_once_with()
    formatted, schema = llm.invoke_structured.call_args.args
    assert schema is CodingOutput
    messages = formatted.to_messages()
    assert messages[0].type == "system"
    human = messages[1].content
    for tag, expected in [("requirements_output", before_req), ("design_output", before_design)]:
        payload = human.split(f"<{tag}>\n")[1].split(f"\n</{tag}>")[0]
        assert json.loads(payload) == expected
    assert "禁止执行生成代码" in messages[0].content
    assert "20" in messages[0].content
    assert "65536" in messages[0].content
    assert requirements.model_dump() == before_req
    assert design.model_dump() == before_design


@pytest.mark.parametrize("which", ["requirements", "design", "mismatch", "mutated"])
def test_invalid_inputs_before_model(which: str, requirements: RequirementsOutput, design: DesignOutput, llm: MagicMock) -> None:
    req_input, design_input = requirements, design
    if which == "requirements":
        req_input = "raw text"
    elif which == "design":
        design_input = {}
    elif which == "mismatch":
        design.preserved_constraints.clear()
    else:
        requirements.functional_requirements.clear()
    with pytest.raises(CodingInputError):
        generate_code(req_input, design_input)  # type: ignore[arg-type]
    llm.factory.assert_not_called()


@pytest.mark.parametrize("budget", [-1, 4, True, 1.5])
def test_invalid_budget(budget: object, requirements: RequirementsOutput, design: DesignOutput, llm: MagicMock) -> None:
    with pytest.raises(CodingInputError):
        generate_code(requirements, design, max_repair_attempts=budget)  # type: ignore[arg-type]
    llm.factory.assert_not_called()


@pytest.mark.parametrize("reason", [LLMStructuredOutputUnsupported("unsupported"), LLMStructuredOutputError("invalid")])
def test_json_fallback(reason: Exception, requirements: RequirementsOutput, design: DesignOutput, output_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = reason
    llm.invoke.return_value = AIMessage(content=json.dumps(output_data))
    assert len(generate_code(requirements, design).files) == 2
    llm.invoke.assert_called_once()


@pytest.mark.parametrize("problem", ["json", "path", "empty", "duplicate", "syntax", "oversized"])
def test_invalid_output_repaired(problem: str, requirements: RequirementsOutput, design: DesignOutput, output_data: dict[str, object], llm: MagicMock) -> None:
    invalid = json.loads(json.dumps(output_data))
    if problem == "json":
        text = '{"files": ['
    elif problem == "oversized":
        text = "x" * (MAX_OUTPUT_BYTES + 1)
    else:
        if problem == "path":
            invalid["files"][0]["path"] = "../escape.py"
        elif problem == "empty":
            invalid["files"][0]["content"] = " "
        elif problem == "duplicate":
            invalid["files"][1]["path"] = "CALCULATOR.py"
        else:
            invalid["files"][0]["content"] = "def truncated("
        text = json.dumps(invalid)
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = [AIMessage(content=text), AIMessage(content=json.dumps(output_data))]
    result = generate_code(requirements, design)
    assert result.files[0].path == "calculator.py"
    assert llm.invoke.call_count == 2
    if problem == "oversized":
        assert len(llm.invoke.call_args.args[0].to_messages()[2].content) < 1000


@pytest.mark.parametrize("budget", [0, 2, 3])
def test_finite_repairs(budget: int, requirements: RequirementsOutput, design: DesignOutput, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.return_value = AIMessage(content="private-invalid-code")
    with pytest.raises(CodingOutputError) as info:
        generate_code(requirements, design, max_repair_attempts=budget)
    assert llm.invoke.call_count == 1 + budget
    assert "private-invalid-code" not in str(info.value)


@pytest.mark.parametrize("error_type", [LLMAuthenticationError, LLMRateLimitError, LLMNetworkError, LLMCallError, LLMTruncatedOutputError])
def test_model_failure_not_repaired(error_type: type[Exception], requirements: RequirementsOutput, design: DesignOutput, llm: MagicMock) -> None:
    error = error_type("Call failed")
    llm.invoke_structured.side_effect = error
    with pytest.raises(error_type) as info:
        generate_code(requirements, design)
    assert info.value is error
    llm.invoke.assert_not_called()


def test_parseable_but_truncated_json_rejected(requirements: RequirementsOutput, design: DesignOutput, output_data: dict[str, object], llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.return_value = AIMessage(content=json.dumps(output_data), response_metadata={"finish_reason": "length"})
    with pytest.raises(LLMTruncatedOutputError):
        generate_code(requirements, design)
    llm.invoke.assert_called_once()


def test_json_model_error_propagates(requirements: RequirementsOutput, design: DesignOutput, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = LLMNetworkError("Network failed")
    with pytest.raises(LLMNetworkError):
        generate_code(requirements, design)
    llm.invoke.assert_called_once()
