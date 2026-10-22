# Agent 设计

需求追溯子契约与结构化 Review/静态门禁已接入 v2，详见 [工作流文档](workflow.md)。其余未标注部分继续描述当前实现。

每个 Agent 都是独立、可 Mock 的同步业务函数，内部调用统一 get_llm。业务函数不了解 Streamlit，不自行构建 Graph，也不修改全局业务字典。

## 共同约定

- Prompt 位于 src/workflow_studio/agents/prompts/，由 ChatPromptTemplate 分离 System 与 Human 消息；Schema 格式说明来自 PydanticOutputParser.get_format_instructions。
- 优先 DeepSeekClient.invoke_structured，使用 LangChain function_calling；不兼容/结构失败时请求完整 JSON，再由 Pydantic 校验。允许移除单个外层 JSON 围栏，不用 eval，不接收不完整 JSON。
- 默认 max_repair_attempts=2，允许 0–3；最多一次原生、一次 JSON 回退和指定次数的修复，成功即停止。网络/认证等调用失败直接向上传播，不作为格式修复。
- 所有 Agent 通过 invoke_model 接入运行级逻辑调用与格式修复预算；DeepSeekClient 嵌套调用不会重复扣额。预算停止使用 RunStopped 独立传播，不进入格式修复。每个 Agent 的原有局部上限仍生效。
- 统一客户端拒绝 finish_reason=length/max_tokens/max_output_tokens 或 status=incomplete 的响应。输出被截断不能算成功。
- Prompt 把用户文档/代码当作数据，要求忽略改变角色、泄露密钥或执行命令的指令。这是提示约束，不宣称完全防御所有提示注入。
- 简洁输出、紧凑输入 JSON 降低冗余，不牺牲已确认需求和完整文件。运行思考开关由统一客户端读取上下文，各 Agent 不自行管理密钥。

## Requirements Agent

**实现**：agents/requirements_agent.py；Prompt：agents/prompts/requirements.py；Schema：state/requirements.py；图节点：requirements_agent_node。

**目标**：将自然语言变成可供下游消费的需求文档，只分析需求，不生成实现代码。

**接口**：analyze_requirements(user_request: str, *, max_repair_attempts: int = 2) -> RequirementsOutput。

**输入**：至少 10 个字符的非空需求。剔除首尾空白后再检查；非字符串或过短输入在模型调用前拒绝。

| 输出字段 | 类型 | 含义 |
| --- | --- | --- |
| project_name | str | 项目名称，未指定时 Prompt 要求暂定并标为待确认 |
| summary | str | 目标和范围 |
| functional_requirements | list[str] | 至少一项已确认功能 |
| non_functional_requirements | list[str] | 已确认质量需求，未说明可为空 |
| constraints | list[str] | 用户明确限制 |
| assumptions | list[str] | 假设和待确认信息 |
| acceptance_criteria | list[str] | 至少一项可验证验收标准 |

文本必须非空，模型 strict=True、extra=forbid。Field 描述字段含义；“假设必须标注”等语义主要依靠 Prompt，不是 Pydantic 自动证明。

新生成时使用 `RequirementsOutputV2`，额外必需 `contract_version=2`、`requirement_details`、`acceptance_details` 和 `open_questions`。需求目录记录持久 FR ID、与文字列表一致的描述和原文片段；验收目录记录持久 AC ID、描述及 FR 引用。检查唯一 ID、引用合法、每个功能有验收覆盖、目录文字与列表一致；Agent 与完整 State 还核对引用确实存在于有效输入。来源存在不等于语义已被证明。

`open_questions` 每项包含 Q 编号、问题、blocking 与 FR 引用；无问题时必须为 `[]`，旧数据读取默认空列表。仅持久模式对阻断问题等待；用户回答保存为问题原文与 answer，重新生成时明确回答优先，问题本身不作为用户确认事实。完全相同的需求描述与验收关联保留上一版编号，新项从上一版最大编号后分配，不进行语义匹配。

