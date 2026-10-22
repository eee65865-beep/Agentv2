"""Mock shared LLM; verify the real Review Agent and Coding revision prompt."""

import json
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from workflow_studio.agents.review_agent import ReviewInputError, ReviewOutputError, review_code
from workflow_studio.agents.coding_agent import CodingInputError, generate_code
from workflow_studio.llm.errors import LLMAuthenticationError, LLMNetworkError, LLMRateLimitError, LLMStructuredOutputUnsupported, LLMTruncatedOutputError
from workflow_studio.state import RequirementsOutput, DesignOutput, ModuleDesign, CodingOutput, CodeFile
from workflow_studio.state.review import ReviewOutput


@pytest.fixture
def inputs() -> tuple[RequirementsOutput, DesignOutput, CodingOutput]:
    req = RequirementsOutput(project_name="Addition", summary="Add integers", functional_requirements=["Add"], non_functional_requirements=[], constraints=["Standard library"], assumptions=[], acceptance_criteria=["2+3=5"])
    design = DesignOutput(architecture_overview="One module", technology_stack=["Python"], modules=[ModuleDesign(name="calculator", responsibility="Addition", dependencies=[], requirement_ids=["FR-1"])], data_models=[], interfaces=["add(a,b)"], project_structure="calculator.py", design_decisions=["Simple"], preserved_constraints=req.constraints)
    code = CodingOutput(files=[CodeFile(path="calculator.py", language="python", content="def add(a: int, b: int) -> int:\n    return a - b\n", description="Addition")], instructions="Manual use", dependencies=[], test_suggestions=["2+3=5"], limitations=["Not executed"])
    return req, design, code


@pytest.fixture
def rejected() -> ReviewOutput:
    return ReviewOutput(passed=False, score=40, issues=["add incorrectly subtracts"], suggestions=["Use addition"], summary="Static review only; tests were not run")


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    factory = MagicMock()
    monkeypatch.setattr("workflow_studio.agents.review_agent.get_llm", factory)
    monkeypatch.setattr("workflow_studio.agents.coding_agent.get_llm", factory)
    return factory.return_value


def test_review_reads_actual_three_inputs_and_independent_prompt(inputs: tuple, rejected: ReviewOutput, llm: MagicMock) -> None:
    before = [model.model_dump() for model in inputs]
    llm.invoke_structured.return_value = rejected
    assert review_code(*inputs) == rejected
    formatted, schema = llm.invoke_structured.call_args.args
    assert schema is ReviewOutput
    messages = formatted.to_messages()
    for tag, expected in zip(["requirements_output", "design_output", "coding_output"], before):
        payload = messages[1].content.split(f"<{tag}>\n")[1].split(f"\n</{tag}>")[0]
        assert json.loads(payload) == expected
    assert "不能证明代码完全正确" in messages[0].content
    assert "不执行代码" in messages[0].content
    assert [model.model_dump() for model in inputs] == before
    llm.invoke.assert_not_called()


def test_review_json_fallback_and_bounded_repair(inputs: tuple, rejected: ReviewOutput, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.side_effect = [AIMessage(content="not-json-private"), AIMessage(content=rejected.model_dump_json())]
    assert review_code(*inputs) == rejected
    assert llm.invoke.call_count == 2
    assert "not-json-private" not in str(llm.invoke.call_args)


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_invalid_json_fails_after_finite_repairs(budget: int, inputs: tuple, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.return_value = AIMessage(content="private-unparseable-response")
    with pytest.raises(ReviewOutputError) as info:
        review_code(*inputs, max_repair_attempts=budget)
    assert llm.invoke.call_count == budget + 1
    assert "private-unparseable" not in str(info.value)


@pytest.mark.parametrize("error", [LLMAuthenticationError, LLMNetworkError, LLMRateLimitError])
def test_provider_error_propagates_not_false_verdict(error: type[Exception], inputs: tuple, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = error("safe")
    with pytest.raises(error):
        review_code(*inputs)
    llm.invoke.assert_not_called()


def test_truncated_response_is_not_review_verdict(inputs: tuple, rejected: ReviewOutput, llm: MagicMock) -> None:
    llm.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
    llm.invoke.return_value = AIMessage(content=rejected.model_dump_json(), response_metadata={"finish_reason": "length"})
    with pytest.raises(LLMTruncatedOutputError):
        review_code(*inputs)


@pytest.mark.parametrize("which", [0, 1, 2])
def test_invalid_input_rejected_before_llm(which: int, inputs: tuple, llm: MagicMock) -> None:
    arguments = list(inputs)
    arguments[which] = None
    with pytest.raises(ReviewInputError):
        review_code(*arguments)
    llm.invoke_structured.assert_not_called()


@pytest.mark.parametrize("changes", [{"score": 101}, {"score": True}, {"passed": "false"}, {"issues": []}, {"summary": " "}])
def test_review_schema_validates_verdict(changes: dict, rejected: ReviewOutput) -> None:
    with pytest.raises(ValidationError):
        ReviewOutput.model_validate({**rejected.model_dump(), **changes})


def test_coding_revision_consumes_feedback_and_previous_files(inputs: tuple, rejected: ReviewOutput, llm: MagicMock) -> None:
    req, design, code = inputs
    fixed = code.model_copy(deep=True)
    fixed.files[0].content = "def add(a: int, b: int) -> int:\n    return a + b\n"
    llm.invoke_structured.return_value = fixed
    assert generate_code(req, design, review=rejected, previous_code=code) == fixed
    messages = llm.invoke_structured.call_args.args[0].to_messages()
    assert len(messages) == 3
    revision = messages[2].content
    assert json.loads(revision.split("<review_feedback>\n")[1].split("\n</review_feedback>")[0]) == rejected.model_dump()
    assert json.loads(revision.split("<previous_code>\n")[1].split("\n</previous_code>")[0]) == code.model_dump()
    assert "return a - b" in revision and "add incorrectly subtracts" in revision
    assert "保持 RequirementsOutput" in revision


@pytest.mark.parametrize("mode", ["missing_code", "missing_feedback", "passed"])
def test_coding_rejects_invalid_revision_context(mode: str, inputs: tuple, rejected: ReviewOutput, llm: MagicMock) -> None:
    req, design, code = inputs
    kwargs = {"review": rejected, "previous_code": code}
    if mode == "missing_code":
        kwargs["previous_code"] = None
    elif mode == "missing_feedback":
        kwargs["review"] = None
    else:
        kwargs["review"] = ReviewOutput(passed=True, score=90, issues=[], suggestions=[], summary="Static only")
    with pytest.raises(CodingInputError):
        generate_code(req, design, **kwargs)
    llm.invoke_structured.assert_not_called()
