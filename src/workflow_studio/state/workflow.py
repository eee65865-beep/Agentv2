"""LangGraph state channels, validated partial updates and replay-safe events.

No graph, Agent execution, checkpointer, configuration or global run state lives
here. Future nodes should validate inputs and return validate_update(...) results.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Self, TypedDict, cast
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from workflow_studio.budget import BudgetSnapshot

from workflow_studio.state.review import MAX_REVISIONS, ReviewOutput, ReviewRecord, ReviewStatus, merge_review_history, validate_review_context
from workflow_studio.state.telemetry import LLMCallRecord, TokenUsage
from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.patch import RepairReceipt, validate_repair_receipts
from workflow_studio.state.design import DesignOutput
from workflow_studio.state.requirements import ClarificationAnswer, OpenQuestion, RequirementsOutput, validate_requirement_sources
from workflow_studio.state.validation import DesignConsistencyError, validate_design_requirements

Payload = dict[str, object]
EventType = Literal["initialized", "started", "completed", "failed", "info", "stopped", "waiting"]


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    FAILED = "failed"
    STOPPED = "stopped"
    WAITING = "waiting"


class WorkflowStateError(ValueError):
    """Invalid state/update; messages never echo potentially private payloads."""


class EventConflictError(WorkflowStateError):
    """An existing event ID was reused for a different logical event."""


def _uuid_string(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("identifier_must_be_uuid_string")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("identifier_must_be_uuid_string") from None
    if str(parsed) != value:
        raise ValueError("identifier_must_be_canonical_uuid_string")
    return value


class WorkflowEvent(BaseModel):
    """Immutable event; reuse its ID for replay, use a new ID for a real retry."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    event_id: str = Field(default_factory=lambda: str(uuid4()), description="逻辑事件唯一 ID，重放同一事件必须复用。")
    run_id: str = Field(description="事件所属运行，必须与共享 State 一致。")
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="带时区的事件发生时间，默认 UTC。")
    agent_name: str = Field(min_length=1, description="workflow 或 Agent 名称。")
    event_type: EventType = Field(description="初始化、开始、完成、失败或普通信息事件。")
    message: str = Field(min_length=1, description="不含密钥或原始异常详情的事件说明。")
    status: WorkflowStatus = Field(description="事件发生时的工作流状态。")

    revision_count: int = Field(default=0, ge=0, le=MAX_REVISIONS, strict=True, description="0 表示初次生成，1 起表示代码修复轮次。")
    schema_version: Literal[1] = Field(default=1, description="事件持久化格式版本。")
    model_name: str | None = Field(default=None, description="配置或实际调用的模型名，不含客户端配置。")
    started_at: AwareDatetime | None = Field(default=None, description="节点 UTC 开始时间。")
    ended_at: AwareDatetime | None = Field(default=None, description="节点 UTC 结束时间，运行中为空。")
    elapsed_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False, description="单调时钟测量的节点耗时。")
    input_fields: tuple[str, ...] = Field(default=(), description="读取的业务 State 字段名，不记录其值。")
    output_fields: tuple[str, ...] = Field(default=(), description="实际部分更新的 State 字段名。")
    error_type: str | None = Field(default=None, description="安全异常类别，不保存原始异常正文。")
    llm_calls: tuple[LLMCallRecord, ...] = Field(default=(), description="包括格式修复调用；不包含 SDK 内部不可观察的重试细节。")
    token_usage: TokenUsage = Field(default_factory=TokenUsage, description="可完整观察到的各项 token 总数；缺失项为 null。")
    repair_receipt: RepairReceipt | None = None

    @field_validator("input_fields", "output_fields")
    @classmethod
    def safe_field_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"user_request", "requirements", "design", "code", "status", "current_agent", "events", "error", "review", "review_status", "revision_count", "review_history", "budget"}
        allowed |= {"clarifications", "awaiting_questions", "design_feedback"}
        if len(set(value)) != len(value) or any(field not in allowed for field in value):
            raise ValueError("invalid_state_field_names")
        return value

    @model_validator(mode="after")
    def valid_timing(self) -> Self:
        if self.repair_receipt is not None and (self.agent_name != "coding_agent" or self.event_type not in ("completed", "stopped") or self.revision_count < 1):
            raise ValueError("patch_receipt_requires_committed_coding_revision")
        if self.ended_at is not None and (self.started_at is None or self.ended_at < self.started_at):
            raise ValueError("invalid_event_time_range")
        if self.event_type == "started" and self.ended_at is not None:
            raise ValueError("started_event_cannot_have_end_time")
        return self

    @field_validator("event_id", "run_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return _uuid_string(value)

    @field_validator("agent_name", "message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event_text_must_not_be_blank")
        return value


def merge_events(existing: list[WorkflowEvent], incoming: list[WorkflowEvent]) -> list[WorkflowEvent]:
    """Append without mutating either list; first timestamp wins on replay.

    Same ID and same logical payload deduplicate even if replay regenerated a
    timestamp. Same ID with different payload is an error, never an overwrite.
    Commit order is retained; timestamps are not used as identity or sort keys.
    """
    result: list[WorkflowEvent] = []
    by_id: dict[str, WorkflowEvent] = {}
    run_id: str | None = None
    try:
        for source in (existing, incoming):
            if not isinstance(source, list):
                raise WorkflowStateError("events must be a list.")
            for item in source:
                data = item.model_dump() if isinstance(item, WorkflowEvent) else item
                event = WorkflowEvent.model_validate(data)
                if run_id is not None and event.run_id != run_id:
                    raise WorkflowStateError("Cannot merge events from different runs.")
                run_id = event.run_id
                previous = by_id.get(event.event_id)
                if previous is not None:
                    if previous.model_dump(exclude={"timestamp"}) != event.model_dump(exclude={"timestamp"}):
                        raise EventConflictError("Event ID conflicts with an existing logical event.")
                    continue
                by_id[event.event_id] = event
                result.append(event)
    except ValidationError:
        raise WorkflowStateError("Invalid event schema.") from None
    return result


def _replace_payload(value: Payload | None, schema: type[BaseModel]) -> Payload | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WorkflowStateError("Agent output must be a schema-valid dictionary or None.")
    try:
        return schema.model_validate(value).model_dump(mode="json")
    except ValidationError:
        raise WorkflowStateError("Invalid Agent output schema.") from None


def _requirements_channel(previous: Payload | None, incoming: Payload | None) -> Payload | None:
    return _replace_payload(incoming, RequirementsOutput)


def _design_channel(previous: Payload | None, incoming: Payload | None) -> Payload | None:
    return _replace_payload(incoming, DesignOutput)


def _code_channel(previous: Payload | None, incoming: Payload | None) -> Payload | None:
    return _replace_payload(incoming, CodingOutput)


def _review_channel(previous: Payload | None, incoming: Payload | None) -> Payload | None:
    return _replace_payload(incoming, ReviewOutput)


def _revision_channel(previous: int, incoming: int) -> int:
    if type(incoming) is not int or not 0 <= incoming <= MAX_REVISIONS or incoming not in (previous, previous + 1):
        raise WorkflowStateError("Revision count must remain unchanged or increase by one.")
    return incoming


def _run_id_channel(previous: str, incoming: str) -> str:
    try:
        _uuid_string(incoming)
    except ValueError:
        raise WorkflowStateError("run_id must be a canonical UUID string.") from None
    if previous and previous != incoming:
        raise WorkflowStateError("run_id cannot change during a run.")
    return incoming


def _request_channel(previous: str, incoming: str) -> str:
    if not isinstance(incoming, str) or not incoming.strip():
        raise WorkflowStateError("user_request must be a nonblank string.")
    if previous and previous != incoming:
        raise WorkflowStateError("user_request cannot change during a run.")
    return incoming


def _status_channel(previous: WorkflowStatus | str, incoming: WorkflowStatus | str) -> WorkflowStatus:
    try:
        target = WorkflowStatus(incoming)
        current = WorkflowStatus(previous)
    except (ValueError, TypeError):
        raise WorkflowStateError("Unknown workflow status.") from None
    transitions = {
        WorkflowStatus.PENDING: {WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.FAILED, WorkflowStatus.STOPPED},
        WorkflowStatus.RUNNING: {WorkflowStatus.RUNNING, WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES, WorkflowStatus.FAILED, WorkflowStatus.STOPPED, WorkflowStatus.WAITING},
        WorkflowStatus.WAITING: {WorkflowStatus.WAITING, WorkflowStatus.RUNNING, WorkflowStatus.FAILED, WorkflowStatus.STOPPED},
        WorkflowStatus.COMPLETED: {WorkflowStatus.COMPLETED},
        WorkflowStatus.COMPLETED_WITH_ISSUES: {WorkflowStatus.COMPLETED_WITH_ISSUES},
        WorkflowStatus.FAILED: {WorkflowStatus.FAILED},
        WorkflowStatus.STOPPED: {WorkflowStatus.STOPPED},
    }
    if target not in transitions[current]:
        raise WorkflowStateError("Invalid workflow status transition.")
    return target


class WorkflowState(TypedDict):
    run_id: Annotated[str, _run_id_channel]
    user_request: Annotated[str, _request_channel]
    requirements: Annotated[Payload | None, _requirements_channel]
    design: Annotated[Payload | None, _design_channel]
    code: Annotated[Payload | None, _code_channel]
    status: Annotated[WorkflowStatus, _status_channel]
    current_agent: str
    error: str | None
    events: Annotated[list[WorkflowEvent], merge_events]
    review: Annotated[Payload | None, _review_channel]
    review_enabled: bool
    revision_count: Annotated[int, _revision_channel]
    max_revisions: int
    review_status: ReviewStatus
    review_history: Annotated[list[ReviewRecord], merge_review_history]
    budget: Payload | None
    clarifications: list[Payload]
    awaiting_questions: list[Payload]
    design_feedback: str | None


class WorkflowUpdate(TypedDict, total=False):
    """Nodes return only changed fields, never the immutable run identity."""

    requirements: Payload | None
    design: Payload | None
    code: Payload | None
    status: WorkflowStatus
    current_agent: str
    error: str | None
    events: list[WorkflowEvent]
    review: Payload | None
    revision_count: int
    review_status: ReviewStatus
    review_history: list[ReviewRecord]
    budget: Payload | None
    clarifications: list[Payload]
    awaiting_questions: list[Payload]
    design_feedback: str | None


class _ValidatedState(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: str
    user_request: str
    requirements: Payload | None
    design: Payload | None
    code: Payload | None
    status: WorkflowStatus
    current_agent: str
    error: str | None
    events: list[WorkflowEvent]
    budget: BudgetSnapshot | None = None
    clarifications: list[ClarificationAnswer] = Field(default_factory=list, max_length=48)
    awaiting_questions: list[OpenQuestion] = Field(default_factory=list, max_length=16)
    design_feedback: str | None = Field(default=None, max_length=32768)
    review: ReviewOutput | None = None
    review_enabled: bool = Field(default=False, strict=True)
    revision_count: int = Field(default=0, ge=0, le=MAX_REVISIONS, strict=True)
    max_revisions: int = Field(default=1, ge=0, le=MAX_REVISIONS, strict=True)
    review_status: ReviewStatus = ReviewStatus.DISABLED
    review_history: list[ReviewRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        _run_id_channel("", self.run_id)
        _request_channel("", self.user_request)
        if (self.status == WorkflowStatus.STOPPED) != (self.budget is not None and self.budget.stop_reason is not None):
            raise WorkflowStateError("Stopped state requires an explicit budget stop reason.")
        if self.current_agent and not self.current_agent.strip():
            raise WorkflowStateError("current_agent cannot be whitespace only.")
        self.requirements = _requirements_channel(None, self.requirements)
        if self.requirements is not None:
            validate_requirement_sources(RequirementsOutput.model_validate(self.requirements), (self.user_request, *(item.answer for item in self.clarifications)))
        if self.status == WorkflowStatus.WAITING and not self.awaiting_questions:
            raise WorkflowStateError("Waiting state requires clarification questions.")
        self.design = _design_channel(None, self.design)
        self.code = _code_channel(None, self.code)
        if self.design is not None:
            if self.requirements is None:
                raise WorkflowStateError("Design requires validated requirements.")
            validate_design_requirements(DesignOutput.model_validate(self.design), RequirementsOutput.model_validate(self.requirements))
        if self.code is not None and (self.requirements is None or self.design is None):
            raise WorkflowStateError("Code requires validated requirements and design.")
        self.events = merge_events([], self.events)
        if any(event.run_id != self.run_id for event in self.events):
            raise WorkflowStateError("Event belongs to another run.")
        if self.status == WorkflowStatus.FAILED:
            if self.error is None or not self.error.strip():
                raise WorkflowStateError("Failed state requires a nonblank error.")
        elif self.error is not None:
            raise WorkflowStateError("Only failed state may contain an error.")
        if self.status in (WorkflowStatus.COMPLETED, WorkflowStatus.COMPLETED_WITH_ISSUES) and any(value is None for value in (self.requirements, self.design, self.code)):
            raise WorkflowStateError("Completed state requires all three Agent outputs.")
        self.review_history = merge_review_history([], self.review_history)
        validate_review_context(
            self.run_id, self.status, self.review_enabled, self.revision_count, self.max_revisions,
            self.review_status, self.review, self.review_history, CodingOutput.model_validate(self.code) if self.code else None,
            RequirementsOutput.model_validate(self.requirements) if self.requirements is not None else None,
            DesignOutput.model_validate(self.design) if self.design is not None else None,
        )
        validate_repair_receipts(self.events, self.code, self.review_history, self.revision_count, self.requirements, self.design)
        return self


def validate_state(state: Mapping[str, object]) -> WorkflowState:
    """Validate and copy a full state, including cross-channel invariants.

    TypedDict alone does not validate at runtime. Reducers guard individual
    updates; this boundary enforces invariants spanning several channels.
    """
    try:
        model = _ValidatedState.model_validate(dict(state))
    except (ValidationError, DesignConsistencyError):
        raise WorkflowStateError("Invalid workflow state or inconsistent Agent outputs.") from None
    data = model.model_dump(exclude={"events", "review_history"})
    data["events"] = list(model.events)
    data["review_history"] = list(model.review_history)
    return cast(WorkflowState, data)


def create_initial_state(user_request: str, *, review_enabled: bool = False, max_revisions: int = 1) -> WorkflowState:
    """Create fresh identity/containers per invocation without reading config."""
    return validate_state({
        "run_id": str(uuid4()), "user_request": user_request,
        "requirements": None, "design": None, "code": None,
        "status": WorkflowStatus.PENDING, "current_agent": "", "error": None, "events": [],
        "review_enabled": review_enabled, "max_revisions": max_revisions,
        "review_status": ReviewStatus.PENDING if review_enabled else ReviewStatus.DISABLED,
    })


def validate_update(state: WorkflowState, update: Mapping[str, object]) -> WorkflowUpdate:
    """Return a copied partial update, applying the same reducers for validation.

    The merged snapshot is checked but NOT returned: LangGraph owns the actual
    merge. In particular, returning full merged events would replay old history.
    """
    allowed = set(WorkflowUpdate.__annotations__)
    if set(update) - allowed:
        raise WorkflowStateError("Unknown or immutable state fields in update.")
    current = validate_state(state)
    if "budget" in update and current["budget"] is not None:
        if update["budget"] is None:
            raise WorkflowStateError("Run budget cannot be removed.")
        try:
            incoming = BudgetSnapshot.model_validate(update["budget"]).model_dump(mode="json")
        except ValidationError:
            raise WorkflowStateError("Invalid run budget snapshot.") from None
        if incoming["limits"] != current["budget"]["limits"]:
            raise WorkflowStateError("Run budget limits cannot change during execution.")
        counters = ("logical_calls", "http_attempts", "format_repairs", "node_executions", "known_tokens", "unknown_usage_calls", "elapsed_seconds")
        if any(incoming[field] < current["budget"][field] for field in counters):
            raise WorkflowStateError("Run budget counters cannot decrease.")
    reducers = {
        "requirements": _requirements_channel, "design": _design_channel,
        "code": _code_channel, "status": _status_channel, "events": merge_events,
        "review": _review_channel, "revision_count": _revision_channel, "review_history": merge_review_history,
    }
    merged: dict[str, object] = dict(current)
    for key, value in update.items():
        reducer = reducers.get(key)
        merged[key] = reducer(current[key], value) if reducer else value
    validated = validate_state(merged)
    result = {key: validated[key] for key in update if key not in ("events", "review_history")}
    if "events" in update:
        # Validate/deduplicate ONLY the supplied batch, not accumulated history.
        result["events"] = merge_events([], update["events"])
    if "review_history" in update:
        result["review_history"] = merge_review_history([], update["review_history"])
    return cast(WorkflowUpdate, result)


def failure_update(state: WorkflowState, agent_name: str, error: Exception, *, event: WorkflowEvent | None = None) -> WorkflowUpdate:
    """Make a failed partial update without copying the raw exception message."""
    safe_message = f"Agent execution failed ({type(error).__name__})."
    if event is None:
        fields = ("status", "current_agent", "error", "events")
        if agent_name == "review_agent" and state["review_enabled"]:
            fields += ("review_status",)
        event = WorkflowEvent(run_id=state["run_id"], agent_name=agent_name, event_type="failed", message=safe_message, status=WorkflowStatus.FAILED, error_type=type(error).__name__, output_fields=fields, revision_count=state["revision_count"])
    if event.run_id != state["run_id"] or event.agent_name != agent_name or event.event_type != "failed" or event.status != WorkflowStatus.FAILED:
        raise WorkflowStateError("Invalid failure event.")
    update: dict[str, object] = {
        "status": WorkflowStatus.FAILED, "current_agent": agent_name,
        "error": safe_message, "events": [event],
    }
    if agent_name == "review_agent" and state["review_enabled"]:
        update["review_status"] = ReviewStatus.ERROR
    return validate_update(state, update)
