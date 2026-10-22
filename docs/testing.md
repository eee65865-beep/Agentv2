# 测试与验收指南

## 全项目缺陷审查记录（2026-10-22）

本轮只修复可复现的正确性问题，没有新增功能或开展无关重构。审查覆盖 Agent 输入/输出、图与状态转换、预算、持久化恢复、增量修复、固定验收、Web 后台任务和存储导出边界。

| 缺陷 | 修复与回归 |
| --- | --- |
| 澄清问题中的文字可被误认作用户确认的来源 | 需求 Agent 和状态边界仅接受原始请求、实际回答中的引文；问题继续提供上下文但不能证明需求来源。 |
| 原生结构化输出校验后仍返回未规范化对象；原生校验异常可能引用未赋值变量 | Requirements/Design 返回重新校验后的嵌套模型，异常进入有限 JSON 修复，避免字典属性错误和未赋值变量错误。 |
| Requirements/Design 在替代客户端返回可解析但被截断的 JSON 时仍接受结果 | 对 length/max_tokens/max_output_tokens/incomplete 元数据拒绝结果，不继续格式重试。 |
| 含澄清但无可选预算的合法结果无法保存 | v3 归档允许预算为空，保留 v1/v2 版本约束；验证读写与幂等保存。 |
| 图/数据库异常或预算账本落盘异常被包装成持久任务业务失败，页面还会补造终态 | 基础设施异常向调用方传播；预算持久化异常在模型和节点边界保持类型，页面不补造或归档失败结果；用短暂写入故障验证恢复和累计预算。 |
| 普通工作流遇到图执行异常后预算回退 | 同步、异步、状态流、事件流均从实际 tracker 生成异常终态预算，同时保留取消停止状态。 |
| 失败验证报告未核对固定用例身份与完成度 | 所有报告核对注册用例前缀，finished 必须包含完整用例；正常中断的已知前缀仍兼容，阻止伪造/缺失反馈进入修复。 |
| 候选持久化文件的深层 JSON 可令验证抛出 RecursionError | 作为存储不匹配返回，不让解析异常逃逸验证服务。 |
| 修复后的候选在验证后端异常时保留旧预算 | pending 候选始终使用最新累计预算，保留代码和收据，父运行不变。 |

新增 **31 项回归用例**：`test_review_bugfixes.py` 28 项、`test_durable.py` 2 项、`test_incremental_repair.py` 1 项。另将两项旧测试的对象身份断言改为数据等价断言，以允许边界重新校验。澄清准备完成、interrupt 尚未持久化时中断并继续的场景也通过，未据此修改正常逻辑。

最终全套离线回归：**718 passed, 2 skipped in 33.34s**；`compileall`、`pip check`、`git diff --check` 通过。两项跳过为 OS 符号链接权限测试与未开启的真实 Docker 测试。本轮没有调用真实模型 API 或运行真实容器，不将 Mock 协议通过视为这些集成已验收。

