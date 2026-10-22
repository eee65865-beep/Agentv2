"""Small Streamlit composition root; business logic stays in Workflow services."""

import streamlit as st

from workflow_studio.diagnostics import configure_logging
from workflow_studio.web.history import render_history, render_save_status
from workflow_studio.web.components import render_exports, render_header, render_progress, render_results
from workflow_studio.web.configuration import get_configuration
from workflow_studio.web.controller import PageState, collect_updates, start_task
from workflow_studio.web.examples import EXAMPLES
from workflow_studio.state.review import MAX_REVISIONS

SESSION_KEY = "workflow_studio_page"

STYLE = """<style>
.block-container {max-width: 1240px; padding-top: 2.2rem; padding-bottom: 3rem;}
h1 {letter-spacing: -0.045em; font-weight: 800 !important;}
h2, h3 {letter-spacing: -0.02em;}
[data-testid="stSidebar"] {border-right: 1px solid #dfe7ef;}
[data-testid="stMetricValue"] {font-size: 1.45rem;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 16px;}
[data-testid="stTabs"] [role="tablist"] {gap: 1.2rem;}
[data-testid="stCode"] {border-radius: 12px;}
</style>"""


def _apply_example() -> None:
    choice = st.session_state["example_choice"]
    st.session_state["request_input"] = EXAMPLES[choice]


def _submit() -> None:
    page = st.session_state[SESSION_KEY]
    config = get_configuration()
    start_task(page, st.session_state["request_input"], config.configured, config.model_name, review_enabled=st.session_state["review_enabled"], max_revisions=st.session_state["max_revisions"], thinking_enabled=st.session_state["thinking_enabled"])


def main() -> None:
    configure_logging()
    st.set_page_config(page_title="Multi-Agent Workflow Studio", page_icon="🧩", layout="wide")
    st.markdown(STYLE, unsafe_allow_html=True)  # Static CSS only; no user/model HTML.
    if SESSION_KEY not in st.session_state:
        st.session_state[SESSION_KEY] = PageState()
    if "request_input" not in st.session_state:
        st.session_state["request_input"] = ""
    page: PageState = st.session_state[SESSION_KEY]
    config = get_configuration()
    if "thinking_enabled" not in st.session_state:
        st.session_state["thinking_enabled"] = config.thinking_enabled
    render_header(config)
    with st.sidebar:
        st.header("任务配置")
        st.caption("选择示例，或描述你想开发的软件。")
        st.selectbox("示例需求", list(EXAMPLES), key="example_choice", on_change=_apply_example, disabled=page.running)
        st.text_area("开发需求", key="request_input", height=260, placeholder="描述目标、主要功能、技术限制和验收标准……", disabled=page.running)
        st.toggle("开启思考模式", key="thinking_enabled", disabled=page.running, help="开启以进行更深入的分析，可能增加等待时间；关闭优先降低延迟。仅影响下一次启动的任务，不修改 .env。")
        st.markdown("**Workflow 模式**")
        enabled = st.checkbox("开启代码审查", key="review_enabled", disabled=page.running)
        st.number_input("最大修复次数", min_value=0, max_value=MAX_REVISIONS, value=1, step=1, key="max_revisions", disabled=page.running or not enabled, help="0 表示仅审查；1 表示首次审查后最多修复一次。格式修复与此计数独立。")
        st.caption("审查模式 · 有限修复循环" if enabled else "标准模式 · 顺序执行")
        st.markdown("需求分析 → 系统设计 → 代码生成 → 审查 ↔ 修复" if enabled else "需求分析 → 系统设计 → 代码生成")
        if enabled:
            st.caption("LLM 静态审查不能证明代码正确；不会执行生成代码或测试。")
        st.button("启动 Workflow" if not page.running else "Workflow 正在运行", key="start_workflow", type="primary", width="stretch", disabled=page.running or not config.configured, on_click=_submit)
        st.caption("仅点击启动会调用 API。重绘页面、查看结果与下载不会启动新任务。")
        st.divider()
        st.caption("生成代码仅供查看与下载，不自动运行或安装依赖。")
        st.divider()
        render_history(page)

    @st.fragment(run_every=0.5 if page.running else None)
    def workspace() -> None:
        finished = collect_updates(page)
        if finished:
            # Re-enable sidebar controls and stop timed polling after completion.
            st.rerun()
        render_progress(page)
        if page.thinking_enabled is not None and not page.from_history:
            st.caption("本次任务思考模式：" + ("已开启" if page.thinking_enabled else "已关闭"))
        render_save_status(page)
        st.divider()
        render_results(page)
        st.divider()
        render_exports(page)

    workspace()


if __name__ == "__main__":
    main()
