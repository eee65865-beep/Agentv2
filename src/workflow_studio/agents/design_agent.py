"""Requirements-driven design interface, independent of future LangGraph nodes."""

import json
import re

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import ValidationError

from workflow_studio.agents.prompts.design import build_design_prompt
from workflow_studio.llm import get_llm
from workflow_studio.llm.errors import LLMStructuredOutputError, LLMStructuredOutputUnsupported
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.validation import (
    DesignConsistencyError as _DesignConsistencyError,
    validate_design_requirements as _validate_consistency,
)


class DesignInputError(ValueError):
    """The caller did not supply valid structured requirements or options."""


class DesignOutputError(ValueError):
    """No validated and traceable design was produced within the repair budget."""



def _parse_json(text: str) -> DesignOutput:
    """Accept complete JSON, optionally surrounded by one Markdown JSON fence."""
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    return DesignOutput.model_validate_json(fence.group(1) if fence else text)


def _feedback(error: ValidationError | _DesignConsistencyError) -> str:
    """Give repair instructions without exposing validation input values."""
    if isinstance(error, _DesignConsistencyError):
        return str(error)
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['type']}"
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    )


def design_system(
    requirements: RequirementsOutput, *, user_request: str | None = None,
    max_repair_attempts: int = 2,
) -> DesignOutput:
    """Design from validated requirements, with optional secondary raw context.

    No graph is built and the input is never mutated. Native structured output
    is preferred. Unsupported/invalid structured output falls back to JSON with
    at most max_repair_attempts repairs; transport/auth/call errors propagate.
    """
    if not isinstance(requirements, RequirementsOutput):
        raise DesignInputError("requirements 必须是有效的 RequirementsOutput 对象。")
    try:
        # Revalidate a snapshot: model instances may have been mutated or built
        # via model_construct, which bypasses the original Pydantic validation.
        validated = RequirementsOutput.model_validate(requirements.model_dump())
    except ValidationError:
        raise DesignInputError("RequirementsOutput 字段校验失败，请提供完整有效的结构化需求。") from None
    if user_request is not None and not isinstance(user_request, str):
        raise DesignInputError("user_request 必须为字符串或 None。")
    if type(max_repair_attempts) is not int or not 0 <= max_repair_attempts <= 3:
        raise DesignInputError("max_repair_attempts 必须是 0 到 3 的整数。")

    parser = PydanticOutputParser(pydantic_object=DesignOutput)
    instructions = parser.get_format_instructions()
    prompt = build_design_prompt(instructions)
    repair_prompt = build_design_prompt(instructions, repair=True)
    values = {
        "requirements_json": validated.model_dump_json(),
        "requirement_catalog": json.dumps([
            {"id": f"FR-{i}", "description": description}
            for i, description in enumerate(validated.functional_requirements, start=1)
        ], ensure_ascii=False),
        "user_request": user_request.strip() if user_request and user_request.strip() else "未提供原始需求；以结构化需求为准。",
    }
    formatted = prompt.invoke(values)
    llm = get_llm()
    try:
        native = llm.invoke_structured(formatted, DesignOutput)
        return _validate_consistency(native, validated)
    except (LLMStructuredOutputUnsupported, LLMStructuredOutputError):
        pass
    except _DesignConsistencyError as error:
        # A schema-valid native response can still omit a constraint or FR.
        formatted = repair_prompt.invoke({
            **values, "validation_feedback": _feedback(error),
            "invalid_output": native.model_dump_json(),
        })

    for attempt in range(max_repair_attempts + 1):
        text = llm.invoke(formatted).text
        try:
            return _validate_consistency(_parse_json(text), validated)
        except (ValidationError, _DesignConsistencyError) as error:
            if attempt == max_repair_attempts:
                raise DesignOutputError(
                    f"架构设计输出校验失败：JSON 回退已用尽 {max_repair_attempts} 次修复机会。"
                ) from None
            formatted = repair_prompt.invoke({
                **values, "validation_feedback": _feedback(error), "invalid_output": text,
            })
    raise AssertionError("Unreachable repair loop")
