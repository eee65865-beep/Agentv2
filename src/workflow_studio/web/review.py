"""Advisory review verdict and per-revision history; display only."""

import streamlit as st

from workflow_studio.state.review import ReviewOutput, ReviewStatus
from workflow_studio.state.review_policy import evaluate_review
from workflow_studio.web.controller import PageState
from workflow_studio.web.timeline import format_time

REVIEW_LABELS = {ReviewStatus.DISABLED: "未启用", ReviewStatus.PENDING: "待审查", ReviewStatus.NEEDS_REVISION: "未通过，等待修复", ReviewStatus.PASSED: "静态审查门禁通过", ReviewStatus.FAILED: "审查未通过，自动修复已停止", ReviewStatus.ERROR: "审查执行异常"}


def _feedback(review: ReviewOutput) -> None:
    decision = evaluate_review(review)
    st.write(f"模型判断：{'通过' if review.passed else '未通过'} · 参考评分：{review.score} / 100")
    st.write(f"程序静态门禁：{'通过' if decision.passed else '未通过'} · 真实测试：未执行")
    if decision.action == "manual_required":
        st.warning("发现需处理的需求或设计问题，已停止自动代码修复；请先核对需求/设计。")
    if review.contract_version == 2:
        st.caption(f"门禁策略：{decision.policy_version} · 代码摘要：{review.artifact_digest}")
    else:
        st.caption("旧版审查沿用历史模型结论，未记录结构化问题和代码摘要。")
    st.write(review.summary)
    st.markdown("**发现的问题**")
    if review.contract_version == 2:
        for item in review.issue_details:
            st.write(f"{item.issue_id} · {item.severity} · {item.category} · {item.message}")
            location = item.file or "位置未知/缺失文件"
            if item.line_start is not None:
                location += f":{item.line_start}–{item.line_end}"
            st.caption(location + " · 需求：" + (", ".join(item.requirement_ids) or "未关联") + " · 验收：" + (", ".join(item.acceptance_ids) or "未关联"))
            st.write("期望：" + item.expected)
            st.write("静态观察：" + item.observed)
            if item.evidence_quote is not None:
                st.code(item.evidence_quote, language="text")
    else:
        for item in review.issues:
            st.write("• " + item)
    if not review.issues:
        st.caption("模型未发现阻断问题；不代表代码完全正确。")
    st.markdown("**修复建议**")
    for item in review.suggestions:
        st.write("• " + item)


def render_review(page: PageState) -> None:
    st.info("这里展示 LLM 静态审查意见。系统没有执行生成代码或测试，模型判断不等于真实测试通过。")
    state = page.snapshot
    if state is None:
        st.caption("审查结果将在 Review Agent 完成后显示。")
        return
    st.write("审查状态：" + REVIEW_LABELS[state["review_status"]])
    st.caption(f"已尝试修复 {state['revision_count']} 次 / 上限 {state['max_revisions']} 次；首次代码生成不计为修复。")
    if state["review_status"] == ReviewStatus.ERROR:
        st.warning("审查调用异常，未产生新的有效审查结论，不会作为普通不通过触发修复。")
    if state["review"]:
        st.markdown("**最近一次有效审查**")
        _feedback(ReviewOutput.model_validate(state["review"]))
    st.markdown("**每次修复执行记录**")
    repairs = [event for event in page.events if event.agent_name == "coding_agent" and event.revision_count > 0]
    if repairs:
        st.dataframe([{"修复次数": event.revision_count, "时间": format_time(event.timestamp), "状态": {"started": "运行中", "completed": "已完成", "failed": "执行失败", "stopped": "已停止"}[event.event_type], "读取反馈": ", ".join(event.input_fields)} for event in repairs], hide_index=True, width="stretch")
    else:
        st.caption("尚未执行代码修复。")
    st.markdown("**历次审查与代码快照**")
    for item in state["review_history"]:
        label = "初次审查" if item.revision_count == 0 else f"第 {item.revision_count} 次修复后的审查"
        with st.expander(label + (" · 静态门禁通过" if evaluate_review(item.review).passed else " · 未通过")):
            st.caption(format_time(item.reviewed_at))
            _feedback(item.review)
            st.caption("当轮文件：" + ", ".join(file.path for file in item.code.files))
            if st.checkbox("查看此轮代码快照", key=f"review_code_{item.record_id}"):
                for file in item.code.files:
                    st.caption(file.path)
                    st.code(file.content, language="python" if file.language.casefold() in ("python", "py") else "text")