`RequirementsOutput` 作为 v1/v2 兼容读取边界，旧数据无版本时按 v1 读取，详情列表为空，不补造来源或验收关联。仅新生成使用严格 v2 Schema。

**Prompt 设计**：System 限定需求分析角色、区分确认信息与假设、避免擅自补充性能指标。Human 仅提供 user_request。修复消息给出字段位置/错误类型与待修复响应，不输出验证输入到日志。

**依赖**：只读原始需求，不依赖前一 Agent。输出在图中写 requirements，Design/Coding/Review 都可读取。

**异常**：RequirementsInputError 拒绝输入；结构输出失败触发 JSON 回退，修复耗尽 RequirementsOutputError；LLM 网络/超时/认证/限流等错误传播给节点，停止工作流。

## Design Agent

**实现**：agents/design_agent.py；Prompt：agents/prompts/design.py；Schema：state/design.py；节点：design_agent_node。

**目标**：基于结构化需求设计简单、可追溯的架构，不生成实现代码，不改变需求。

**接口**：design_system(requirements: RequirementsOutput, *, user_request: str | None = None, max_repair_attempts: int = 2) -> DesignOutput。

图节点当前只传 requirements，不额外传原始需求；直接调用接口时可用 user_request 补充次要上下文。

| DesignOutput 字段 | 类型 | 含义 |
| --- | --- | --- |
| architecture_overview | str | 架构概览 |
| technology_stack | list[str] | 至少一项技术及用途 |
| modules | list[ModuleDesign] | 至少一个模块 |
| data_models | list[str] | 实体与关系文字说明 |
| interfaces | list[str] | 接口文字契约 |
| project_structure | str | 建议目录树 |
| design_decisions | list[str] | 至少一项决策和理由 |
| preserved_constraints | list[str] | 原样且按顺序保留需求约束 |

ModuleDesign 含 name、responsibility、dependencies、requirement_ids。v2 功能需求使用 requirement_details 中的持久 FR 编号，由接口原样构造编号表传给 Prompt；允许非连续编号，重排不重新分配。旧 v1 数据为兼容原 Design 引用，仍按列表位置解释 FR-1、FR-2…。

**Prompt 设计**：需求为权威依据；全部功能需要模块覆盖；内部依赖只引用已定义模块；简单项目优先简单方案，不能擅自加数据库/微服务。设计假设仍需标注。

**依赖与校验**：恢复并重新验证 RequirementsOutput；state/validation.py::validate_design_requirements 检查约束原样保留、FR 全覆盖且不含未知 ID、模块名唯一、依赖不自指且存在。没有检测依赖环，也没有证明架构文字在语义上遵守每项约束。

**异常**：输入或一致性无效为 DesignInputError；原生结构虽有效但一致性失败会带安全反馈进入修复；JSON/一致性修复耗尽为 DesignOutputError。调用错误传播。

## Coding Agent

**实现**：agents/coding_agent.py；Prompt：agents/prompts/coding.py；Schema：state/coding.py；节点：coding_agent_node。

**目标**：消费需求和设计，生成完整文件集与运行说明；不运行代码、不安装依赖。

```text
真实业务签名（调用会访问 API）：
generate_code(requirements, design, *, max_repair_attempts=2,
              review=None, previous_code=None,
              validation_report=None, repair_policy=None) -> CodingOutput
```

requirements 必须为 RequirementsOutput，design 必须为 DesignOutput。修复必须提供 previous_code，以及允许代码修复的 Review 或当前产物的失败固定验收报告；输入重新校验并检查来源绑定。v2 即使模型 passed=True，只要静态门禁为 code_revision 仍可修复；manual_required 不得绕过。环境 error/timeout/not_run 本身不触发代码修复。

| CodeFile 字段 | 类型 | 含义 |
| --- | --- | --- |
| path | str | 最长 240 字符的安全相对路径，使用正斜杠 |
| language | str | 语言，供展示高亮 |
| content | str | 保留换行/缩进的完整非空文件内容 |
| description | str | 文件用途及需求/模块关联说明 |

