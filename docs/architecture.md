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
| 可选持续执行 | storage/checkpoints.py、workflows/durable.py、workflows/clarification.py、web/durable.py | SQLite checkpoint、澄清中断、恢复、关联重试/修订与预算账本 |
| 独立验收服务 | validation/、web/validation.py | Docker 后端、固定接口用例、证据绑定与存档、独立评测 CLI 和页面显式执行 |
| 增量修复 | state/patch.py、agents/incremental.py、llm/repairs.py、validation/repair.py | 补丁权限/版本校验、纯内存合并、上下文隔离收据、显式验证修复及回归比较 |
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

Requirements 新生成使用严格 v2 子契约：保存稳定 FR/AC 编号、原文引用和验收关联；Agent 与完整 State 检查引用存在于原始输入。Design 从持久目录读取编号。兼容读取仍接受 v1 需求，旧记录不补造追溯关系，存档封套格式和校验摘要机制不变。见 [工作流文档](workflow.md)。

Review 同样区分生成与运行契约：模型仅生成 `ReviewAssessmentV2` 结构化意见；适配器检查文件/行号/代码引用和 FR/AC 引用后，绑定代码与完整上下文摘要，返回兼容的 `ReviewOutput`。`state/review_policy.py` 负责确定性门禁，节点、Coding 修复入口、状态校验、页面和导出均使用同一规则。需求/设计问题在本阶段提前交付 `completed_with_issues`，不进入无效代码循环。旧 v1 审查仍按原模型结论读取，避免改写历史。

## 页面到 Workflow

主区域底部固定需求输入面板，设置与示例收进 popover，侧栏仅保留折叠的历史/持久进度入口。空白页显示简短引导，提交后才渲染进度与结果；终态验证/下载在折叠区中。保留原控件 key 与显式提交回调，设置仍在运行时锁定，不改变预算、持久化或 API 调用语义。布局适配窄屏，并为底部面板预留滚动空间。

web/app.py::_submit 读取控件 → controller.start_task 做输入和运行中防重 → 创建后台 Thread → _worker 调用 stream_workflow_events。

后台只向 Queue 写 WorkflowStreamPacket，不调用 Streamlit API。图同时输出 custom 事件和 values State 快照；节点 started 在模型返回前即可显示，values 通常在节点提交后更新。片段 workspace 每 0.5 秒调用 collect_updates，合并并去重事件，展示当前 State。没有 sleep 伪造执行。

后台流结束发送 WorkerFinished；_finish 校验终态。缺少合法终态则构造失败，不把流耗尽当成成功。之后 save_page 自动保存；磁盘保存失败单独标记，不改变 WorkflowResult.status。

切换开关、下载和历史读取不调用 start_task。审查循环中同名节点通过 revision_count 区分轮次；进度不是剩余时间预测。

## State 与事件

业务 State 是 TypedDict 及 Annotated channel 定义，运行时由 validate_state/validate_update 与 Pydantic 验证。requirements/design/code/review 在 State 中为字典，在 Agent 边界恢复为对应对象。WorkflowResult 则以类型化字段向调用者交付终态。

run_id 每次重新生成 UUID，初始列表与业务数据独立；普通字段替换，events/review_history 经 reducer 合并。事件按 event_id 去重，同 ID 不同内容报错（比较不包括 timestamp）；审查记录按 record_id 去重。

llm/telemetry.py 使用节点级 ContextVar 收集逻辑模型调用。state/tracing.py::summarize_run 只读汇总。缺失 token usage 仍为 null，不计为零。新增 budget.py 在运行级分别计数逻辑调用和 SDK 发送前准入的 HTTP 尝试；未知 token 的核算预留独立记录，不冒充实测遥测。

## 运行预算运行时

`budget.py` 提供不可变 RunBudget / BudgetSnapshot、带锁 BudgetTracker，以及 ContextVar 执行上下文。Agent 的 invoke_model 与 DeepSeekClient 装饰器复用同一次逻辑调用预留，不重复计数。后者的原生输出和普通输出都经统一结算；SDK `_prepare_request` 钩子对每次出站尝试准入并压缩分阶段 timeout。

runner 为每次执行新建预算运行时；同步、异步、流式入口共享同一规则。流式上下文只在推进底层 iterator 时生效。节点在准入前和完成边界检查预算；RunStopped 单独转换为 stopped 快照，不当作网络错误、格式错误或普通审查拒绝。停止流程仍通过携带快照的 WorkflowNodeError 退出图，不沿普通边继续执行。

