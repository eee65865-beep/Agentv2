"""Requirements-only business interface suitable for a future graph node."""

import re

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import ValidationError

from workflow_studio.agents.prompts.requirements import build_requirements_prompt
from workflow_studio.llm import get_llm
from workflow_studio.budget import invoke_model
from workflow_studio.llm.errors import LLMStructuredOutputError, LLMStructuredOutputUnsupported, LLMTruncatedOutputError
from workflow_studio.state.requirements import (
    RequirementsOutput, RequirementsOutputV2, RequirementsSourceError, validate_requirement_sources, preserve_requirement_ids,
)

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
    return RequirementsOutputV2.model_validate_json(text)


def _validation_feedback(error: ValidationError | RequirementsSourceError) -> str:
    """Return schema locations/types without echoing private response values."""
    if isinstance(error, RequirementsSourceError):
        return str(error)
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['type']}"
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    )


def analyze_requirements(
    user_request: str, *, max_repair_attempts: int = DEFAULT_REPAIR_ATTEMPTS,
    previous_requirements: RequirementsOutput | None = None,
    source_texts: tuple[str, ...] | None = None,
) -> RequirementsOutput:
    """Analyze a request without code generation or graph-specific side effects.

    Prefer native LangChain structured output. On unsupported/invalid structured
    output, request JSON and perform at most max_repair_attempts repair calls.
    LLM authentication, transport, rate-limit and ordinary call failures propagate.
    For prompts containing generated questions, source_texts restricts provenance
    to the original request and the user's submitted answers.
    """
    if not isinstance(user_request, str) or not user_request.strip():
        raise RequirementsInputError("user_request 不能为空，且必须为字符串。")
    request = user_request.strip()
    if len(request) < MIN_REQUEST_LENGTH:
        raise RequirementsInputError(f"user_request 至少需要 {MIN_REQUEST_LENGTH} 个字符，请补充目标与主要功能。")
    if type(max_repair_attempts) is not int or not 0 <= max_repair_attempts <= 3:
        raise RequirementsInputError("max_repair_attempts 必须是 0 到 3 的整数。")
    if source_texts is not None and (not isinstance(source_texts, tuple) or not source_texts or any(not isinstance(item, str) or not item.strip() for item in source_texts)):
        raise RequirementsInputError("source_texts 必须为非空的已确认原文元组。")
    if previous_requirements is not None:
        if not isinstance(previous_requirements, RequirementsOutput):
            raise RequirementsInputError("previous_requirements 必须为有效需求对象。")
        try:
            previous_requirements = RequirementsOutput.model_validate(previous_requirements.model_dump())
        except ValidationError:
            raise RequirementsInputError("previous_requirements 字段无效。") from None

    def finish(output):
        validate_requirement_sources(output, source_texts if source_texts is not None else request)
        return preserve_requirement_ids(output, previous_requirements) if previous_requirements is not None else output

    llm = get_llm()
    parser = PydanticOutputParser(pydantic_object=RequirementsOutputV2)
    instructions = parser.get_format_instructions()
    prompt = build_requirements_prompt(instructions)
    formatted = prompt.invoke({"user_request": request})
    native_repair = False
    native = None
    try:
        native = invoke_model(llm, formatted, schema=RequirementsOutputV2)
        # Revalidate even injected clients/model_construct outputs at this boundary.
        native = RequirementsOutputV2.model_validate(native.model_dump())
        return finish(native)
    except (LLMStructuredOutputUnsupported, LLMStructuredOutputError):
        pass
    except (ValidationError, RequirementsSourceError) as error:
        native_repair = True
        formatted = build_requirements_prompt(instructions, repair=True).invoke({
            "user_request": request, "validation_feedback": _validation_feedback(error),
            "invalid_output": native.model_dump_json() if native is not None else "原生结构化响应未通过校验。",
        })

    repair_prompt = build_requirements_prompt(instructions, repair=True)
    for attempt in range(max_repair_attempts + 1):
        message = invoke_model(llm, formatted, repair=native_repair or attempt > 0)
        if message.response_metadata.get("finish_reason") in ("length", "max_tokens", "max_output_tokens") or message.response_metadata.get("status") == "incomplete":
            raise LLMTruncatedOutputError("Requirements output was truncated.")
        text = message.text
        try:
            return finish(_parse_json(text))
        except (ValidationError, RequirementsSourceError) as error:
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
