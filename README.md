# Multi-Agent Workflow Studio

基于 Python、LangChain、LangGraph 与 DeepSeek API 的多 Agent 软件开发工作流。输入自然语言需求，系统按顺序完成需求分析、系统设计和代码生成；可选加入代码审查与有限修复，通过 Streamlit 展示执行过程并下载结果。

本文描述当前实现。生成的代码和测试仅作为文本交付，系统不会自动执行它们。

## 项目目标

将自然语言需求转换为结构化需求、系统设计和代码文件，通过可观察的工作流、可选代码审查与有限修复，支持小型软件项目的原型交付。

## 文档导航

- [整体架构与模块职责](docs/architecture.md)
- [Workflow、条件分支与 State 示例](docs/workflow.md)
- [四个 Agent 的设计](docs/agent_design.md)
- [测试与验收指南](docs/testing.md)

## 技术栈

版本来自当前 pyproject.toml，直接依赖固定版本；传递依赖没有完整锁文件。

| 组件 | 版本/要求 | 作用 |
| --- | --- | --- |
| Python | >=3.11,<4 | 运行环境；本机实际验证为 3.14.6 |
| LangChain | 1.4.3 | Prompt、消息和模型抽象 |
| LangGraph | 1.2.14 | StateGraph、节点、状态合并和条件循环 |
| langchain-deepseek | 1.1.1 | ChatDeepSeek 适配器 |
| OpenAI SDK | 3.26.0 | 适配器底层 HTTP 调用、异常与有限重试 |
| Streamlit | 1.65.0 | 中文 Web 界面 |
| Pydantic | 2.13.5 | 配置与结构化数据校验 |
| python-dotenv | 1.2.4 | 读取 .env |
| pytest | 9.1.1（dev） | 离线自动化测试 |

## 已实现功能

- Requirements、Design、Coding 三个 Agent，通过共享 State 传递经过校验的数据。
- 可选 Review Agent；审查未通过时按预算修复，最多配置 0–3 次，默认一次。
- 同步、异步工作流入口及阶段事件流；节点开始、完成、失败、耗时、State 读写字段和可获得的 token usage。
- 中文任务输入、三个示例、进度卡、结果 Tabs、单节点详情、审查和修复历史。
- 思考模式开关：默认读取 .env，仅覆盖本次任务，不改写文件，不影响其他会话。
- 本地 UTF-8 JSON 终态结果保存、部分结果保存、历史列表与按 run_id 打开。
- Requirements.md、Design.md、代码 ZIP、WorkflowReport.md 下载。
- 分类错误提示、有限重试、脱敏技术日志、安全路径及文件大小限制。

## 系统架构

```mermaid
flowchart TD
    UI[Streamlit 页面] --> CT[控制器与后台工作线程]
    CT --> RUN[Workflow 流式服务]
    RUN --> GR[编译后的 LangGraph StateGraph]
    GR --> AG[独立 Agent 业务函数]
    AG --> LC[统一 get_llm / DeepSeekClient]
    LC --> API[ChatDeepSeek / SDK / DeepSeek API]
    GR --> EV[custom 事件与 values State 快照]
    EV --> CT
    CT --> UI
    CT --> ST[RunStore 本地终态存档]
    UI --> HI[历史读取]
    HI --> ST
    UI --> EX[内存 Markdown 和 ZIP 导出]
```

标准模式：START → Requirements → Design → Coding → END。
审查模式：Coding → Review；通过结束，未通过且有预算则返回 Coding，预算耗尽以 completed_with_issues 结束。调用异常以 failed 停止，不伪装为审查未通过。

State 中保存业务数据和事件，不保存 API Key。Agent 返回部分更新，合并由 LangGraph channel/reducer 完成。详见 [工作流文档](docs/workflow.md)。

## 环境安装

从项目根目录执行以下 PowerShell 命令。安装依赖需要网络，但以下初始化与普通测试不调用 DeepSeek。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip check
```

若未安装 Python 3.11，可使用已安装的兼容 Python 创建虚拟环境；本机用 Python 3.14.6 测试，未声称已逐一实测所有支持版本。使用虚拟环境解释器完整路径，无需执行激活脚本。

## DeepSeek API 配置

项目已有 .env 时直接编辑，不覆盖现有密钥。仅首次且文件不存在时复制：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

填写本地 .env，不把真实密钥写入源代码、聊天或版本控制：

```dotenv
DEEPSEEK_API_KEY=在本地替换为你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TEMPERATURE=0.2
DEEPSEEK_TIMEOUT=300
DEEPSEEK_MAX_RETRIES=2
DEEPSEEK_THINKING=disabled
```

| 配置 | Settings 默认值 | 说明 |
| --- | --- | --- |
| DEEPSEEK_API_KEY | 无 | 安装/离线测试不要求配置，真实调用要求非空 |
| DEEPSEEK_BASE_URL | https://api.deepseek.com | HTTP 服务地址 |
| DEEPSEEK_MODEL | deepseek-flash | 可修改；模型须支持相应调用能力 |
| DEEPSEEK_TEMPERATURE | 0.2 | 范围 0–2 |
| DEEPSEEK_TIMEOUT | 60 | 有限正数、秒；示例文件为代码生成配置 300 |
| DEEPSEEK_MAX_RETRIES | 2 | 范围 0–5，首次请求之外的重试次数 |
| DEEPSEEK_THINKING | disabled | enabled 或 disabled |

优先级：Settings 默认值 < 指定 .env < 进程环境变量。默认 .env 路径相对于当前工作目录，所以应从项目根目录启动。读取不会修改 os.environ，不向父目录搜索，不展开 dotenv 变量插值。

Web 思考开关首次读取上述默认值，之后保留会话选择；提交时覆盖本次任务全部 Agent 调用。运行中的任务不能切换。新任务读取新配置，已经创建的客户端不会中途更新。

项目按官方接口通过 extra_body.thinking.type 传入思考开关。思考模式可能增加等待时间，关闭可能影响输出质量，不承诺加速比例。[DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)

## 启动方式

```powershell
# 离线配置检查：不会发送模型请求，也不会启动页面
.\.venv\Scripts\python.exe -m workflow_studio

