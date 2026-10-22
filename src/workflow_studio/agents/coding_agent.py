"""Generate validated code artifacts without writing or executing them."""

import re

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import ValidationError

from workflow_studio.agents.prompts.coding import build_coding_prompt
from workflow_studio.llm import get_llm
from workflow_studio.budget import invoke_model
from workflow_studio.llm.errors import LLMStructuredOutputError, LLMStructuredOutputUnsupported, LLMTruncatedOutputError
from workflow_studio.state.coding import MAX_FILES, MAX_FILE_BYTES, MAX_OUTPUT_BYTES, CodingOutput, utf8_size
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.review import ReviewOutput
from workflow_studio.state.review_policy import ReviewBindingError, evaluate_review, validate_review_binding
from workflow_studio.state.validation import DesignConsistencyError, validate_design_requirements


class CodingInputError(ValueError):
    """Invalid or mutually inconsistent requirements and design."""


class CodingOutputError(ValueError):
    """Generation failed validation within the finite repair budget."""


def _parse_json(text: str) -> CodingOutput:
    if utf8_size(text) > MAX_OUTPUT_BYTES:
        raise ValueError("raw_coding_output_exceeds_total_byte_limit")
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    return CodingOutput.model_validate_json(fence.group(1) if fence else text)


def _feedback(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['type']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
    return "coding_output_exceeds_limits_or_contains_invalid_unicode"


def generate_code(
    requirements: RequirementsOutput, design: DesignOutput, *, max_repair_attempts: int = 2,
    review: ReviewOutput | None = None, previous_code: CodingOutput | None = None,
    validation_report=None, repair_policy=None,
) -> CodingOutput:
    """Consume both previous outputs, return files as data only.

    Schema/path/size/syntax failures may be repaired. Provider truncation,
    transport, authentication and ordinary call errors propagate immediately.
    """
    if not isinstance(requirements, RequirementsOutput) or not isinstance(design, DesignOutput):
        raise CodingInputError("必须提供 RequirementsOutput 和 DesignOutput 对象。")
    try:
        req = RequirementsOutput.model_validate(requirements.model_dump())
        architecture = DesignOutput.model_validate(design.model_dump())
        validate_design_requirements(architecture, req)
    except (ValidationError, DesignConsistencyError):
        raise CodingInputError("输入需求与设计字段无效，或约束、功能覆盖、模块依赖不一致。") from None
    if type(max_repair_attempts) is not int or not 0 <= max_repair_attempts <= 3:
        raise CodingInputError("max_repair_attempts 必须是 0 到 3 的整数。")

    revision = review is not None or previous_code is not None or validation_report is not None
    if repair_policy is not None and not revision:
        raise CodingInputError("文件修复策略仅适用于增量修复。")
    if revision:
        if not isinstance(previous_code, CodingOutput) or (review is None and validation_report is None) or (review is not None and not isinstance(review, ReviewOutput)):
            raise CodingInputError("修复时必须提供上一版代码及有效审查或验证反馈。")
        try:
            previous_code = CodingOutput.model_validate(previous_code.model_dump())
            if review is not None:
                review = ReviewOutput.model_validate(review.model_dump())
                validate_review_binding(review, req, architecture, previous_code)
            if validation_report is not None:
                from workflow_studio.validation.models import ValidationReport
                from workflow_studio.validation.service import validate_report_binding
                if not isinstance(validation_report, ValidationReport):
                    raise ValueError("invalid_validation_report_type")
                validation_report = ValidationReport.model_validate(validation_report.model_dump())
                validate_report_binding(validation_report, req, architecture, previous_code)
            from workflow_studio.state.patch import RepairPolicy
            policy = RepairPolicy.replacements_only(previous_code) if repair_policy is None else RepairPolicy.model_validate(repair_policy.model_dump())
        except (ValueError, AttributeError):
            raise CodingInputError("修复反馈或上一版代码校验失败。") from None
        decision = evaluate_review(review) if review is not None else None
        if decision is not None and decision.action == "manual_required":
            raise CodingInputError("审查门禁不允许代码修复；请先处理需求/设计问题或保留已通过结果。")
        if not (decision is not None and decision.action == "code_revision") and not (validation_report is not None and validation_report.status == "failed"):
            raise CodingInputError("没有允许代码修复的有效反馈。")
        from workflow_studio.agents.incremental import generate_incremental
        from workflow_studio.state.patch import PatchError
        try:
            return generate_incremental(req, architecture, previous_code, review, validation_report, policy, max_repair_attempts=max_repair_attempts, factory=get_llm)
        except PatchError:
            raise CodingInputError("文件修复策略与当前基线不匹配，未调用模型。") from None
    instructions = PydanticOutputParser(pydantic_object=CodingOutput).get_format_instructions()
    limits = f"最多 {MAX_FILES} 个文件；每个文件 UTF-8 内容最多 {MAX_FILE_BYTES} 字节；完整输出最多 {MAX_OUTPUT_BYTES} 字节。"
    prompt = build_coding_prompt(instructions, limits)
    repair_prompt = build_coding_prompt(instructions, limits, repair=True)
    values = {"requirements_json": req.model_dump_json(), "design_json": architecture.model_dump_json()}
    formatted = prompt.invoke(values)
    llm = get_llm()
    native_repair = False
    try:
        native = invoke_model(llm, formatted, schema=CodingOutput)
        # Revalidate nested instances even when a caller supplied an existing model.
        return CodingOutput.model_validate(native.model_dump())
    except (LLMStructuredOutputUnsupported, LLMStructuredOutputError):
        pass
    except ValidationError:
        # The shared client normally handles this; also guard mutated model instances.
        native_repair = True

    for attempt in range(max_repair_attempts + 1):
        message = invoke_model(llm, formatted, repair=native_repair or attempt > 0)
        # Defense in depth for alternate/mock clients bypassing shared checks.
        if message.response_metadata.get("finish_reason") in ("length", "max_tokens", "max_output_tokens") or message.response_metadata.get("status") == "incomplete":
            raise LLMTruncatedOutputError("Coding output was truncated; incomplete files were rejected.")
        text = message.text
        try:
            return _parse_json(text)
        except ValueError as error:
            if attempt == max_repair_attempts:
                raise CodingOutputError(f"代码输出校验失败：JSON 回退已用尽 {max_repair_attempts} 次修复机会。") from None
            # Do not echo an oversized response into a larger repair request.
            try:
                previous = text if utf8_size(text) <= MAX_OUTPUT_BYTES else "上一轮响应超出总长度限制，内容不再回传。"
            except ValueError:
                previous = "上一轮响应包含无效 Unicode，内容不再回传。"
            formatted = repair_prompt.invoke({**values, "validation_feedback": _feedback(error), "invalid_output": previous})
    raise AssertionError("Unreachable repair loop")
