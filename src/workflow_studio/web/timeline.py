"""Read-only execution timeline and per-Agent details from WorkflowEvent."""

from datetime import datetime, timedelta, timezone

import streamlit as st

from workflow_studio.state.tracing import agent_events, summarize_run
from workflow_studio.state.workflow import WorkflowEvent
from workflow_studio.web.controller import friendly_error

LABELS = {"requirements_agent": "Requirements Agent", "design_agent": "Design Agent", "coding_agent": "Coding Agent", "review_agent": "Code Review Agent"}
EVENT_LABELS = {"started": "开始执行", "completed": "执行完成", "failed": "执行失败", "info": "信息", "initialized": "初始化"}
STATUS_LABELS = {"pending": "等待中", "running": "运行中", "completed": "已完成", "failed": "执行失败", "completed_with_issues": "已完成但审查未通过"}
LOCAL = timezone(timedelta(hours=8))


def format_time(value: datetime | None) -> str:
    return value.astimezone(LOCAL).isoformat(timespec="milliseconds") if value else "—"


def tokens(value: int | None) -> str:
    return str(value) if value is not None else "不可用"


def render_run_metadata(events: list[WorkflowEvent]) -> None:
    trace = summarize_run(events)
    st.caption(f"Workflow 状态：{STATUS_LABELS[trace.status]} · 运行模型：{', '.join(trace.model_names) or '不可用'}")
    left, right = st.columns(2)
    left.caption("开始时间（UTC+8）：" + format_time(trace.started_at))
    right.caption("结束时间（UTC+8）：" + format_time(trace.ended_at))


def render_timeline(events: list[WorkflowEvent]) -> None:
    if not events:
        st.caption("启动任务后，将显示来自 LangGraph 的真实执行事件。")
        return
    st.markdown("**真实事件时间线**")
    st.dataframe([{
        "时间（UTC+8）": format_time(event.timestamp),
        "Agent": LABELS.get(event.agent_name, "Workflow"),
        "事件": EVENT_LABELS[event.event_type], "修复轮次": event.revision_count,
        "节点状态": {"started": "运行中", "completed": "已完成", "failed": "执行失败"}.get(event.event_type, "—"),
        "Workflow 状态": STATUS_LABELS[event.status],
        "耗时（秒）": f"{event.elapsed_seconds:.3f}" if event.elapsed_seconds is not None else "—",
        "模型": event.model_name or "不可用",
        "输入字段": ", ".join(event.input_fields) or "—",
        "写入字段": ", ".join(event.output_fields) or "—",
        "Token 总数": tokens(event.token_usage.total_tokens),
        "说明": event.message,
    } for event in events], width="stretch", hide_index=True)
    st.caption("节点完成不代表整个 Workflow 完成；审查模式由 Review 决定通过、修复或带问题结束。时间线按事件提交顺序显示。")
    selected = st.selectbox("查看单个 Agent 执行详情", list(LABELS), format_func=lambda name: LABELS[name], key="trace_agent_choice")
    selected_events = agent_events(events, selected)
    if not selected_events:
        st.info("该 Agent 尚未执行。")
        return
    attempts = list(dict.fromkeys(event.revision_count for event in selected_events))
    if len(attempts) > 1:
        attempt = st.selectbox("选择执行轮次", attempts, index=len(attempts) - 1, format_func=lambda value: "初次执行" if value == 0 else f"第 {value} 次修复", key=f"trace_attempt_{events[0].run_id}_{selected}")
        selected_events = [event for event in selected_events if event.revision_count == attempt]
    last = selected_events[-1]
    with st.container(border=True):
        st.markdown("**" + LABELS[selected] + "**")
        st.write("节点状态：" + {"started": "运行中", "completed": "已完成", "failed": "执行失败"}.get(last.event_type, "等待中"))
        st.caption("运行 ID：" + last.run_id)
        st.caption("模型：" + (last.model_name or "不可用"))
        st.write("开始时间：" + format_time(last.started_at))
        st.write("结束时间：" + format_time(last.ended_at))
        st.write("节点耗时：" + (f"{last.elapsed_seconds:.3f} 秒" if last.elapsed_seconds is not None else "执行中"))
        st.write("读取 State 字段：" + (", ".join(last.input_fields) or "—"))
        st.write("实际写入 State 字段：" + (", ".join(last.output_fields) or "尚未提交"))
        if last.error_type:
            st.warning("失败原因：" + friendly_error(last.error_type))
            st.caption("异常类别：" + last.error_type + "；原始异常正文未记录。")
        usage = last.token_usage
        st.write(f"Token 用量：输入 {tokens(usage.input_tokens)} / 输出 {tokens(usage.output_tokens)} / 总计 {tokens(usage.total_tokens)}")
        st.caption(f"可观察的模型调用次数：{len(last.llm_calls)}。包含 Agent 格式修复调用；不推测 SDK 内部重试次数。")
        if last.llm_calls:
            st.dataframe([{
                "调用": index, "模型": call.model_name, "状态": call.status,
                "耗时（秒）": round(call.elapsed_seconds, 3),
                "输入 Token": tokens(call.usage.input_tokens),
                "输出 Token": tokens(call.usage.output_tokens),
                "总 Token": tokens(call.usage.total_tokens),
            } for index, call in enumerate(last.llm_calls, 1)], hide_index=True, width="stretch")
        st.caption("不可用表示服务未返回该用量或没有可观察的调用；部分调用缺失用量时，不将已知小计当作完整总数。")
