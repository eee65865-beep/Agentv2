"""Context-local model options inherited by LangGraph executor tasks."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

ThinkingMode = Literal["enabled", "disabled"]
_thinking_mode: ContextVar[ThinkingMode | None] = ContextVar("workflow_thinking_mode", default=None)


def current_thinking_mode() -> ThinkingMode | None:
    return _thinking_mode.get()


@contextmanager
def thinking_mode(enabled: bool | None) -> Iterator[None]:
    """Override one execution only; None retains .env, including after failure."""
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("thinking_enabled must be a bool or None")
    mode: ThinkingMode | None = None if enabled is None else "enabled" if enabled else "disabled"
    token = _thinking_mode.set(mode)
    try:
        yield
    finally:
        _thinking_mode.reset(token)
