"""Small Streamlit composition root; business logic stays in Workflow services."""

import streamlit as st

from workflow_studio.diagnostics import configure_logging
from workflow_studio.web.history import render_history, render_save_status
from workflow_studio.web.components import render_exports, render_header, render_progress, render_results
from workflow_studio.web.configuration import get_configuration
from workflow_studio.web.controller import PageState, collect_updates, request_stop, start_task
from workflow_studio.budget import RunBudget
from workflow_studio.web.examples import EXAMPLES
from workflow_studio.state.review import MAX_REVISIONS
from workflow_studio.web.durable import render_durable_controls, render_saved_tasks
from workflow_studio.web.validation import render_validation

SESSION_KEY = "workflow_studio_page"

STYLE = """<style>
.block-container {max-width: 980px; padding-top: 2rem; padding-bottom: 270px;}
h1 {font-size: 1.3rem !important; letter-spacing: -.035em; font-weight: 650 !important;}
h2,h3 {letter-spacing: -.025em;}
[data-testid="stSidebar"] {min-width: 260px !important; max-width: 260px !important; border-right: 1px solid #80808018;}
[data-testid="stSidebar"] h3 {font-size: 1rem;}
[data-testid="stMetricValue"] {font-size: 1.25rem;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 16px;}
[data-testid="stTabs"] [role="tablist"] {gap: 1.2rem;}
[data-testid="stCode"] {border-radius: 12px;}
.welcome {text-align:center; margin:clamp(3rem,18vh,11rem) auto 2rem;}
.welcome .eyebrow {font-size:12px; letter-spacing:.17em; color:#888; margin-bottom:20px;}
.welcome h2 {font-size:32px; font-weight:550; padding:0;}
.welcome p {font-size:15px; color:#888; margin-top:14px;}
.st-key-composer {position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
 width:min(860px,calc(100vw - 40px)); z-index:100; border:1px solid #8883; border-radius:24px;
 background:var(--background-color,#fff); box-shadow:0 8px 40px #0000000d; padding:16px 20px 10px;}
body:has([data-testid="stSidebar"][aria-expanded="true"]) .st-key-composer {
 left:calc(50% + 130px); width:min(860px,calc(100vw - 320px));}
.st-key-composer [data-testid="stTextArea"] textarea {border:0 !important; background:transparent; box-shadow:none !important; font-size:16px;}
.st-key-composer [data-testid="stTextArea"] [data-baseweb="textarea"] {border:0 !important; background:transparent;}
.st-key-composer [data-testid="stVerticalBlock"] {gap:.35rem;}
.st-key-composer [data-testid="stCaptionContainer"] {font-size:11px; opacity:.7;}
.st-key-start_workflow button {border-radius:50px; background:#25272c; color:white; border:0;}
@media(max-width:700px) {
 .block-container {padding:1.5rem 1rem 270px;}
 .st-key-composer,body:has([data-testid="stSidebar"][aria-expanded="true"]) .st-key-composer {left:50%; width:calc(100vw - 24px); bottom:12px; padding:12px;}
 .welcome {margin-top:12vh;} .welcome h2 {font-size:26px;}
}
</style>"""


def _apply_example() -> None:
    choice = st.session_state["example_choice"]
    st.session_state["request_input"] = EXAMPLES[choice]


def _submit() -> None:
    page = st.session_state[SESSION_KEY]
    config = get_configuration()
    budget = RunBudget(max_logical_calls=st.session_state["max_logical_calls"], max_elapsed_seconds=st.session_state["max_elapsed_seconds"])
    start_task(page, st.session_state["request_input"], config.configured, config.model_name, review_enabled=st.session_state["review_enabled"], max_revisions=st.session_state["max_revisions"], thinking_enabled=st.session_state["thinking_enabled"], budget=budget, durable=st.session_state["durable_enabled"])


def _settings(page, config):
    with st.popover("设置", icon=":material/tune:", disabled=page.running):
        st.caption("任务设置 · 仅影响下一次启动")
        st.toggle("开启思考模式", key="thinking_enabled", disabled=page.running)
        enabled = st.checkbox("开启代码审查", key="review_enabled", disabled=page.running)
        st.checkbox("保存进度并支持澄清", key="durable_enabled", disabled=page.running)
        st.number_input("最大修复次数", min_value=0, max_value=MAX_REVISIONS, value=1, step=1, key="max_revisions", disabled=page.running or not enabled)
        with st.expander("运行预算"):
            st.number_input("模型逻辑调用上限", min_value=1, max_value=100, value=40, step=1, key="max_logical_calls", disabled=page.running)
            st.number_input("总时限（秒）", min_value=1, max_value=7200, value=1800, step=1, key="max_elapsed_seconds", disabled=page.running)
            st.caption("时限与停止请求在安全边界生效，在途调用可能继续返回。")
        st.caption(f"模型：{config.model_name}")
        st.caption("默认交付代码文本；静态审查不代表测试通过。")


def _composer(page, config):
    with st.container(key="composer"):
        st.text_area("开发需求", key="request_input", height=104, label_visibility="collapsed",
                     placeholder="描述你想开发的软件，或提出一个任务…", disabled=page.running)
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            _settings(page, config)
            with st.popover("示例", icon=":material/lightbulb:", disabled=page.running):
                st.selectbox("示例需求", list(EXAMPLES), key="example_choice", on_change=_apply_example, disabled=page.running)
            st.space("stretch")
            if page.running:
                st.button("停止", icon=":material/stop:", key="stop_workflow", disabled=page.task is not None and page.task.cancel_event.is_set(), on_click=request_stop, args=(page,))
            st.button("发送" if not page.running else "运行中", icon=":material/arrow_upward:", key="start_workflow", type="primary", disabled=page.running or not config.configured, on_click=_submit)
        if page.running and page.task is not None and page.task.cancel_event.is_set():
            st.caption("已请求停止，等待当前调用返回。")
        else:
            st.caption("思考" + ("已开启" if st.session_state["thinking_enabled"] else "已关闭") + " · " + ("审查模式" if st.session_state["review_enabled"] else "标准模式") + " · 发送后开始执行")


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
        st.markdown("### Workflow Studio")
        st.caption("你的开发工作台")
        with st.expander("历史运行", expanded=False):
            render_history(page)
        with st.expander("已保存的进度", expanded=False):
            render_saved_tasks(page)

    @st.fragment(run_every=0.5 if page.running else None)
    def workspace() -> None:
        finished = collect_updates(page)
        if finished:
            # Re-enable sidebar controls and stop timed polling after completion.
            st.rerun()
        if page.snapshot is None and not page.running:
            st.markdown('<div class="welcome"><div class="eyebrow">WORKFLOW STUDIO</div><h2>今天想构建什么？</h2><p>从一个想法开始，逐步完成需求、设计与代码。</p></div>', unsafe_allow_html=True)
            if page.error:
                st.error(page.error)
            return
        render_progress(page)
        render_durable_controls(page, config.configured)
        if page.thinking_enabled is not None and not page.from_history:
            st.caption("本次任务思考模式：" + ("已开启" if page.thinking_enabled else "已关闭"))
        render_save_status(page)
        st.divider()
        render_results(page)
        if page.result is not None:
            with st.expander("验证与下载", expanded=False):
                render_validation(page)
                render_exports(page)

    workspace()
    _composer(page, config)


if __name__ == "__main__":
    main()
