"""Chinese local history browser and explicit storage-only retry controls."""

import streamlit as st

from workflow_studio.errors import StorageError, friendly_error
from workflow_studio.storage import default_store
from workflow_studio.web.controller import PageState
from workflow_studio.web.persistence import open_history, save_page
from workflow_studio.web.timeline import STATUS_LABELS, format_time


def _open(page: PageState, key: str) -> None:
    if open_history(page, st.session_state[key]):
        st.session_state["request_input"] = page.request
        st.session_state["review_enabled"] = page.review_enabled
        st.session_state["max_revisions"] = page.max_revisions


def render_history(page: PageState) -> None:
    st.subheader("历史运行")
    try:
        listing = default_store().list_runs()
    except (StorageError, OSError) as error:
        st.warning(friendly_error(error))
        return
    by_id = {item.run_id: item for item in listing.entries}
    options = ["", *by_id]
    def label(run_id: str) -> str:
        if not run_id:
            return "选择历史运行"
        item = by_id[run_id]
        return f"{format_time(item.started_at or item.saved_at)} · {STATUS_LABELS[item.status]} · {run_id}"
    st.selectbox("历史运行列表", options, key="history_choice", format_func=label, disabled=page.running)
    st.button("打开所选记录", key="open_history_selected", disabled=page.running or not st.session_state["history_choice"], on_click=_open, args=(page, "history_choice"))
    st.text_input("按 run_id 打开", key="history_run_id", placeholder="完整 UUID", disabled=page.running)
    st.button("打开指定 run_id", key="open_history_id", disabled=page.running or not st.session_state.get("history_run_id"), on_click=_open, args=(page, "history_run_id"))
    st.button("刷新历史列表", key="refresh_history", disabled=page.running)
    if not listing.entries:
        st.caption("暂无已保存的历史运行。")
    if listing.problems:
        st.warning(f"已跳过 {len(listing.problems)} 条损坏或不可读取的记录，其他历史仍可打开。")
        with st.expander("查看异常记录标识"):
            for problem in listing.problems:
                st.caption(problem.run_id + " · " + problem.category)
    if page.history_error:
        st.error(page.history_error)


def render_save_status(page: PageState) -> None:
    if page.result is None:
        return
    if page.persistence_error:
        st.warning(page.persistence_error)
        st.button("仅重试保存（不调用 API）", key="retry_save", on_click=save_page, args=(page,), disabled=page.running)
    elif page.persisted_run_id:
        st.caption(("已打开历史：" if page.from_history else "结果已保存：") + f"outputs/{page.persisted_run_id}/")