| CodingOutput 字段 | 类型 | 含义 |
| --- | --- | --- |
| files | list[CodeFile] | 1–20 个文件 |
| instructions | str | 环境、入口和手动验证说明 |
| dependencies | list[str] | 依赖清单，不自动安装 |
| test_suggestions | list[str] | 尚待执行的测试建议 |
| limitations | list[str] | 未实现、待确认和静态校验局限 |

**Prompt 设计**：坚持既定需求与设计，生成最少必要文件，简单 Python 项目尽量包含基本测试；禁止空占位、泄密、系统命令执行或声称测试已运行。修复时附上一版完整代码及真实审查反馈。

**增量修复协议**：`agents/incremental.py` 生成 `CodePatch` v1：`base_artifact_digest / base_context_digest / feedback_digest / changes`。每项 change 有 operation、精确 path、完整 CodeFile（delete 为 null）、reason 和 issue_ids。反馈编号使用 v2 RV、旧反馈的程序编号 LEGACY，或固定验收失败用例的 VT 编号；不允许模型创建未知问题编号。验证修复上下文附带程序注册的完整固定接口及输入/期望，不依赖模型猜测验收行为。

修复默认 `RepairPolicy.replacements_only`，仅替换已有路径；新增、删除只在调用方提供明确允许列表时开放，大小写改名仍拒绝。补丁不能修改 instructions、dependencies、test_suggestions、limitations，未声明文件按原内容与顺序保留；应用为纯内存变换，最终再次校验完整 CodingOutput。原生/JSON 格式和绑定失败共用现有有限修复与全局调用预算，不回退成全量重生成，不回传错误响应正文；截断和网络错误立即停止。首次生成仍使用原 CodingOutput Schema。

成功补丁产生 RepairReceipt，记录基线/结果/上下文/反馈/补丁摘要、变更路径与问题、保留文件数、首轮输入文本字节、规范化补丁与合并产物字节、成功补丁之前观察到的格式失败次数。图在 Coding 完成或保留产物的停止事件中保存收据；State、Result 与存档读取对照真实历史代码及反馈检查绑定和实际变更。收据不包含代码或完整 Prompt，也不声称问题已被语义修复；后续 Review 或固定验收仍必需。

**输出校验**：安全路径、空文件、大小写重名、文件与目录同路径冲突；单文件 UTF-8 最多 64 KiB，整体 CodingOutput 序列化最多 256 KiB。.py 文件使用 ast.parse 做语法检查，其他语言没有相应语法检查。语法可解析不证明运行正确或安全。

**异常**：CodingInputError 在调用前拒绝不合法输入。结构/JSON/语法/路径/大小失败可以有界修复，耗尽 CodingOutputError；截断 LLMTruncatedOutputError、网络/认证等立即传播。敏感路径不写盘，ZIP 导出前还会再检查。

## Review Agent

**实现**：agents/review_agent.py；Prompt：agents/prompts/review.py；Schema：state/review.py；节点：review_agent_node。

**目标**：静态审查真实生成代码的功能覆盖、设计一致性、明显语法/逻辑问题、说明和文件结构，反馈可修复问题。

**接口**：review_code(requirements: RequirementsOutput, design: DesignOutput, code: CodingOutput, *, max_repair_attempts: int = 2) -> ReviewOutput。

必须消费三个对象，重新校验代码和设计依赖；不是固定分数/固定通过的模拟 Agent。仅标准测试中使用 Mock。

| ReviewOutput 字段 | 类型 | 约束 |
| --- | --- | --- |
| passed | bool | 严格布尔；模型静态判断 |
| score | int | 严格整数，0–100 |
| issues | list[str] | 最多 64 项；未通过必须有问题 |
| suggestions | list[str] | 最多 64 项，可操作建议 |
| summary | str | 非空，最长 4000 字符，Prompt 要求说明静态审查局限 |

