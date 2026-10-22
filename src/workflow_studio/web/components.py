"""Chinese presentation components; no model or Workflow invocation."""

import streamlit as st

from workflow_studio.errors import friendly_error
from workflow_studio.diagnostics import log_failure
from workflow_studio.exporters.documents import code_zip, design_markdown, requirements_markdown, workflow_report
from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.web.configuration import ConfigurationView
from workflow_studio.web.controller import AGENTS, PageState, agent_statuses
from workflow_studio.web.review import render_review
from workflow_studio.web.timeline import LABELS, render_run_metadata, render_timeline

DESCRIPTIONS = ["梳理目标与验收标准", "设计模块与技术方案", "生成多文件代码与测试"]


def render_header(config: ConfigurationView) -> None:
    st.caption("LANGGRAPH × DEEPSEEK · 多智能体开发工作台")
    st.title("Multi-Agent Workflow Studio")
    st.markdown("从一句开发需求出发，让三个 Agent 协作完成 **需求分析 → 系统设计 → 代码生成**。")
    first, second = st.columns([2, 1])
    first.caption(f"当前模型：{config.model_name}")
    if config.configured:
        second.success("API 配置：已配置", icon="✅")
    else:
        second.warning("API 配置：未配置", icon="⚠️")
    if config.error:
        st.error(config.error)
    elif not config.configured:
        st.info("请在项目根目录 .env 中配置 DEEPSEEK_API_KEY 后刷新页面。页面不会显示或保存密钥。")


def render_progress(page: PageState) -> None:
    st.subheader("Workflow 执行进度")
    statuses = agent_statuses(page)
    agents = (*AGENTS, "review_agent") if page.review_enabled else AGENTS
    descriptions = [*DESCRIPTIONS, "审查并给出修复反馈"] if page.review_enabled else DESCRIPTIONS
    total = len(agents)
    for column, agent, description in zip(st.columns(total), agents, descriptions):
        with column.container(border=True):
            st.caption(LABELS[agent])
            st.metric(description, statuses[agent])
    completed = sum(value == "已完成" for value in statuses.values())
    st.progress(completed / total, text=f"本轮已完成 {completed} / {total} 个阶段")
    current = next((LABELS[key] for key, value in statuses.items() if value == "运行中"), None)
    if current is None:
        current = next((LABELS[key] for key, value in statuses.items() if value == "执行失败"), "全部完成" if completed == total else "等待启动")
    first, second = st.columns([2, 1])
    first.caption(f"当前节点：{current}")
    second.caption(f"运行时间：{page.elapsed:.1f} 秒")
    if page.snapshot:
        st.caption(f"运行 ID：{page.snapshot['run_id']}")
    render_run_metadata(page.events)
    if page.error:
        st.error(page.error)
    elif page.result and page.result.status == "completed":
        st.success("模型静态审查通过，未执行真实测试。" if page.review_enabled else "三个 Agent 已完成协作，结果已保存到当前会话。")
    elif page.result and page.result.status == "completed_with_issues":
        st.warning("已达到修复次数上限，代码审查仍未通过。结果可查看和下载，但不代表交付或测试通过。")


def _list(title: str, values: list[str]) -> None:
    st.markdown(f"**{title}**")
    if values:
        for value in values:
            st.markdown(f"- {value}")
    else:
        st.caption("暂无")


def _requirements(data: RequirementsOutput) -> None:
    st.subheader(data.project_name)
    st.write(data.summary)
    for title, values in [("功能需求", data.functional_requirements), ("非功能需求", data.non_functional_requirements), ("约束条件", data.constraints), ("假设与待确认", data.assumptions), ("验收标准", data.acceptance_criteria)]:
        _list(title, values)


def _design(data: DesignOutput) -> None:
    st.write(data.architecture_overview)
    _list("技术栈", data.technology_stack)
    for module in data.modules:
        with st.expander(module.name, expanded=True):
            st.write(module.responsibility)
            st.caption("内部依赖：" + (", ".join(module.dependencies) or "无"))
            st.caption("需求对应：" + (", ".join(module.requirement_ids) or "基础模块"))
    st.markdown("**项目结构**")
    st.code(data.project_structure, language="text")
    for title, values in [("数据模型", data.data_models), ("接口", data.interfaces), ("设计决策", data.design_decisions), ("保留的需求约束", data.preserved_constraints)]:
        _list(title, values)


def _code(data: CodingOutput) -> None:
    st.caption(f"共 {len(data.files)} 个文件 · 已做结构、路径与 Python 语法校验，未执行生成代码。")
    languages = {"python": "python", "py": "python", "javascript": "javascript", "js": "javascript", "typescript": "typescript", "json": "json", "markdown": "markdown", "md": "markdown", "html": "html", "css": "css", "toml": "toml", "yaml": "yaml", "sql": "sql", "bash": "bash", "shell": "bash"}
    for file in data.files:
        with st.expander(file.path, expanded=True):
            st.caption(file.description)
            st.code(file.content, language=languages.get(file.language.casefold(), "text"))
    st.markdown("**运行说明**")
    st.write(data.instructions)
    _list("依赖", data.dependencies)
    _list("测试建议", data.test_suggestions)
    _list("限制与待确认", data.limitations)


def render_results(page: PageState) -> None:
    st.subheader("执行结果")
    labels = ["需求分析", "系统设计", "代码生成", *(["代码审查"] if page.review_enabled else []), "执行日志"]
    tabs = st.tabs(labels)
    req_tab, design_tab, code_tab = tabs[:3]
    log_tab = tabs[-1]
    snapshot = page.snapshot
    with req_tab:
        if snapshot and snapshot["requirements"] is not None:
            _requirements(RequirementsOutput.model_validate(snapshot["requirements"]))
        else:
            st.info("需求分析结果将在该阶段完成后显示。")
    with design_tab:
        if snapshot and snapshot["design"] is not None:
            _design(DesignOutput.model_validate(snapshot["design"]))
        else:
            st.info("系统设计结果将在该阶段完成后显示。")
    with code_tab:
        if snapshot and snapshot["code"] is not None:
            _code(CodingOutput.model_validate(snapshot["code"]))
        else:
            st.info("代码文件将在 Coding Agent 完成后显示。")
    if page.review_enabled:
        with tabs[3]:
            render_review(page)
    with log_tab:
        render_timeline(page.events)


def render_exports(page: PageState) -> None:
    st.subheader("导出结果")
    if page.result is None:
        st.caption("任务结束后可下载文档、代码包和执行报告。")
        return
    if not page.exports:
        try:
            result = page.result
            exports = {"report": workflow_report(result, page.request, page.model_name, page.elapsed).encode("utf-8")}
            if result.requirements:
                exports["requirements"] = requirements_markdown(result.requirements).encode("utf-8")
            if result.design:
                exports["design"] = design_markdown(result.design).encode("utf-8")
            if result.code:
                exports["code"] = code_zip(result.code)
            page.exports = exports
        except ValueError as error:
            log_failure("export_result", error, run_id=page.result.run_id)
            st.error("导出失败：" + friendly_error(error))
            return
    for column, label, key, filename, mime in zip(
        st.columns(4), ["下载需求文档", "下载设计文档", "下载代码 ZIP", "下载执行报告"],
        ["requirements", "design", "code", "report"], ["Requirements.md", "Design.md", "generated-code.zip", "WorkflowReport.md"],
        ["text/markdown", "text/markdown", "application/zip", "text/markdown"],
    ):
        column.download_button(label, data=page.exports.get(key, b""), file_name=filename, mime=mime, disabled=key not in page.exports, key=f"download_{key}", on_click="ignore", width="stretch")