Web 使用 TaskHandle.cancel_event 请求协作式停止，后台线程继续收集到合法终态后保存；模型在途请求不能由页面强杀。预算 UI 只控制下一次提交，运行中禁用。完整默认值和核算边界见 [Workflow 文档](workflow.md)。

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

仅终态可保存，路径为项目根目录 outputs/{run_id}/。requirements.json、design.json、code.json、events.json、metadata.json 必须存在；未到达阶段写 null。启用审查额外保存 review.json，包含 latest 和 history（每轮对应代码快照）。metadata 保存需求、模型、时间、状态、审查计数和文件 SHA-256。带预算的结果采用封套 schema_version=2 并保存预算快照；无预算旧封套继续按 v1 读取和幂等保存，不从历史事件补造用量。stopped 终态也可以保存、打开和导出。

验证 → 独占写 .pending-* → flush/fsync → 重读验证 → 同根目录 rename 发布。历史忽略暂存目录；失败暂存目录不自动删除。相同内容幂等，不同内容同 ID 拒绝覆盖。读取校验 UTF-8、重复键、Schema、摘要、状态和事件归属，损坏记录不影响其他历史。

导出 functions 为 requirements_markdown、design_markdown、workflow_report、code_zip，全部在内存完成。生成代码路径必须安全且相对，Windows/POSIX 歧义和敏感配置路径被拒绝。JSON 存储使用固定文件名，不把生成文件写到应用源码。

## 异常与边界

SDK 仅对合适的瞬态 HTTP 错误、连接与超时有限重试；400/401/403/404/422 不重试，即使服务器建议重试。408/409/429/5xx 仍尊重 SDK 退避/重试建议。配置预算最多 5，Agent 格式预算最多 3，Workflow 修复预算最多 3，彼此不同。

节点异常记录安全类别与位置，然后抛 WorkflowNodeError，携带失败快照供 runner 生成终态；不使用失败边继续跑下游。只有业务审查不通过会进入修复分支。

Web 配置 logs/workflow_studio.log：UTF-8、每文件 1 MiB、两个轮转备份，只针对应用命名空间。Python runner 调用方可以自行配置日志 handler。

持久模式使用本地 SQLite，无需数据库服务器；本系统没有队列服务器、宿主代码执行、自动安装或多用户权限系统。固定 Python 代码执行仅可显式通过独立容器验证服务。扩展 Agent 时新增 Schema/Prompt/业务函数/节点/图路由及测试，而不是向页面堆叠业务逻辑。

## 可选 checkpoint 存储

`CheckpointStore` 使用 `checkpoints/workflows.sqlite3` 保存完整 LangGraph checkpoint 和 pending writes，使用 WAL 与 synchronous FULL。会话记录固定工作流版本、模型非秘密参数和活跃尝试；尝试记录保存父尝试、run_id、可信入口、累计预算账本及终态快照。OS 文件锁保证同一会话跨线程/进程只运行一个消费者，进程退出由 OS 释放；执行时再检查 checkpoint ID，拒绝旧页面重复提交。

运行中预算的每次准入/结算独立持久化；图恢复不回滚已消耗额度。崩溃时未结算调用转为未知预留，等待与离线不计执行时长。此账本与节点 checkpoint 不是外部 API 的原子事务，未提交节点可能重放。SQLite 载荷禁用 pickle、限制可恢复类型和单条大小；本地数据未加密，未实现多用户权限或自动清理。

等待结果由 `DurableView` 交付，`WorkflowResult` 与 `RunStore` 仍只接受终态。含问答的终态 metadata 使用 schema_version=3 保存 `clarifications`；v1/v2 兼容读取和幂等保存。失败重试/审查修订创建关联的新 attempt_id/run_id，而不覆盖旧终态。

## 独立隔离验证边界

`validation/service.py::validate_artifact` 对已有完整终态产物执行固定验收。它不修改原 WorkflowResult、图状态、修复次数或 RunStore；每次显式执行生成新 report_id，另存 `artifacts/validation/{run_id}`。这是生成后的独立操作，不嵌入持久图的自动回边，也不在打开历史或重绘时调用模型/执行代码。业务函数与 Docker 协议分层，页面使用后台 Thread/Queue 和独立取消信号。

