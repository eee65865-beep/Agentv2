"""Design schemas independent of Agent prompts and LLM clients."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DesignText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RequirementId = Annotated[str, StringConstraints(pattern=r"^FR-[1-9][0-9]*$")]


class ModuleDesign(BaseModel):
    """One module and its explicit links to confirmed functional requirements."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    name: DesignText = Field(description="唯一模块名称，与项目目录及模块依赖名称保持一致。")
    responsibility: DesignText = Field(description="模块的明确职责，说明如何支持对应功能需求；不输出实现代码。")
    dependencies: list[DesignText] = Field(description="依赖的内部模块名称；外部技术放入 technology_stack，无依赖时为空列表。")
    requirement_ids: list[RequirementId] = Field(description="对应输入功能需求的编号，例如 FR-1；纯基础模块可为空，但所有功能需求必须被覆盖。")


class DesignOutput(BaseModel):
    """Validated architecture with constraint preservation and traceability."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    architecture_overview: DesignText = Field(description="系统架构概述，采用满足已确认需求的最简单合理方案，不生成代码。")
    technology_stack: list[DesignText] = Field(min_length=1, description="技术选型及用途，遵守用户约束；未确认技术属于设计建议，不是假定的用户要求。")
    modules: list[ModuleDesign] = Field(min_length=1, description="模块职责、内部依赖和功能需求追溯关系。")
    data_models: list[DesignText] = Field(description="数据实体、关键属性和关系的文字设计；不生成实现代码，无持久化需要时可为空。")
    interfaces: list[DesignText] = Field(description="模块间或用户交互接口的文字契约；简单本地项目无需引入 HTTP 服务。")
    project_structure: DesignText = Field(description="建议的项目文件目录树，与模块设计一致，不包含源代码。")
    design_decisions: list[DesignText] = Field(min_length=1, description="技术与架构决策及理由，说明如何满足限制；假设必须继续标为待确认。")
    preserved_constraints: list[str] = Field(description="逐项原样复制 RequirementsOutput.constraints，保持顺序，不得删减、替换或添加约束。")
