# 系统架构

本文依据当前 src/workflow_studio 代码，不将预留能力描述为已实现功能。

## 分层结构

| 层 | 目录/文件 | 职责 |
| --- | --- | --- |
| 页面 | web/app.py、components.py、timeline.py、review.py | 输入、结果 Tabs、真实事件时间线、修复详情、下载按钮 |
| 会话控制 | web/controller.py | PageState、后台 Thread/Queue、启动防重、终态判断 |
| 图服务 | workflows/runner.py、graph.py、routing.py | 新运行初始化、编译 StateGraph、invoke/ainvoke/stream、条件路由 |
| 图节点 | workflows/nodes.py | 从 State 恢复上游模型，调用 Agent，记录事件并返回部分更新 |
| Agent | agents/*_agent.py、agents/prompts/ | 单阶段业务、独立 Prompt、结构化输出与有限格式修复 |
| 模型客户端 | llm/deepseek_client.py、retry_policy.py | ChatDeepSeek 初始化、调用、错误翻译、HTTP 重试约束 |
| 数据约束 | state/ | 四阶段 Pydantic 模型、State channel/reducer、跨阶段校验、终态结果 |
| 本地持久化 | storage/、web/persistence.py、web/history.py | 完整/部分终态存档、读取、历史展示、仅保存重试 |
| 导出 | exporters/ | 路径校验与内存 Markdown/ZIP；不执行、不解压 |
| 横向支持 | config.py、errors.py、diagnostics.py、llm/options.py、llm/telemetry.py | 配置、友好提示、脱敏日志、任务上下文、实际用量 |

## 文件夹结构

以下为源代码及文档树；省略虚拟环境、缓存、安装生成的 egg-info 和本地凭据。

```text
AgentWorkflow/
├── app.py
├── pyproject.toml
├── .env.example
├── .gitignore
├── .streamlit/config.toml
├── README.md
├── docs/
│   ├── architecture.md
│   ├── workflow.md
│   ├── agent_design.md
│   └── testing.md
├── src/workflow_studio/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── errors.py
│   ├── diagnostics.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── requirements_agent.py
│   │   ├── design_agent.py
│   │   ├── coding_agent.py
│   │   ├── review_agent.py
│   │   └── prompts/{__init__,requirements,design,coding,review}.py
│   ├── workflows/{__init__,graph,routing,nodes,runner,integration_test}.py
│   ├── state/{__init__,requirements,design,coding,review,validation,workflow,result,telemetry,tracing}.py
│   ├── llm/{__init__,deepseek_client,errors,retry_policy,options,telemetry,smoke_test}.py
│   ├── web/{__init__,app,configuration,controller,components,timeline,review,examples,history,persistence}.py
│   ├── exporters/{__init__,paths,documents}.py
│   └── storage/{__init__,models,paths,store}.py
└── tests/
    ├── conftest.py
    ├── test_imports.py
    ├── test_config.py
    ├── test_deepseek_client.py
    ├── test_smoke_test.py
    ├── test_requirements_agent.py
    ├── test_design_agent.py
    ├── test_coding_agent.py
    ├── test_coding_output.py
    ├── test_review_agent.py
    ├── test_workflow_state.py
    ├── test_workflow.py
    ├── test_review_workflow.py
    ├── test_telemetry.py
    ├── test_web.py
    ├── test_persistence.py
    ├── test_error_policies.py
    └── test_generation_speed.py
```

大括号用于压缩展示文件列表，不是实际目录名。每个包都有 __init__.py。

## LangChain 与 LangGraph 的分工

LangChain 处理“如何提出并接收一次模型任务”：ChatPromptTemplate 构造 system/human 消息，PydanticOutputParser 提供字段格式说明，ChatDeepSeek 调用模型，with_structured_output 解析工具调用结构。

LangGraph 处理“哪个节点何时运行及怎样传递结果”：StateGraph 注册节点与边，compile 后运行；各 channel 校验/合并部分更新，条件边实现审查循环，custom/values 流向页面传递执行过程。

这里的 Agent 是明确分工的 LLM 业务函数，不是无限自主规划、浏览或执行工具的智能体。程序可以普通函数调用单个 Agent，组合流程由图负责。

## DeepSeek 调用链与配置

get_llm() → load_settings() → 任务思考模式覆盖 → DeepSeekClient(settings) → ChatDeepSeek → 注入的 DeepSeekRetryClient.chat.completions → DeepSeek API。

创建客户端不会发请求；invoke 或 invoke_structured 才会发送。统一客户端把认证、限流、网络/超时、调用错误翻译为安全应用异常；日志不输出响应正文、提示词或密钥。

原生结构化接口采用 with_structured_output(schema, method="function_calling", include_raw=True)。原始消息仅用于截断、解析和用量检查，不写入技术日志。不支持该能力或解析失败时，由 Agent 决定 JSON 回退；普通网络或认证异常不会伪装为接口不支持。

配置从项目启动目录的 .env 与进程环境读取；SecretStr 在显示时隐藏且配置序列化排除 Key。网页只获取模型名、Key 是否存在和默认思考设置。

llm/options.py 使用 ContextVar 临时覆盖思考选项。worker 在 thinking_mode(...) 范围内消费整个工作流流；LangGraph 执行器继承上下文，finally 恢复。不会修改 os.environ、.env 或 WorkflowState，也不会把密钥放入 PageState。

## Agent 内部共同行为

输入校验 → 构建独立 Prompt → get_llm → 原生结构化尝试 → Pydantic 校验 → 返回阶段对象。

原生不支持/结构错误 → 请求 JSON → 完整 JSON 解析 → 校验 → 必要时有限格式修复。默认最多 4 次逻辑模型调用：一次原生尝试、一次 JSON 回退、两次格式修复；提前成功即结束。SDK 内部 HTTP 重试可能增加物理请求次数。

Design 还校验约束原样保留及 FR 覆盖，Coding 校验路径/大小/重名/Python 语法，Review 检查输入依赖和审查反馈结构。这些不等于真实功能测试。

## 页面到 Workflow

web/app.py::_submit 读取控件 → controller.start_task 做输入和运行中防重 → 创建后台 Thread → _worker 调用 stream_workflow_events。

后台只向 Queue 写 WorkflowStreamPacket，不调用 Streamlit API。图同时输出 custom 事件和 values State 快照；节点 started 在模型返回前即可显示，values 通常在节点提交后更新。片段 workspace 每 0.5 秒调用 collect_updates，合并并去重事件，展示当前 State。没有 sleep 伪造执行。

后台流结束发送 WorkerFinished；_finish 校验终态。缺少合法终态则构造失败，不把流耗尽当成成功。之后 save_page 自动保存；磁盘保存失败单独标记，不改变 WorkflowResult.status。

切换开关、下载和历史读取不调用 start_task。审查循环中同名节点通过 revision_count 区分轮次；进度不是剩余时间预测。

## State 与事件

业务 State 是 TypedDict 及 Annotated channel 定义，运行时由 validate_state/validate_update 与 Pydantic 验证。requirements/design/code/review 在 State 中为字典，在 Agent 边界恢复为对应对象。WorkflowResult 则以类型化字段向调用者交付终态。

run_id 每次重新生成 UUID，初始列表与业务数据独立；普通字段替换，events/review_history 经 reducer 合并。事件按 event_id 去重，同 ID 不同内容报错（比较不包括 timestamp）；审查记录按 record_id 去重。

llm/telemetry.py 使用节点级 ContextVar 收集逻辑模型调用。state/tracing.py::summarize_run 只读汇总。缺失 token usage 为 null，不计为零，不估算。没有观察 SDK 内部每一次 HTTP 尝试的遥测。

## 本地保存与导出

RunStore.save(result, request, model_name, elapsed) 返回保存目录 Path；load(run_id) 返回 SavedRun(metadata, result)；list_runs(limit=100) 返回正常记录和异常记录标识。

```python
from workflow_studio.storage import RunStore

# 使用已有终态对象，不调用模型。
store = RunStore()
folder = store.save(result, request=user_request,
                    model_name=model_name, elapsed=elapsed_seconds)
saved = store.load(result.run_id)
assert saved.result == result
```

仅终态可保存，路径为项目根目录 outputs/{run_id}/。requirements.json、design.json、code.json、events.json、metadata.json 必须存在；未到达阶段写 null。启用审查额外保存 review.json，包含 latest 和 history（每轮对应代码快照）。metadata 保存需求、模型、时间、状态、审查计数和文件 SHA-256。

验证 → 独占写 .pending-* → flush/fsync → 重读验证 → 同根目录 rename 发布。历史忽略暂存目录；失败暂存目录不自动删除。相同内容幂等，不同内容同 ID 拒绝覆盖。读取校验 UTF-8、重复键、Schema、摘要、状态和事件归属，损坏记录不影响其他历史。

导出 functions 为 requirements_markdown、design_markdown、workflow_report、code_zip，全部在内存完成。生成代码路径必须安全且相对，Windows/POSIX 歧义和敏感配置路径被拒绝。JSON 存储使用固定文件名，不把生成文件写到应用源码。

## 异常与边界

SDK 仅对合适的瞬态 HTTP 错误、连接与超时有限重试；400/401/403/404/422 不重试，即使服务器建议重试。408/409/429/5xx 仍尊重 SDK 退避/重试建议。配置预算最多 5，Agent 格式预算最多 3，Workflow 修复预算最多 3，彼此不同。

节点异常记录安全类别与位置，然后抛 WorkflowNodeError，携带失败快照供 runner 生成终态；不使用失败边继续跑下游。只有业务审查不通过会进入修复分支。

Web 配置 logs/workflow_studio.log：UTF-8、每文件 1 MiB、两个轮转备份，只针对应用命名空间。Python runner 调用方可以自行配置日志 handler。

本系统没有数据库、队列服务器、任意代码执行、自动安装、checkpoint、任务续跑或多用户权限系统。扩展 Agent 时新增 Schema/Prompt/业务函数/节点/图路由及测试，而不是向页面堆叠业务逻辑。
