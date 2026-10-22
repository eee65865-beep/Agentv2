"""Per-node context-local telemetry; no process-global business state."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from workflow_studio.state.telemetry import LLMCallRecord

_calls: ContextVar[list[LLMCallRecord] | None] = ContextVar("workflow_llm_calls", default=None)


@contextmanager
def collect_llm_calls() -> Iterator[list[LLMCallRecord]]:
    calls: list[LLMCallRecord] = []
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


def record_llm_call(call: LLMCallRecord) -> None:
    calls = _calls.get()
    if calls is not None:
        calls.append(call)
