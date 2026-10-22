"""Validated requirements-analysis output shared with future workflow nodes."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RequirementsOutput(BaseModel):
    """Separate confirmed requirements from explicitly labelled assumptions."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    project_name: NonEmptyText = Field(description="项目名称；用户未指定时使用暂定名称，并在 assumptions 标注。")
    summary: NonEmptyText = Field(description="软件目标、用户和范围的需求摘要，不包含代码或未经确认的事实。")
    functional_requirements: list[NonEmptyText] = Field(min_length=1, description="用户明确提出的可观察功能需求，每项独立描述。")
    non_functional_requirements: list[NonEmptyText] = Field(description="用户明确提出的性能、安全、可用性等质量需求；未说明时为空列表。")
    constraints: list[NonEmptyText] = Field(description="用户明确限定的技术、平台、时间、预算等条件；未说明时为空列表。")
    assumptions: list[NonEmptyText] = Field(description="尚未确认的合理假设或待确认信息，必须显式标记为假设/待确认。")
    acceptance_criteria: list[NonEmptyText] = Field(min_length=1, description="可验证的验收条件，应追溯到明确需求；新增范围须标为待确认。")
