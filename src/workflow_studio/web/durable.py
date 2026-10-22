"""Saved task discovery and explicit clarification/resume controls."""

import streamlit as st

from workflow_studio.errors import friendly_error
from workflow_studio.storage.checkpoints import default_checkpoints
from workflow_studio.workflows.durable import list_durable
from workflow_studio.web.controller import open_durable_task, resume_task


def _open(page):
    if open_durable_task(page, st.session_state["durable_choice"]):
        st.session_state["request_input"] = page.request
        st.session_state["review_enabled"] = page.review_enabled
        st.session_state["max_revisions"] = page.max_revisions


def render_saved_tasks(page):
    st.subheader("已保存的任务进度")
    store = None
    try:
        store = default_checkpoints()
        values = list_durable(store)
    except Exception as error:
        st.caption(friendly_error(error))
        return
    finally:
        if store is not None:
            store.close()
    by_id = {item.thread_id: item for item in values}
    st.selectbox("选择持久任务", ["", *by_id], key="durable_choice", disabled=page.running,
                 format_func=lambda value: "选择已保存进度" if not value else f"{by_id[value].status} · {value}")
    st.button("打开任务进度", key="open_durable", disabled=page.running or not st.session_state["durable_choice"], on_click=_open, args=(page,))
    st.caption("打开仅读取；继续、重试或提交澄清才会推进任务。运行中或异常退出的任务恢复时会检查占用状态。")


def _answers(page, configured):
    values = {item["id"]: st.session_state.get(f"clarify_{page.checkpoint_id}_{item['id']}", "") for item in page.snapshot["awaiting_questions"]}
    if any(not value.strip() for value in values.values()):
        page.error = "请完整填写当前澄清问题后再继续。"
        return
    resume_task(page, configured, answers=values)


def render_durable_controls(page, configured):
    if not page.durable_thread_id:
        return
    st.caption("持久任务 ID：" + page.durable_thread_id)
    if page.durable_attempts:
        with st.expander("执行尝试记录"):
            st.dataframe(page.durable_attempts, hide_index=True, width="stretch")
    if page.waiting and page.snapshot:
        st.warning("进度已保存，等待你的澄清；等待期间不会调用模型。")
        with st.form(f"clarification_{page.checkpoint_id}"):
            for item in page.snapshot["awaiting_questions"]:
                st.text_area(item["question"], key=f"clarify_{page.checkpoint_id}_{item['id']}", max_chars=4000)
            st.form_submit_button("提交澄清并继续", disabled=page.running or not configured, on_click=_answers, args=(page, configured))
    elif not page.running:
        if page.result and page.result.status == "failed":
            st.button("重试失败节点", key="retry_durable", disabled=not configured, on_click=resume_task, args=(page, configured), kwargs={"mode": "retry"})
        elif page.result and page.result.status == "completed_with_issues" and page.result.review:
            from workflow_studio.state.review_policy import evaluate_review
            if evaluate_review(page.result.review).action == "manual_required":
                st.button("按审查反馈修订", key="replan_durable", disabled=not configured, on_click=resume_task, args=(page, configured), kwargs={"mode": "replan"})
                st.caption("修订通过关联的新尝试进行，原审查保留；需求问题会先请求澄清，设计问题返回设计阶段。")
        elif page.result is None:
            st.button("继续未完成任务", key="resume_durable", disabled=not configured, on_click=resume_task, args=(page, configured))