`DockerBackend` 只连接本机默认 Docker endpoint，CLI 使用最小环境与新建空 Docker 配置目录；不传递宿主 API Key、Docker context、代理或 TLS 配置。镜像需管理员预装并审核，必须匹配所配置 RepoDigest、Linux 平台且无声明卷。预检拒绝资源限制或 seccomp 不可用的 daemon。容器使用非特权 UID/GID 65534、禁网、只读根、cap-drop ALL、no-new-privileges、128 MiB 内存且不增加 swap、0.5 CPU、32 PID、64 文件描述符；工作区与 tmp 各使用 16 MiB tmpfs。仅挂载专用临时产物目录且只读；不挂载宿主源码、密钥或 Docker socket。

命令为固定 Python 隔离启动参数，输入仅由可信用例提供；模型的 instructions、测试建议、依赖说明和命令不能变成 shell。临时目录与子进程负责宿主编排，不被视为沙箱；真实执行只发生在容器。每例和整批有时限，stdout/stderr 各限量读取，取消/超时杀掉 CLI 后仍通过唯一容器名强制删除整个容器。清理失败阻止通过；不保存候选程序原始 stdout/stderr，以避免日志泄露与注入。Docker 容器共享宿主内核，未宣称可抵御内核逃逸；公开多租户部署需要独立 worker/VM 及额外运维隔离。

主机比较受限 JSON 输出与版本化期望，不信任容器打印的“测试通过”报告。跨模块任务另用固定入口调用 calculator.add；文件存储任务保持固定 holder 容器运行，exec 候选程序后通过 Docker cp 读取仍存在的 tmpfs 文件，仅解析有限 tar 的正规单文件并在内存检查，不解包到宿主。该检查证明文件系统写入，不证明重启持久性。普通用例启动后检查 Docker State 区分启动故障与程序非零退出、标记 OOM；文件用例检查 holder 仍在运行，再使用 exec 的退出码。非零结果均不通过。

报告包含代码/需求设计上下文摘要、验收集摘要、验证器版本、镜像 RepoDigest 与 image ID、固定命令标识、参数限额、用例退出码与状态。通过要求非空且完整的固定用例全部执行成功；无后端/不支持/人工项为 not_run，断言失败为 failed，基础设施为 error，时限为 timeout。代码、上下文、运行或验收集变化使旧证据为 stale。摘要不是签名；本地有写权限的人可重写证据，未提供远程证明。

旧终态历史的 Schema 不变，验证文件采用独立不可覆盖、可幂等读取的格式，需文件系统支持同目录硬链接原子发布。本模块未新增宿主执行替代路径、第三方依赖安装、任意测试发现或自动回写原审查。

容器参数与生命周期依据 [Docker create 官方文档](https://docs.docker.com/reference/cli/docker/container/create/)和 [Docker run 参数参考](https://docs.docker.com/reference/cli/docker/container/run)核对；实现仍需在实际部署的 Linux daemon 上运行可选 canary 验证，不能把参数构造测试当作隔离实测。

## 增量修复边界

`state/patch.py` 提供独立的 CodePatch/RepairPolicy/apply_code_patch，不执行或写入生成文件。补丁检查 code/context/feedback 三个绑定摘要、允许的操作/精确路径及已知问题；每个文件替换为完整内容，不实现文本 diff。默认策略只替换现有文件，新增/删除必须由可信调用方列入策略；不允许通过补丁扩大自身权限。合并保持原有未改文件与 CodingOutput 元数据，再做完整产物限制检查，任何一步失败均不发布新产物。

`agents/incremental.py` 与首次完整生成分开，原生与 JSON 仅接收补丁 Schema，旧式全量修复响应会被有限修复或拒绝。输入未进行语义缩写或猜测依赖裁剪；失败响应正文不进入更大的修复 Prompt。`llm/repairs.py` 使用 ContextVar 收集成功收据，节点通过已有事件交付，存档文件集合和 metadata v1/v2/v3 语义保持兼容。收据包含摘要、路径与量化字段，不包含完整代码；State/Result 重读会对照 Review 历史实际版本和差异，保留原有 token 遥测而不把字节换算成费用。

独立 `validation/repair.py` 将模块 5 的确定性证据接到受限模型修复，复用同一操作内的累计预算与源业务修复余额，为新版建立独立身份并重验固定集。证据为 error/timeout/not_run 不触发修复，回归或人工问题阻止继续。普通 LangGraph 审查仍使用静态 Review 回边；独立容器修复编排不伪装成该图中的 checkpoint 节点，也不自动改变历史。模块 5 的后端部署与隔离实测限制继续适用。
