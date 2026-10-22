# Agent 设计

每个 Agent 都是独立、可 Mock 的同步业务函数，内部调用统一 get_llm。业务函数不了解 Streamlit，不自行构建 Graph，也不修改全局业务字典。

## 共同约定

- Prompt 位于 src/workflow_studio/agents/prompts/，由 ChatPromptTemplate 分离 System 与 Human 消息；Schema 格式说明来自 PydanticOutputParser.get_format_instructions。
- 优先 DeepSeekClient.invoke_structured，使用 LangChain function_calling；不兼容/结构失败时请求完整 JSON，再由 Pydantic 校验。允许移除单个外层 JSON 围栏，不用 eval，不接收不完整 JSON。
- 默认 max_repair_attempts=2，允许 0–3；最多一次原生、一次 JSON 回退和指定次数的修复，成功即停止。网络/认证等调用失败直接向上传播，不作为格式修复。
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

ModuleDesign 含 name、responsibility、dependencies、requirement_ids。功能需求按列表位置编号 FR-1、FR-2…，由接口构造编号表传给 Prompt；编号不是需求对象中的新字段。

**Prompt 设计**：需求为权威依据；全部功能需要模块覆盖；内部依赖只引用已定义模块；简单项目优先简单方案，不能擅自加数据库/微服务。设计假设仍需标注。

**依赖与校验**：恢复并重新验证 RequirementsOutput；state/validation.py::validate_design_requirements 检查约束原样保留、FR 全覆盖且不含未知 ID、模块名唯一、依赖不自指且存在。没有检测依赖环，也没有证明架构文字在语义上遵守每项约束。

**异常**：输入或一致性无效为 DesignInputError；原生结构虽有效但一致性失败会带安全反馈进入修复；JSON/一致性修复耗尽为 DesignOutputError。调用错误传播。

## Coding Agent

**实现**：agents/coding_agent.py；Prompt：agents/prompts/coding.py；Schema：state/coding.py；节点：coding_agent_node。

**目标**：消费需求和设计，生成完整文件集与运行说明；不运行代码、不安装依赖。

```text
真实业务签名（调用会访问 API）：
generate_code(requirements, design, *, max_repair_attempts=2,
              review=None, previous_code=None) -> CodingOutput
```

requirements 必须为 RequirementsOutput，design 必须为 DesignOutput；修复时 review 与 previous_code 必须成对提供，且 review.passed=False。输入会重新校验，并检查设计与需求一致性。

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

**Prompt 设计**：逐文件读实际代码；以需求、设计为依据；阻断问题应拒绝通过，给出位置和理由；禁止执行测试或把模型判断称为真实测试通过。分数是参考，不代替 passed。

**异常**：ReviewInputError、有限格式修复后的 ReviewOutputError，或直接传播 LLM 错误。JSON 回退检查响应最多 32768 UTF-8 字节；该长度检查位于回退解析分支，不宣称所有原生对象统一经过同样的总字节检查。

review.passed=False 是有效业务结果，允许有限修复；API 异常没有有效结论，必须 failed 停止。每轮有效结果写 ReviewRecord(record_id, run_id, revision_count, reviewed_at, review, code)，保留当时的代码快照。

## 协作依赖总结

Requirements → Design；Requirements + Design → Coding；Requirements + Design + Coding → Review；未通过的 Review + 上一版 Coding + Requirements + Design → 下一轮 Coding。

各对象在 Agent 内是 Pydantic 模型，在 State 中是经过校验的字典；节点承担转换。新增 Agent 时需要新增数据契约、Prompt、业务函数、节点及图路由，不让 Web 页面直接调用模型。