```powershell
$reviewTemp = Join-Path $env:TEMP ('agentworkflow-review-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp=$reviewTemp -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe -X utf8 -m compileall -q src tests app.py
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

## 模块 6 验收记录（2026-10-22）

增量产物修复与效率指标完整离线回归：**687 passed, 2 skipped in 26.08s**。新增 `test_incremental_repair.py` 的 44 项通过用例；`compileall` 和 `git diff --check` 通过。

覆盖纯内存补丁原子应用、未改文件内容/顺序/元数据保留、代码/上下文/反馈摘要绑定、未知问题及路径/操作权限、大小写/目录冲突、原生/JSON Schema 与有限修复、合并后的文件数/体积/空集/AST 校验、截断和网络不重试、累计预算与停止保留产物。实际运行 LangGraph 与 SQLite 验证修复收据、存储/导出与 checkpoint 序列化，检查旧事件缺失收据时仍按原字节幂等保存。真实 AppTest 验证增量修复详情，固定验收 Mock 后端验证新版重验、旧证据失效、回归分母、源预算继承与 pending 候选保留。

测试中成功补丁只返回一个小代码文件，而基线包含未改的大文档，验证其规范化补丁字节明显小于完整产物；没有用这一夹具推导实际 Token、费用或网络速度。原有 Coding/Review 集成测试仍校验真实上下文与程序门禁，只将修复响应从完整 CodingOutput 改为新版 CodePatch。

两项跳过仍是 OS 符号链接权限测试和未启用的 Docker canary。本机没有可用隔离后端，本次未调用真实模型或执行生成代码；新 Schema 的真实模型兼容性与完整容器修复效果未作验收。

```powershell
$patchTemp = Join-Path $env:TEMP ('agentworkflow-incremental-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp=$patchTemp -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe -m compileall -q src tests app.py
git diff --check
```

## 模块 5 验收记录（2026-10-22）

隔离验证服务与固定评测完整离线回归：**643 passed, 2 skipped in 25.24s**；`compileall` 和 `git diff --check` 通过。新增 `test_validation_runtime.py` 的 38 项通过用例及 1 项显式 Docker 集成测试。

验证服务使用 Mock Docker 协议检查容器限制、只读绑定、最小环境、强制清理和容器 State 分类；实际受限子进程仅执行仓库自有的打印/休眠夹具以检查管道限量与终止，不运行模型生成内容。其余覆盖固定输入输出比较、空集/后端缺失、错误/超时/OOM、日志不持有原始输出、过期证据、模型无法覆盖失败、存档冲突/损坏、评测分母、配置和实际页面的显式执行/重绘不重复。

两项跳过：`test_persistence.py` 的 OS 符号链接权限测试；`test_opt_in_real_container_checks_identity_filesystem_and_network` 未开启 `WORKFLOW_VALIDATION_INTEGRATION=1`。本机未检测到 Docker，未运行容器、下载镜像、访问模型 API 或执行生成代码。参数和协议通过不代表真实隔离已验收。

```powershell
$validationTemp = Join-Path $env:TEMP ('agentworkflow-validation-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp=$validationTemp -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe -m compileall -q src tests app.py
git diff --check

# 仅读取版本化任务目录，不执行候选程序
.\.venv\Scripts\python.exe -m workflow_studio.validation --list
```

真实集成仅在管理员预装审核后的本机 Linux Docker 后端、配置固定 RepoDigest 后显式开启。Canary 只在容器内运行仓库可信样例，检查非 root、宿主 Key 未传入、产物目录不可写与网络不可达，再执行整数加法及文件写入固定用例；没有通过时不可将本环境记录为已验证。离线测试额外覆盖 holder 容器仍运行时读取 tmpfs 的时序，文件系统写入不等于重启持久性。

## 模块 4 验收记录（2026-10-22）

checkpoint、等待澄清与恢复升级后的最终完整离线回归：**605 passed, 1 skipped in 22.44s**。`compileall` 和 `git diff --check` 通过。

新增 `test_durable.py` 的 25 个用例，覆盖 SQLite 原生 interrupt/resume、重开会话、非法/重复/过期回答、跨连接执行锁、失败节点关联重试、需求/设计修订、预算继承与澄清上限、版本拒绝、固定模型参数、密钥排除、安全序列化、异步入口及 ContextVar 清理、终态 v3 存档、稳定编号与旧审查摘要兼容、控制器与实际 Streamlit 表单。真实子进程在模型调用内部 `os._exit(23)` 后，验证锁释放、已提交阶段不重跑、未知调用保留预算，并从原尝试继续完成。

本次真实运行框架、SQLite 和子进程；Agent 业务使用 Mock，未调用真实模型 API或执行生成代码。唯一跳过项仍为 `tests/test_persistence.py:212`，OS 不允许创建符号链接。恢复不保证外部请求恰好一次，也没有验证真实 API 在断电后的账单。

```powershell
$checkpointTemp = Join-Path $env:TEMP ('agentworkflow-checkpoints-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp=$checkpointTemp -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe -m compileall -q src tests app.py
git diff --check
```

## 模块 3 验收记录（2026-10-22）

全局预算与协作式停止升级后的完整离线回归：**580 passed, 1 skipped in 15.81s**。`compileall` 和 `git diff --check` 通过。

新增 `test_budget.py` 的 38 个用例，原有 542 个通过用例继续通过；已有遥测字段断言同步加入实际更新的 budget 字段，流替身接收新增可选参数。验证内容包含限额参数、逻辑调用去重、未知 token 预留与结算、实际 SDK + MockTransport HTTP 重试准入、剩余时限、并发/嵌套上下文隔离、流关闭与 yield 清理、四种公开入口、停止/取消/超时的部分产物、精确重复问题、停止路由、v1/v2 存档和页面按钮。

唯一跳过项仍为 `tests/test_persistence.py:212`，OS 不允许创建符号链接。普通测试未调用真实模型 API，未执行生成代码，也未验证真实网络下的强制终止；当前停止为协作式。

```powershell
$budgetTemp = Join-Path $env:TEMP ('agentworkflow-budget-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp=$budgetTemp -p no:cacheprovider --tb=short
```

## 模块 2 验收记录（2026-10-22）

结构化 Review 与程序静态门禁升级后的完整离线回归：**542 passed, 1 skipped in 13.86s**。`compileall` 和 `git diff --check` 通过。

新增 `test_review_policy.py` 的 55 个用例，原有 487 个通过用例继续通过。覆盖模型高分/声明通过却存在阻断问题、保守拒绝、结构化位置与证据、失效代码/上下文绑定、模型不可填写可信摘要、原生/JSON 有限修复和字节上限、需求/设计问题提前停止、伪造成功终态被拒绝、v1 原字节兼容、存档/导出及页面展示。仍未调用真实模型 API 或执行生成代码。

唯一跳过项仍为 `tests/test_persistence.py:212`，OS 不允许创建符号链接。本次命令：

```powershell
$reviewTemp = Join-Path $env:TEMP ('agentworkflow-review-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp=$reviewTemp -p no:cacheprovider --tb=short
```

## 模块 1 验收记录（2026-10-22）

需求子契约与稳定追溯升级后的完整离线回归：**487 passed, 1 skipped in 12.33s**。另执行 `python -m compileall -q src tests app.py`，退出码 0。

新增 29 个追溯契约用例；原有 458 个通过用例继续通过。唯一跳过项仍为 `tests/test_persistence.py:212`，原因是当前 OS 不允许创建符号链接。

验证范围包括稳定 FR/AC 引用、来源校验及有限修复、真实 LangGraph 节点衔接、存储和 Markdown 导出、旧版原字节存档读取与幂等保存，以及 Streamlit AppTest 页面展示。本次未调用真实模型 API，未执行生成代码，不代表生成软件已通过功能验收。

由于固定临时目录曾发生冲突，本次使用独立临时路径：

```powershell
$moduleTemp = Join-Path $env:TEMP ('agentworkflow-contracts-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp=$moduleTemp -p no:cacheprovider --tb=short
```


## 测试原则

普通 pytest 不调用真实 DeepSeek API，也不执行 AI 生成代码。Agent 与模型调用用 Mock；LangGraph、Pydantic、Streamlit AppTest、ZIP 和本地临时文件使用实际组件。部分测试用真实 SDK + httpx2.MockTransport 验证请求参数与重试次数，仍没有外网请求。

pytest 配置 testpaths=["tests"]；SDK smoke/integration 入口在 src 下，不作为普通测试收集。tests/test_smoke_test.py 测试的是 Mock 和显式开启保护。tests/conftest.py 为每个测试隔离 RunStore，并关闭测试页面的实际文件日志配置，避免污染真实历史。

## 测试覆盖映射

| 测试文件（tests/） | 核心覆盖 |
| --- | --- |
| test_imports.py、test_config.py | 包导入、配置优先级、无 Key 初始化、非法参数、凭据隐藏 |
| test_deepseek_client.py、test_smoke_test.py | 统一工厂、异常翻译、原生结构化输出、截断、Mock 连通性和 --run 保护 |
| test_requirements_agent.py | 正常/空/过短需求、无效 JSON、有限修复、模型异常 |
| test_requirements_contract.py | 持久需求/验收编号、引用与覆盖、原文来源、v1/v2 兼容、存储导出与页面展示 |
| test_design_agent.py | 消费结构化需求、约束保留、功能覆盖、模块依赖和调用错误 |
| test_coding_agent.py、test_coding_output.py | 两阶段输入、代码路径/语法/大小/重名、截断与修复 |
| test_review_agent.py | 三阶段输入、合法反馈、格式修复和调用异常 |
| test_review_policy.py | 分级静态门禁、文件/行号/引用、上下文摘要、修复分类、终态防绕过、v1/v2 存档和页面 |
| test_budget.py | 全局预算、实际 SDK 尝试、未知用量预留、会话隔离、停止/取消、部分结果、存档与页面 |
| test_durable.py | SQLite checkpoint、澄清、进程崩溃恢复、锁与过期命令、关联重试/修订、累计预算、v3 存档与页面 |
| test_validation_runtime.py | 隔离协议、输出/时限/清理、独立证据、固定用例/评测分母、Review 联合门禁、页面显式执行与可选真实容器 |
| test_incremental_repair.py | 补丁权限/摘要/原子合并、完整产物重验、收据与旧档兼容、图/checkpoint/UI、固定验收修复/回归和预算 |
| test_workflow_state.py | 实际图状态更新、reducer、事件冲突、独立运行隔离 |
| test_workflow.py | 生产三节点图、顺序/传参、失败中断、同步/异步/流式终态 |
| test_review_workflow.py | 条件图、0–3 修复上限、历史代码、审查异常和审查页面 |
| test_telemetry.py | 实际节点事件、耗时/字段、token 缺失、采集上下文隔离与脱敏 |
| test_web.py | 输入/示例/提交、实际事件、失败显示、重渲染不重复调用、思考开关与并发隔离 |
| test_persistence.py | 完整/部分保存、写入中断后重试、冲突、损坏/缺失文件、路径/大小、历史页面 |
| test_error_policies.py | 实际 SDK 的认证不重试、429/超时有限重试、恢复、脱敏日志、导出边界 |
| test_generation_speed.py | 普通/结构化请求传入思考选项、配置校验、上下文异常恢复；不测真实速度 |

pytest 临时根目录为项目内 .pytest_cache/tmp，由 pytest 管理并清理；不要在该目录保存人工文件。

## 可复现命令

在项目根目录安装 dev 依赖后：

```powershell
# 全套离线回归
.\.venv\Scripts\python.exe -X utf8 -m pytest -q

# Agent
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_requirements_agent.py tests/test_design_agent.py tests/test_coding_agent.py tests/test_coding_output.py tests/test_review_agent.py -q

# State、图与审查分支
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_workflow_state.py tests/test_workflow.py tests/test_review_workflow.py -q

# Web、日志与本地存储
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_web.py tests/test_telemetry.py tests/test_persistence.py tests/test_error_policies.py tests/test_generation_speed.py -q

# 环境和语法
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src tests app.py
.\.venv\Scripts\python.exe -m workflow_studio
```

compileall 只编译本应用与测试源文件，不运行生成的软件。当前没有覆盖率统计，不报告虚构覆盖率。

## 人工页面验收步骤

这些是可重复操作步骤，不能仅凭列出步骤就声称已执行真实 API 验收：

1. 启动 Streamlit，检查模型名、Key 配置状态和三个示例；没有 Key 时可读历史但不可提交。
2. 提交小型需求，观察每个节点真实 started/completed；检查结果 Tabs、耗时和日志。
3. 开启审查，检查反馈、修复次数与历史代码；预算耗尽应显示仍有问题。
4. 打开历史、切换控件、下载后重渲染，确认不重复启动任务。
5. 本应用对 Design 异常和磁盘错误的自动验收见 test_web.py/test_persistence.py；不要为人工验收破坏真实文件权限或生产历史。

## 显式真实 API 测试

以下会产生 API 使用量，未包含在普通 pytest 内。只有主动执行 --run 才发送请求；不加该参数退出码为 2。

```powershell
# 单次逻辑模型调用，检查非空文本
.\.venv\Scripts\python.exe -X utf8 -m workflow_studio.llm.smoke_test --run

# 默认标准模式，极简整数加法需求，完整三 Agent 链路
.\.venv\Scripts\python.exe -X utf8 -m workflow_studio.workflows.integration_test --run
```

smoke 输入为“请只回答：DeepSeek API 连接成功。”，判定条件是非空文本，不严格匹配该句。
真实工作流入口判断 status=completed 与生成对象有效；不执行代码/测试、不自动导出或持久化。SDK 重试与 Agent 修复可能导致一次逻辑验收产生多次请求。