# Web 页面
.\.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器通常访问 http://localhost:8501，以 Streamlit 终端输出为准。无 Key 时可以查看历史；启动新 Workflow 按钮禁用。

## 使用示例

1. 选择“Python 命令行计算器”“学生成绩管理系统”或“简单 Todo 任务管理器”，或填写至少 10 字符的开发需求。
2. 首次演示建议标准模式、关闭思考，需求限定为小型项目。
3. 需要审查时勾选“开启代码审查”，配置最大修复次数；0 表示审查但不修复。
4. 点击“启动 Workflow”才产生真实 API 请求。观察节点状态、时间线与结果；修复时查看各轮审查记录。
5. 下载文档和代码 ZIP。完成或失败结果自动保存；磁盘保存失败可“仅重试保存”，不会重跑模型。
6. 在左侧历史列表或通过完整 run_id 打开旧结果；重绘、切换详情和下载不会重新调用 API。

最小需求示例：

> 使用 Python 标准库开发两个整数相加的命令行工具，提供基本 unittest 测试和运行说明，不使用数据库或网络。

Python 业务入口也可调用（下例会产生真实 API 请求，不属于离线检查）：

```python
from workflow_studio.workflows import run_workflow

result = run_workflow("使用 Python 标准库开发整数加法工具，提供基本测试和运行说明。")
print(result.run_id, result.status)
```

runner 不自动写磁盘。需要时对已有 result 显式调用 RunStore.save；方法签名和示例见架构文档。

## 测试命令

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src tests app.py
```

普通 pytest 使用 Mock、固定数据及真实本地测试图/临时文件，不产生模型费用。符号链接测试可能因系统权限跳过。完整测试说明见 [testing.md](docs/testing.md)。

以下两条需显式执行，会调用真实 API，可能产生费用；本次文档核对没有执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m workflow_studio.llm.smoke_test --run
.\.venv\Scripts\python.exe -X utf8 -m workflow_studio.workflows.integration_test --run
```

## 常见问题

**为什么 Coding Agent 很慢？** 三个阶段有输入依赖，耗时累加；代码输出长、开启思考、SDK 重试、JSON 修复、审查循环都会增加等待。先使用小需求、关闭思考及审查，查看节点详情中的实际耗时和调用次数。300 秒超时是等待设置，不是加速设置，也不是整节点硬性 deadline。

**开关会改写 DEEPSEEK_THINKING 吗？** 不会。它只覆盖提交任务的上下文。新会话仍读取 .env 默认值。旧历史没有保存思考设置，不从当前开关推断历史模式。

**为什么已配置仍认证失败？** “已配置”只表示 Key 非空，不表示 API 已验证。401/403 不重试；检查账户权限和进程环境变量是否覆盖 .env。

**如何处理限流、超时和格式错误？** SDK 对合适的瞬态错误有限重试；Agent 对结构错误进行有界修复。预算用尽终止，失败节点之后不继续执行。不会无限重试，也不自动重跑整图。

**审查通过是否代表代码正确？** 不代表。Python 文件只做 ast.parse 语法检查，LLM 审查是静态意见，生成的测试没有执行。

**结果存在哪里？** 项目根目录 outputs/{run_id}/；固定 JSON 文件、UTF-8 编码。失败时已完成阶段保留，缺失阶段为 null。review.json 仅在启用审查时存在。技术日志在 logs/workflow_studio.log。相关目录已忽略提交。

**保存失败或历史损坏怎么办？** 保存失败不改变业务终态，当前结果仍可下载；确认目录权限/磁盘空间后只重试保存。损坏记录会被跳过，打开失败不替换当前结果。不要把输出目录指向源码目录或链接目录。

**导出有哪些限制？** 最多 20 个文件、每文件 64 KiB、CodingOutput 总计 256 KiB；ZIP 512 KiB、单份 Markdown 2 MiB；持久化 JSON 每文件 4 MiB、每运行 8 MiB、事件最多 512 条。拒绝路径穿越、绝对路径、敏感文件、重名及文件/目录冲突，不解压到源码目录。

## 已知限制

- 没有 checkpoint、断点续跑、任务取消/暂停或运行中状态持久化。保存的是终态结果，不是恢复执行所需的完整现场。
- 页面是阶段事件流，没有 token 逐字输出；进度按节点状态统计，修复时可能回退，并非耗时估计。
- 本地历史没有账户隔离、加密、备份管理或跨进程写协调。摘要用于损坏检测，不是防篡改签名；业务需求与代码会按要求保存在本地。
- 未发布的 .pending-* 目录保留供排查，没有自动清理。刷新/断开可能失去运行中会话，后台调用可能继续完成。
- 同步 LLM 包装器未提供独立 ainvoke；工作流有 arun_workflow，LangGraph 会在线程执行器中运行同步节点。
- 语义正确性和约束实际遵守程度无法仅靠 Schema 证明；没有自动运行代码、安装生成依赖、自动功能验收或数据库等额外基础设施。
- SDK 重试策略使用固定版本的 _should_retry 钩子；升级 SDK 需要重跑策略与适配测试。没有全量依赖锁文件。