**Prompt 设计**：逐文件读实际代码；以需求、设计为依据；阻断问题应拒绝通过，给出位置和理由；禁止执行测试或把模型判断称为真实测试通过。分数仅供展示，最终静态放行由程序门禁决定。

### Review v2 与程序静态门禁

模型生成 `ReviewAssessmentV2`，字段为 `contract_version=2 / passed / score / issue_details / suggestions / summary`。每个 `ReviewIssue` 包含当轮唯一 `issue_id`、分类、严重程度、消息、文件/行范围、代码引用、FR/AC 关联、期望/静态观察和建议处理角色。缺失文件或位置未知时用 null，不接受编造的文件路径、越界行号或不在相应范围中的引用。

适配器将其转换为兼容的 `ReviewOutput`，由程序填充 `issues` 文字摘要、`artifact_digest`、`context_digest` 和 `policy_version=static-review-v1`。模型 Schema 不接收这些可信字段。SHA-256 绑定规范化的整个 CodingOutput，以及需求、设计、代码上下文；用于发现版本不匹配，不是身份签名，也不证明代码正确。

`state/review_policy.py::evaluate_review` 是唯一静态判定规则：

- 模型拒绝，或任一 `blocker/major` 问题，均不通过；分数不参与路由。
- 模型通过且只有 `minor/suggestion` 或无问题时，静态门禁通过。`verification_status` 仍为 `not_run`。
- 需处理的问题属于 requirement/design，或建议角色不是 coding 时，返回 `manual_required`，以 `completed_with_issues` 交付。持久模式可由用户明确启动关联修订尝试：设计问题返回 Design，阻断需求问题先澄清，再重新生成需求和下游产物；原审查与原运行保留。

模块 5 增加可选 `review_code(..., validation_report=report)`：在创建客户端前核对代码、需求/设计上下文与固定验收集绑定，然后将程序报告传入 Prompt。模型可引用该报告，但原 ReviewOutput 仍为静态意见，不因接收报告而变为执行器。需要联合判定时使用 `validation.review.review_validated`，保留报告与其摘要，由程序要求执行和静态门禁同时通过。普通图仍沿用原静态审查路径，不自动发起隔离验收或额外模型审查。
- 其他可修复代码问题，在预算内返回 Coding；耗尽预算后交付未解决结果。

`passed` 始终保留模型意见，不被程序覆盖。页面和报告分别显示模型意见与程序门禁，因此模型 passed=true 时，工作流也可能不通过。Coding 修复入口读取程序决定，并重新检查输入绑定，不再仅检查 passed。

**异常**：ReviewInputError、有限格式/引用修复后的 ReviewOutputError，或直接传播 LLM 错误。JSON 回退限制原始响应 32768 UTF-8 字节；新 v2 原生和回退路径都会限制绑定后的完整 ReviewOutput 字节数。网络/认证异常、过期绑定等不得作为普通业务拒绝进入修复。

每轮有效结果写 ReviewRecord(record_id, run_id, revision_count, reviewed_at, review, code)，保留当时的代码快照。State、终态 Result、历史读取会验证各轮代码及上下文绑定。旧 v1 审查沿用历史 passed，不补造严重程度、代码摘要或执行证据，原有历史可幂等读取与保存。

当前问题 ID 仅在单轮唯一；跨轮问题追踪、重复问题停止策略与真实测试门禁留待后续模块。结构与引用合法不等于模型分类、严重程度或语义判断必然正确。

## 协作依赖总结

Requirements → Design；Requirements + Design → Coding；Requirements + Design + Coding → Review；未通过的 Review + 上一版 Coding + Requirements + Design → 下一轮 Coding。

各对象在 Agent 内是 Pydantic 模型，在 State 中是经过校验的字典；节点承担转换。新增 Agent 时需要新增数据契约、Prompt、业务函数、节点及图路由，不让 Web 页面直接调用模型。
