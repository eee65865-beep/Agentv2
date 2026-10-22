"""Explicit acceptance action; rerender/history/download never execute code."""

from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, Thread

import streamlit as st

from workflow_studio.state.review_policy import artifact_digest
from workflow_studio.validation.models import ValidationReport
from workflow_studio.validation.service import validate_artifact, validation_quality
from workflow_studio.validation.store import ValidationStore
from workflow_studio.validation.suites import SUITES

SESSION_KEY = "workflow_studio_validation"


def default_validation_store():
    return ValidationStore()


@dataclass
class ValidationPanel:
    identity: tuple[str, str]
    report: ValidationReport | None = None
    running: bool = False
    queue: Queue = field(default_factory=Queue)
    cancel: Event = field(default_factory=Event)
    thread: Thread | None = None
    error: str | None = None


def _worker(panel, result, suite_id):
    report = None
    error = None
    try:
        report = validate_artifact(result, suite_id, cancel=panel.cancel)
        default_validation_store().save(report)
    except Exception:
        error = "验证或证据保存失败；原始运行和产物保留。"
    finally:
        panel.queue.put((report, error))


def _start(panel, result):
    if panel.running:
        return
    panel.cancel.clear()
    panel.error = None
    panel.running = True
    panel.thread = Thread(target=_worker, args=(panel, result.model_copy(deep=True), st.session_state["acceptance_suite"]), daemon=True, name="workflow-validation")
    panel.thread.start()


def validation_markdown(report, quality):
    lines = ["# 固定验收报告", f"- 运行：{report.run_id}", f"- 报告：{report.report_id}",
             f"- 执行结果：{report.status} / {report.reason}", f"- 当前产物质量状态：{quality}",
             f"- 代码摘要：{report.artifact_digest}", f"- 上下文摘要：{report.context_digest}",
             f"- 验收集：{report.suite_id} / {report.suite_digest}", f"- 验证器：{report.validator_version}",
             f"- 环境：{report.environment}", f"- 可信命令：{' '.join(report.command)}",
             f"- 执行用例：{len(report.cases)} / {report.expected_cases}", f"- 耗时：{report.elapsed_seconds:.3f} 秒",
             "", "| 用例 | 状态 | 原因 | 退出码 |", "| --- | --- | --- | --- |"]
    lines += [f"| {case.case_id} | {case.status} | {case.reason} | {case.exit_code} |" for case in report.cases]
    lines += ["", "仅证明所选固定接口用例的结果，不代表完整自然语言需求已验收。模型静态审查不能覆盖或改写执行失败；不保存候选程序的原始输出。"]
    return "\n".join(lines)


def render_validation(page):
    if page.result is None or page.result.code is None:
        return
    identity = (page.result.run_id, artifact_digest(page.result.code))
    old = st.session_state.get(SESSION_KEY)
    if old is None or old.identity != identity:
        if old is not None and old.running:
            old.cancel.set()
        panel = ValidationPanel(identity)
        try:
            values = default_validation_store().list(page.result.run_id)
            panel.report = values[0] if values else None
        except (ValueError, OSError):
            panel.error = "已有验证记录读取失败。"
        st.session_state[SESSION_KEY] = panel
    panel = st.session_state[SESSION_KEY]

    @st.fragment(run_every=0.5 if panel.running else None)
    def content():
        was_running = panel.running
        try:
            while True:
                panel.report, panel.error = panel.queue.get_nowait()
                panel.running = False
        except Empty:
            pass
        if was_running and not panel.running:
            st.rerun()
        st.subheader("隔离验证与固定验收")
        st.caption("只验证所选固定 CLI 契约。不会安装依赖或在宿主运行代码；需管理员预装可信固定摘要镜像。未配置后端时为 not_run。")
        by_id = {suite.id: suite for suite in SUITES}
        identifier = st.selectbox("固定验收接口", list(by_id), key="acceptance_suite", format_func=lambda value: by_id[value].title, disabled=panel.running or page.running)
        st.write(by_id[identifier].contract)
        st.button("验证当前产物（隔离）", key="validate_artifact", on_click=_start, args=(panel, page.result), disabled=panel.running or page.running)
        if panel.running:
            st.info("验证进行中；每例与整批均有时限，取消会清理当前容器。")
            st.button("取消验证", key="cancel_validation", on_click=panel.cancel.set, disabled=panel.cancel.is_set())
        if panel.error:
            st.error(panel.error)
        if panel.report is not None:
            report = panel.report
            quality = validation_quality(page.result, report)
            st.write(f"执行：{report.status} · 原因：{report.reason} · 当前质量：{quality}")
            st.caption("通过仅限所选固定验收；原始 Workflow 状态与静态审查记录保持独立。")
            st.dataframe([item.model_dump() for item in report.cases], hide_index=True, width="stretch")
            st.download_button("下载验证 JSON", data=report.model_dump_json(indent=2), file_name="ValidationReport.json", mime="application/json", key="download_validation", on_click="ignore")
            st.download_button("下载验证说明", data=validation_markdown(report, quality), file_name="ValidationReport.md", mime="text/markdown", key="download_validation_md", on_click="ignore")
    content()
