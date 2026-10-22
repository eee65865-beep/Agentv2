"""Telemetry isolation, validation and actual shared-client instrumentation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from workflow_studio.config import Settings
from workflow_studio.llm.deepseek_client import get_llm
from workflow_studio.llm.errors import LLMCallError, LLMStructuredOutputError
from workflow_studio.llm.telemetry import collect_llm_calls, record_llm_call
from workflow_studio.state.telemetry import LLMCallRecord, TokenUsage, aggregate_usage
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.workflow import WorkflowEvent, merge_events, EventConflictError


def record(model: str = "test-model", usage: TokenUsage | None = None) -> LLMCallRecord:
    return LLMCallRecord(model_name=model, elapsed_seconds=0.1, status="success", usage=usage or TokenUsage())


def test_missing_usage_is_not_zero_or_estimated() -> None:
    complete = record(usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6))
    assert aggregate_usage(()).total_tokens is None
    assert aggregate_usage((complete, complete)).total_tokens == 12
    partial = record(usage=TokenUsage(input_tokens=1))
    total = aggregate_usage((complete, partial))
    assert total.input_tokens == 5 and total.output_tokens is None and total.total_tokens is None
    zero = record(usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0))
    assert aggregate_usage((zero,)).total_tokens == 0


@pytest.mark.parametrize("value", [-1, True, "7", 1.2])
def test_usage_rejects_invalid_counts(value: object) -> None:
    with pytest.raises(ValidationError):
        TokenUsage(total_tokens=value)


def test_context_restores_after_exception_and_nested_scope() -> None:
    with collect_llm_calls() as outer:
        record_llm_call(record("outer"))
        with pytest.raises(RuntimeError):
            with collect_llm_calls() as inner:
                record_llm_call(record("inner"))
                raise RuntimeError("no leak")
        record_llm_call(record("outer-again"))
    record_llm_call(record("outside"))
    assert [call.model_name for call in outer] == ["outer", "outer-again"]
    assert [call.model_name for call in inner] == ["inner"]
    with collect_llm_calls() as fresh:
        assert fresh == []


def test_concurrent_contexts_do_not_share_usage() -> None:
    barrier = Barrier(2)
    def worker(name: str) -> tuple[LLMCallRecord, ...]:
        with collect_llm_calls() as calls:
            barrier.wait(timeout=5)
            record_llm_call(record(name))
            return tuple(calls)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "first")
        second = pool.submit(worker, "second")
        assert [call.model_name for call in first.result(timeout=10)] == ["first"]
        assert [call.model_name for call in second.result(timeout=10)] == ["second"]


def test_failed_and_structured_calls_are_recorded_without_private_data() -> None:
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as sdk:
        llm = get_llm(Settings(deepseek_api_key="private-key"))
        sdk.return_value.invoke.side_effect = RuntimeError("private-exception")
        with collect_llm_calls() as calls:
            with pytest.raises(LLMCallError):
                llm.invoke("private-prompt")
        assert len(calls) == 1 and calls[0].status == "LLMCallError"
        assert calls[0].usage.total_tokens is None
        assert "private" not in calls[0].model_dump_json()
        sdk.return_value.with_structured_output.return_value.invoke.return_value = {
            "raw": AIMessage(content="private-response", usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}),
            "parsed": None, "parsing_error": ValueError("private-parse-error"),
        }
        with collect_llm_calls() as structured:
            with pytest.raises(LLMStructuredOutputError):
                llm.invoke_structured("private-prompt", RequirementsOutput)
        assert structured[0].usage.total_tokens == 7
        assert structured[0].status == "LLMStructuredOutputError"
        assert "private" not in structured[0].model_dump_json()


def test_response_metadata_usage_is_allowlisted() -> None:
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as sdk:
        sdk.return_value.invoke.return_value = AIMessage(content="private-response", response_metadata={"token_usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3, "api_key": "private-key"}})
        with collect_llm_calls() as calls:
            get_llm(Settings(deepseek_api_key="private-key")).invoke("private-request")
        assert calls[0].usage == TokenUsage(input_tokens=2, output_tokens=1, total_tokens=3)
        assert "private" not in calls[0].model_dump_json()


def test_event_metadata_is_immutable_and_replay_safe() -> None:
    event = WorkflowEvent(run_id=str(uuid4()), agent_name="requirements_agent", event_type="completed", message="Completed", status="running", llm_calls=(record(),), input_fields=("user_request",), output_fields=("requirements",))
    assert len(merge_events([event], [WorkflowEvent.model_validate_json(event.model_dump_json())])) == 1
    with pytest.raises(ValidationError):
        event.llm_calls[0].usage.total_tokens = 9
    changed = event.model_copy(update={"elapsed_seconds": 1.0})
    with pytest.raises(EventConflictError):
        merge_events([event], [changed])
    with pytest.raises(ValidationError):
        WorkflowEvent.model_validate({**event.model_dump(), "input_fields": ["api_key"]})
