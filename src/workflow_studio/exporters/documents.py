"""Pure in-memory Markdown and ZIP exports; never execute generated files."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import ValidationError
from workflow_studio.errors import ExportLimitError, UnsafeCodePathError

from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.tracing import summarize_run
from workflow_studio.exporters.paths import validate_export_path


MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 512 * 1024


def _checked_markdown(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise ExportLimitError("markdown_size_limit")
    return text


def _section(title: str, values: list[str]) -> str:
    body = "\n".join(f"- {value}" for value in values) if values else "暂无。"
    return f"## {title}\n\n{body}\n"


def requirements_markdown(output: RequirementsOutput) -> str:
    data = RequirementsOutput.model_validate(output.model_dump())
    parts = [f"# {data.project_name}：需求分析\n", data.summary + "\n"]
    for title, values in [
        ("功能需求", data.functional_requirements), ("非功能需求", data.non_functional_requirements),
        ("约束条件", data.constraints), ("假设与待确认", data.assumptions), ("验收标准", data.acceptance_criteria),
    ]:
        parts.append(_section(title, values))
    return _checked_markdown("\n".join(parts))


def design_markdown(output: DesignOutput) -> str:
    data = DesignOutput.model_validate(output.model_dump())
    parts = ["# 系统设计\n", data.architecture_overview + "\n", _section("技术栈", data.technology_stack), "## 模块设计\n"]
    for module in data.modules:
        parts.append(f"### {module.name}\n\n{module.responsibility}\n\n依赖：{', '.join(module.dependencies) or '无'}\n\n需求对应：{', '.join(module.requirement_ids) or '基础模块'}\n")
    parts.extend([_section("数据模型", data.data_models), _section("接口", data.interfaces), f"## 项目结构\n\n```text\n{data.project_structure}\n```\n", _section("设计决策", data.design_decisions), _section("保留的需求约束", data.preserved_constraints)])
    return _checked_markdown("\n".join(parts))


def code_zip(output: CodingOutput) -> bytes:
    # Revalidate at the export boundary, including paths, duplicates and size.
    try:
        data = CodingOutput.model_validate(output.model_dump())
    except ValidationError as error:
        details = error.errors(include_input=False, include_context=False, include_url=False)
        if any("path" in item["loc"] for item in details):
            raise UnsafeCodePathError("unsafe_generated_file_path") from None
        if any(item["type"] in ("too_long", "string_too_long") for item in details):
            raise ExportLimitError("code_file_count_or_size_limit") from None
        raise
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for file in data.files:
            archive.writestr(validate_export_path(file.path), file.content.encode("utf-8"))
    result = buffer.getvalue()
    if len(result) > MAX_ZIP_BYTES:
        raise ExportLimitError("zip_size_limit")
    return result


def workflow_report(result: WorkflowResult, request: str, model_name: str, elapsed: float) -> str:
    data = WorkflowResult.model_validate(result.model_dump())
    status = {"completed": "已完成", "failed": "执行失败", "completed_with_issues": "已完成但审查未通过"}[data.status]
    parts = [f"# Workflow 执行报告\n\n- 运行 ID：{data.run_id}\n- 状态：{status}\n- 模型：{model_name}\n- 耗时：{elapsed:.2f} 秒\n", f"## 原始需求\n\n{request}\n"]
    trace = summarize_run(data.events)
    parts.append(f"- 运行模型：{', '.join(trace.model_names) or '不可用'}\n- 开始时间：{trace.started_at.isoformat() if trace.started_at else '不可用'}\n- 结束时间：{trace.ended_at.isoformat() if trace.ended_at else '不可用'}\n")
    if data.error:
        parts.append(f"## 错误摘要\n\n{data.error}\n")
    if data.requirements:
        parts.append(requirements_markdown(data.requirements))
    if data.design:
        parts.append(design_markdown(data.design))
    if data.code:
        parts.append(_section("生成文件", [f"{file.path}：{file.description}" for file in data.code.files]))
        parts.append("## 运行说明\n\n" + data.code.instructions)
        parts.append(_section("依赖", data.code.dependencies))
        parts.append(_section("测试建议", data.code.test_suggestions))
        parts.append(_section("限制", data.code.limitations))
    if data.review_enabled:
        parts.append(f"## 代码审查\n\n- 状态：{data.review_status}\n- 已尝试修复：{data.revision_count} / {data.max_revisions}\n\nLLM 静态审查不能证明代码正确；未执行生成代码或测试。\n")
        for item in data.review_history:
            parts.append(f"### 审查轮次 {item.revision_count}\n\n模型判断：{'通过' if item.review.passed else '未通过'}；参考分：{item.review.score}\n\n{item.review.summary}\n")
            parts.append(_section("问题", item.review.issues))
            parts.append(_section("建议", item.review.suggestions))
            parts.append(_section("当轮文件", [file.path for file in item.code.files]))
    parts.append("## 执行事件\n")
    for event in data.events:
        elapsed_text = f"{event.elapsed_seconds:.3f}s" if event.elapsed_seconds is not None else "不可用"
        usage = event.token_usage
        usage_text = "/".join(str(value) if value is not None else "不可用" for value in (usage.input_tokens, usage.output_tokens, usage.total_tokens))
        parts.append(f"- {event.timestamp.isoformat()} | {event.agent_name} | revision={event.revision_count} | {event.event_type} | Workflow={event.status} | {event.message} | model={event.model_name or '不可用'} | elapsed={elapsed_text} | read={','.join(event.input_fields) or '无'} | write={','.join(event.output_fields) or '无'} | tokens(in/out/total)={usage_text}")
        for index, call in enumerate(event.llm_calls, 1):
            parts.append(f"  - LLM {index}: model={call.model_name}, status={call.status}, elapsed={call.elapsed_seconds:.3f}s, usage={call.usage.model_dump_json()}")
    parts.append("\n生成代码及其测试未在本系统中执行。")
    return _checked_markdown("\n".join(parts))
