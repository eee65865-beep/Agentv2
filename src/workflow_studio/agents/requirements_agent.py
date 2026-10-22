"""Requirements-only business interface suitable for a future graph node."""

import re

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import ValidationError

from workflow_studio.agents.prompts.requirements import build_requirements_prompt
from workflow_studio.llm import get_llm
from workflow_studio.llm.errors import LLMStructuredOutputError, LLMStructuredOutputUnsupported
from workflow_studio.state.requirements import RequirementsOutput

MIN_REQUEST_LENGTH = 10
DEFAULT_REPAIR_ATTEMPTS = 2


class RequirementsInputError(ValueError):
    """The original request is missing or too short to analyze."""


class RequirementsOutputError(ValueError):
    """The model did not produce valid requirements within the repair budget."""


def _parse_json(text: str) -> RequirementsOutput:
    """Validate complete JSON only; allow a single enclosing JSON code fence.

    Unlike lenient partial-JSON parsers, truncated responses are not accepted.
    No eval or executable parsing is used.
    """
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    return RequirementsOutput.model_validate_json(text)


def _validation_feedback(error: ValidationError) -> str:
    """Return schema locations/types without echoing private response values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['type']}"
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    )


def analyze_requirements(
    user_request: str, *, max_repair_attempts: int = DEFAULT_REPAIR_ATTEMPTS,
) -> RequirementsOutput:
    """Analyze a request without code generation or graph-specific side effects.

    Prefer native LangChain structured output. On unsupported/invalid structured
    output, request JSON and perform at most max_repair_attempts repair calls.
    LLM authentication, transport, rate-limit and ordinary call failures propagate.
    """
    if not isinstance(user_request, str) or not user_request.strip():
        raise RequirementsInputError("user_request 不能为空，且必须为字符串。")
    request = user_request.strip()
    if len(request) < MIN_REQUEST_LENGTH:
        raise RequirementsInputError(f"user_request 至少需要 {MIN_REQUEST_LENGTH} 个字符，请补充目标与主要功能。")
    if type(max_repair_attempts) is not int or not 0 <= max_repair_attempts <= 3:
        raise RequirementsInputError("max_repair_attempts 必须是 0 到 3 的整数。")

    llm = get_llm()
    parser = PydanticOutputParser(pydantic_object=RequirementsOutput)
    instructions = parser.get_format_instructions()
    prompt = build_requirements_prompt(instructions)
    formatted = prompt.invoke({"user_request": request})
    try:
        return llm.invoke_structured(formatted, RequirementsOutput)
    except (LLMStructuredOutputUnsupported, LLMStructuredOutputError):
        pass

    repair_prompt = build_requirements_prompt(instructions, repair=True)
    for attempt in range(max_repair_attempts + 1):
        text = llm.invoke(formatted).text
        try:
            return _parse_json(text)
        except ValidationError as error:
            if attempt == max_repair_attempts:
                raise RequirementsOutputError(
                    f"需求分析输出校验失败：JSON 回退已用尽 {max_repair_attempts} 次修复机会。"
                ) from None
            formatted = repair_prompt.invoke({
                "user_request": request,
                "validation_feedback": _validation_feedback(error),
                "invalid_output": text,
            })
    raise AssertionError("Unreachable repair loop")
