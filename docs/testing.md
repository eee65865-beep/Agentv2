# 测试与验收指南


## 测试原则

普通 pytest 不调用真实 DeepSeek API，也不执行 AI 生成代码。Agent 与模型调用用 Mock；LangGraph、Pydantic、Streamlit AppTest、ZIP 和本地临时文件使用实际组件。部分测试用真实 SDK + httpx2.MockTransport 验证请求参数与重试次数，仍没有外网请求。

pytest 配置 testpaths=["tests"]；SDK smoke/integration 入口在 src 下，不作为普通测试收集。tests/test_smoke_test.py 测试的是 Mock 和显式开启保护。tests/conftest.py 为每个测试隔离 RunStore，并关闭测试页面的实际文件日志配置，避免污染真实历史。

## 测试覆盖映射

| 测试文件（tests/） | 核心覆盖 |
| --- | --- |
| test_imports.py、test_config.py | 包导入、配置优先级、无 Key 初始化、非法参数、凭据隐藏 |
| test_deepseek_client.py、test_smoke_test.py | 统一工厂、异常翻译、原生结构化输出、截断、Mock 连通性和 --run 保护 |
| test_requirements_agent.py | 正常/空/过短需求、无效 JSON、有限修复、模型异常 |
| test_design_agent.py | 消费结构化需求、约束保留、功能覆盖、模块依赖和调用错误 |
| test_coding_agent.py、test_coding_output.py | 两阶段输入、代码路径/语法/大小/重名、截断与修复 |
| test_review_agent.py | 三阶段输入、合法反馈、格式修复和调用异常 |
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
