"""Validated requirements-analysis output shared with future workflow nodes."""

from collections import Counter
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RequirementId = Annotated[str, StringConstraints(pattern=r"^FR-[1-9][0-9]*$")]
AcceptanceId = Annotated[str, StringConstraints(pattern=r"^AC-[1-9][0-9]*$")]


class FunctionalRequirement(BaseModel):
    """A persisted ID belongs to a requirement, never to its list position."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    id: RequirementId = Field(description="持久功能需求编号；分配后不得因重排而改变。")
    description: NonEmptyText = Field(description="对应 functional_requirements 中的确认功能，文字必须一致。")
    source_quotes: list[NonEmptyText] = Field(min_length=1, max_length=16, description="支持该需求的原始用户输入连续原文片段，不得杜撰或改写引文。")


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    id: AcceptanceId = Field(description="持久验收编号，不随列表重排改变。")
    description: NonEmptyText = Field(description="对应 acceptance_criteria 中的可验证验收条件，文字必须一致。")
    requirement_ids: list[RequirementId] = Field(min_length=1, description="该验收条件检查的确认功能需求编号，不得重复或引用未知编号。")


class OpenQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    id: Annotated[str, StringConstraints(pattern=r"^Q-[1-9][0-9]*$")] = Field(description="当轮唯一澄清问题编号。")
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)] = Field(description="影响范围或约束的具体问题；不要为低风险缺省值强制提问。")
    blocking: bool = Field(description="缺少回答会影响需求或设计正确性时为 true。")
    requirement_ids: list[RequirementId] = Field(max_length=64, description="受影响的已定义 FR 编号，不确定时为空。")


class ClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    question_id: str
    question: NonEmptyText
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


def effective_request(user_request: str, answers: list[ClarificationAnswer]) -> str:
    if not answers:
        return user_request
    return user_request + "\n\n用户已提交的澄清（回答优先；问题本身不是确认事实）：\n" + "\n".join(
        f"问题 {item.question_id}：{item.question}\n用户回答：{item.answer}" for item in answers
    )


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

    contract_version: Literal[1, 2] = Field(default=1, description="需求契约版本；旧记录无版本字段时按 1 读取，新生成结果必须为 2。")
    requirement_details: list[FunctionalRequirement] = Field(default_factory=list, description="v2 持久需求目录，与功能需求文字列表一一对应；v1 为空，不补造来源。")
    acceptance_details: list[AcceptanceCriterion] = Field(default_factory=list, description="v2 持久验收目录及需求关联；v1 为空，不推测历史关联。")
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=16, description="待澄清问题；没有阻断问题时为空。旧记录缺失时不补造。")

    @field_validator("contract_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("contract_version_must_be_integer")
        return value

    @model_validator(mode="after")
    def validate_traceability(self) -> Self:
        if len({item.id for item in self.open_questions}) != len(self.open_questions):
            raise ValueError("duplicate_question_id")
        known = {item["id"] for item in self.requirement_catalog()}
        if any(set(item.requirement_ids) - known or len(set(item.requirement_ids)) != len(item.requirement_ids) for item in self.open_questions):
            raise ValueError("invalid_question_requirement_reference")
        if self.contract_version == 1:
            if self.requirement_details or self.acceptance_details:
                raise ValueError("legacy_requirements_cannot_claim_traceability")
            return self
        if not self.requirement_details or not self.acceptance_details:
            raise ValueError("v2_requires_traceability")
        if Counter(item.description for item in self.requirement_details) != Counter(self.functional_requirements):
            raise ValueError("requirement_catalog_text_mismatch")
        if Counter(item.description for item in self.acceptance_details) != Counter(self.acceptance_criteria):
            raise ValueError("acceptance_catalog_text_mismatch")
        ids = {item.id for item in self.requirement_details}
        if len(ids) != len(self.requirement_details):
            raise ValueError("duplicate_requirement_id")
        if len({item.id for item in self.acceptance_details}) != len(self.acceptance_details):
            raise ValueError("duplicate_acceptance_id")
        covered: set[str] = set()
        for item in self.acceptance_details:
            refs = set(item.requirement_ids)
            if len(refs) != len(item.requirement_ids):
                raise ValueError("duplicate_acceptance_requirement_reference")
            if refs - ids:
                raise ValueError("unknown_acceptance_requirement_reference")
            covered.update(refs)
        if covered != ids:
            raise ValueError("requirements_missing_acceptance_coverage")
        return self

    def requirement_catalog(self) -> list[dict[str, str]]:
        """Legacy IDs preserve old interpretation; v2 always uses stored IDs."""
        if self.contract_version == 2:
            return [{"id": item.id, "description": item.description} for item in self.requirement_details]
        return [{"id": f"FR-{index}", "description": text} for index, text in enumerate(self.functional_requirements, 1)]


class RequirementsOutputV2(RequirementsOutput):
    """Strict generation schema; the base model remains a legacy read boundary."""

    contract_version: Literal[2] = Field(description="新生成需求契约版本，必须为整数 2。")
    requirement_details: list[FunctionalRequirement] = Field(min_length=1, description="为全部确认功能分配持久 FR 编号并引用原文，与功能需求文字一一对应。")
    acceptance_details: list[AcceptanceCriterion] = Field(min_length=1, description="为全部验收条件分配持久 AC 编号并关联 FR，必须覆盖全部确认功能。")
    open_questions: list[OpenQuestion] = Field(max_length=16, description="阻断问题与非阻断待确认项；无需澄清时必须输出空列表。")


def preserve_requirement_ids(candidate: RequirementsOutput, previous: RequirementsOutput) -> RequirementsOutput:
    """Exact unchanged descriptions keep IDs; new descriptions never reuse old IDs."""
    data = candidate.model_dump()
    old_catalog = previous.requirement_catalog()
    available: dict[str, list[str]] = {}
    for item in old_catalog:
        available.setdefault(item["description"], []).append(item["id"])
    next_id = max((int(item["id"].split("-")[1]) for item in old_catalog), default=0)
    mapping = {}
    for item in data["requirement_details"]:
        old_id = item["id"]
        if available.get(item["description"]):
            item["id"] = available[item["description"]].pop(0)
        else:
            next_id += 1
            item["id"] = f"FR-{next_id}"
        mapping[old_id] = item["id"]
    old_acceptance = {}
    for item in previous.acceptance_details:
        old_acceptance.setdefault((item.description, tuple(sorted(item.requirement_ids))), []).append(item.id)
    next_ac = max((int(item.id.split("-")[1]) for item in previous.acceptance_details), default=0)
    for item in data["acceptance_details"]:
        item["requirement_ids"] = [mapping[key] for key in item["requirement_ids"]]
        keys = old_acceptance.get((item["description"], tuple(sorted(item["requirement_ids"]))), [])
        if keys:
            item["id"] = keys.pop(0)
        else:
            next_ac += 1
            item["id"] = f"AC-{next_ac}"
    for item in data["open_questions"]:
        item["requirement_ids"] = [mapping[key] for key in item["requirement_ids"]]
    return RequirementsOutputV2.model_validate(data)


class RequirementsSourceError(ValueError):
    """Safe provenance error; never include the request or generated quotes."""


def validate_requirement_sources(output: RequirementsOutput, user_request: str | tuple[str, ...]) -> RequirementsOutput:
    sources = (user_request,) if isinstance(user_request, str) else user_request
    if any(not any(quote in source for source in sources) for item in output.requirement_details for quote in item.source_quotes):
        raise RequirementsSourceError("requirement_source_quote_not_in_request")
    return output
