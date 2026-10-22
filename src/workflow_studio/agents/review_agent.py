"""Consume all three outputs and return validated advisory review only."""

import re

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import ValidationError

from workflow_studio.agents.prompts.review import build_review_prompt
from workflow_studio.llm import get_llm
from workflow_studio.budget import invoke_model
from workflow_studio.llm.errors import LLMStructuredOutputError, LLMStructuredOutputUnsupported, LLMTruncatedOutputError
from workflow_studio.state.coding import CodingOutput, utf8_size
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.review import MAX_REVIEW_BYTES, ReviewAssessmentV2, ReviewOutput
from workflow_studio.state.review_policy import ReviewBindingError, bind_review
from workflow_studio.state.validation import DesignConsistencyError, validate_design_requirements


class ReviewInputError(ValueError):
    """Invalid upstream input; no model request is made."""


class ReviewOutputError(ValueError):
    """Review could not be parsed after bounded format repairs."""


def review_code(
    requirements: RequirementsOutput, design: DesignOutput, code: CodingOutput, *, max_repair_attempts: int = 2, validation_report=None,
) -> ReviewOutput:
    if not isinstance(requirements, RequirementsOutput) or not isinstance(design, DesignOutput) or not isinstance(code, CodingOutput):
        raise ReviewInputError("审查需要 RequirementsOutput、DesignOutput 和 CodingOutput。")
    try:
        req = RequirementsOutput.model_validate(requirements.model_dump())
        architecture = DesignOutput.model_validate(design.model_dump())
        artifacts = CodingOutput.model_validate(code.model_dump())
        validate_design_requirements(architecture, req)
    except (ValidationError, DesignConsistencyError):
        raise ReviewInputError("审查输入结构无效或需求与设计不一致。") from None
    if type(max_repair_attempts) is not int or not 0 <= max_repair_attempts <= 3:
        raise ReviewInputError("max_repair_attempts 必须为 0 到 3 的整数。")
    instructions = PydanticOutputParser(pydantic_object=ReviewAssessmentV2).get_format_instructions()
    values = {"requirements_json": req.model_dump_json(), "design_json": architecture.model_dump_json(), "coding_json": artifacts.model_dump_json()}
    if validation_report is not None:
        from workflow_studio.validation.service import validate_report_binding
        validate_report_binding(validation_report, req, architecture, artifacts)
        values["validation_json"] = validation_report.model_dump_json()
    prompt = build_review_prompt(instructions)
    repair_prompt = build_review_prompt(instructions, repair=True)
    formatted = prompt.invoke(values)
    llm = get_llm()
    native_repair = False
    try:
        native = invoke_model(llm, formatted, schema=ReviewAssessmentV2)
        return bind_review(native, req, architecture, artifacts)
    except (LLMStructuredOutputUnsupported, LLMStructuredOutputError):
        pass
    except (ValidationError, ReviewBindingError) as error:
        native_repair = True
        formatted = repair_prompt.invoke({**values, "validation_feedback": _feedback(error)})
    for attempt in range(max_repair_attempts + 1):
        message = invoke_model(llm, formatted, repair=native_repair or attempt > 0)
        if message.response_metadata.get("finish_reason") in ("length", "max_tokens", "max_output_tokens") or message.response_metadata.get("status") == "incomplete":
            raise LLMTruncatedOutputError("Review response was truncated.")
        text = message.text.strip()
        try:
            if utf8_size(text) > MAX_REVIEW_BYTES:
                raise ValueError("review_output_too_large")
            fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
            assessed = ReviewAssessmentV2.model_validate_json(fence.group(1) if fence else text)
            return bind_review(assessed, req, architecture, artifacts)
        except ValueError as error:
            if attempt == max_repair_attempts:
                raise ReviewOutputError("审查输出格式校验失败，有限修复次数已用尽。") from None
            formatted = repair_prompt.invoke({**values, "validation_feedback": _feedback(error)})
    raise AssertionError("Unreachable repair loop")


def _feedback(error: ValueError) -> str:
    if isinstance(error, ReviewBindingError):
        return str(error)
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['type']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
    return "review_output_exceeds_size_or_encoding_limits"
