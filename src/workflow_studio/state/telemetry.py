"""Serializable, allowlisted model-call metadata without prompts or credentials."""

from pydantic import BaseModel, ConfigDict, Field


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class LLMCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    model_name: str = Field(min_length=1)
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    status: str = Field(min_length=1, description="success 或脱敏后的异常类型。")
    usage: TokenUsage = Field(default_factory=TokenUsage)


def aggregate_usage(calls: tuple[LLMCallRecord, ...]) -> TokenUsage:
    """Sum only fully reported counts; never treat missing usage as zero."""
    counts: dict[str, int | None] = {}
    for field in TokenUsage.model_fields:
        values = [getattr(call.usage, field) for call in calls]
        counts[field] = sum(values) if values and all(value is not None for value in values) else None
    return TokenUsage.model_validate(counts)
