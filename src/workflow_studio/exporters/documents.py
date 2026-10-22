"""Pure in-memory Markdown and ZIP exports; never execute generated files."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import ValidationError
from workflow_studio.errors import ExportLimitError, UnsafeCodePathError

from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.review_policy import evaluate_review
from workflow_studio.budget import STOP_LABELS
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
    functional = data.functional_requirements
    acceptance = data.acceptance_criteria
    if data.contract_version == 2:
        functional = [f"{item.id}：{item.description}" for item in data.requirement_details]
        acceptance = [f"{item.id} → {', '.join(item.requirement_ids)}：{item.description}" for item in data.acceptance_details]
        parts.append("需求契约 v2：已记录原文引用和验收关联；结构覆盖不代表功能已验证。\n")
    else:
        parts.append("旧版需求：未记录持久需求 ID、原文引用和验收关联；不推测历史映射。\n")
    for title, values in [
        ("功能需求", functional), ("非功能需求", data.non_functional_requirements),
        ("约束条件", data.constraints), ("假设与待确认", data.assumptions), ("验收标准", acceptance),
    ]:
        parts.append(_section(title, values))
    if data.contract_version == 2:
        parts.append(_section("需求原文来源", [f"{item.id}：{quote}" for item in data.requirement_details for quote in item.source_quotes]))
    if data.open_questions:
        parts.append(_section("待澄清问题", [f"{item.id} · {'阻断' if item.blocking else '非阻断'}：{item.question}" for item in data.open_questions]))
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
    status = {"completed": "已完成", "failed": "执行失败", "completed_with_issues": "已完成但审查未通过", "stopped": "已停止"}[data.status]
    parts = [f"# Workflow 执行报告\n\n- 运行 ID：{data.run_id}\n- 状态：{status}\n- 模型：{model_name}\n- 耗时：{elapsed:.2f} 秒\n", f"## 原始需求\n\n{request}\n"]
    trace = summarize_run(data.events)
    parts.append(f"- 运行模型：{', '.join(trace.model_names) or '不可用'}\n- 开始时间：{trace.started_at.isoformat() if trace.started_at else '不可用'}\n- 结束时间：{trace.ended_at.isoformat() if trace.ended_at else '不可用'}\n")
    if data.error:
        parts.append(f"## 错误摘要\n\n{data.error}\n")
    if data.clarifications:
        parts.append(_section("用户澄清记录", [f"{item.question_id}：{item.question}\n  用户回答：{item.answer}" for item in data.clarifications]))
    if data.budget is not None:
        usage, limits = data.budget, data.budget.limits
        parts.append(_section("全局运行预算", [
            f"模型逻辑调用：{usage.logical_calls} / {limits.max_logical_calls}",
            f"HTTP 实际尝试：{usage.http_attempts} / {limits.max_http_attempts}",
            f"格式修复：{usage.format_repairs} / {limits.max_format_repairs}",
            f"节点执行：{usage.node_executions} / {limits.max_node_executions}",
            f"耗时：{usage.elapsed_seconds:.3f} / {limits.max_elapsed_seconds} 秒",
            f"已报告 Token：{usage.known_tokens}；未知用量调用：{usage.unknown_usage_calls}；未知用量预留：{usage.unknown_tokens_reserved}（非实测）",
            f"Token 核算上限：{limits.max_total_tokens if limits.max_total_tokens is not None else '未启用'}；每调用预留：{limits.tokens_reserved_per_call}",
        ]))
        if usage.stop_reason is not None:
            parts.append("## 停止原因\n\n" + STOP_LABELS[usage.stop_reason] + "。保留已有有效结果，不代表验收通过。\n")
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
            decision = evaluate_review(item.review)
            parts.append(f"程序静态门禁：{'通过' if decision.passed else '未通过'}；处理：{decision.action}；策略：{decision.policy_version}；真实测试：未执行。\n")
            if item.review.contract_version == 2:
                parts.append(f"当轮代码摘要：{item.review.artifact_digest}\n\n上下文摘要：{item.review.context_digest}\n")
                for issue in item.review.issue_details:
                    location = issue.file or "位置未知/缺失文件"
                    if issue.line_start is not None:
                        location += f":{issue.line_start}–{issue.line_end}"
                    parts.append(f"#### {issue.issue_id} · {issue.severity} · {issue.category}\n\n{issue.message}\n\n位置：{location}\n\n需求：{', '.join(issue.requirement_ids) or '未关联'}；验收：{', '.join(issue.acceptance_ids) or '未关联'}\n\n期望：{issue.expected}\n\n静态观察：{issue.observed}\n")
                    if issue.evidence_quote is not None:
                        parts.append(_section("代码证据（静态）", [issue.evidence_quote]))
            else:
                parts.append("旧版审查未记录结构化问题、严重程度及产物摘要；沿用历史结论。\n")
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
        if event.repair_receipt is not None:
            repair = event.repair_receipt
            parts.append(f"  - 增量修复：基线 {repair.base_artifact_digest} → {repair.result_artifact_digest}；补丁 {repair.normalized_patch_bytes} 字节 / 完整产物 {repair.merged_artifact_bytes} 字节；保留文件 {repair.preserved_file_count}；格式失败 {repair.format_failures} 次（字节不是实测 Token）。")
            parts.extend(f"    - {item.operation} {item.path} ← {', '.join(item.issue_ids)}" for item in repair.changes)
    parts.append("\n本报告记录生成工作流及静态审查；隔离固定验收结果以单独的 ValidationReport 为准，不执行模型生成的测试作为可信验收。")
    return _checked_markdown("\n".join(parts))
