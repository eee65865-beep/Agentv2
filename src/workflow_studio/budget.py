"""Per-run admission budgets and cooperative stopping, without credentials."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import Event, RLock
from time import monotonic
from collections.abc import Callable, Iterator
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Count = Annotated[int, Field(ge=0, strict=True)]
StopReason = Literal["cancelled", "deadline", "logical_calls", "http_attempts", "format_repairs", "node_executions", "tokens", "repeated_review", "clarifications"]
STOP_LABELS = {
    "cancelled": "已请求停止", "deadline": "总时限已到", "logical_calls": "模型逻辑调用预算不足",
    "http_attempts": "HTTP 请求次数预算不足", "format_repairs": "格式修复预算不足",
    "node_executions": "节点执行预算不足", "tokens": "Token 核算预算不足", "repeated_review": "连续相同审查问题已达到停止阈值",
    "clarifications": "澄清轮数已达到上限",
}


class RunBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    max_logical_calls: Count = 40
    max_http_attempts: Count = 120
    max_format_repairs: Count = 20
    max_node_executions: Count = 10
    max_elapsed_seconds: float = Field(default=1800, gt=0, allow_inf_nan=False)
    max_total_tokens: Count | None = None
    tokens_reserved_per_call: int = Field(default=32768, gt=0, strict=True)
    max_repeated_reviews: int | None = Field(default=None, ge=2, strict=True)
    max_clarification_rounds: Count = 3

    @field_validator("max_elapsed_seconds", mode="before")
    @classmethod
    def numeric_deadline(cls, value: object) -> object:
        if type(value) not in (int, float):
            raise ValueError("deadline_must_be_number")
        return value


class BudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    limits: RunBudget
    logical_calls: Count = 0
    http_attempts: Count = 0
    format_repairs: Count = 0
    node_executions: Count = 0
    known_tokens: Count = 0
    unknown_usage_calls: Count = 0
    unknown_tokens_reserved: Count = 0
    in_flight_calls: Count = 0
    repeated_review_count: Count = 0
    clarification_rounds: Count = 0
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    stop_reason: StopReason | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.logical_calls > self.limits.max_logical_calls or self.http_attempts > self.limits.max_http_attempts:
            raise ValueError("budget_call_counter_exceeds_limit")
        if self.format_repairs > self.limits.max_format_repairs or self.format_repairs > self.logical_calls:
            raise ValueError("invalid_format_repair_counter")
        if self.node_executions > self.limits.max_node_executions:
            raise ValueError("budget_node_counter_exceeds_limit")
        if self.clarification_rounds > self.limits.max_clarification_rounds:
            raise ValueError("clarification_budget_exceeded")
        if self.unknown_usage_calls + self.in_flight_calls > self.logical_calls:
            raise ValueError("invalid_unknown_usage_counter")
        if self.unknown_tokens_reserved != self.unknown_usage_calls * self.limits.tokens_reserved_per_call:
            raise ValueError("invalid_unknown_token_reservation")
        return self


class RunStopped(Exception):
    def __init__(self, reason: StopReason):
        self.reason = reason
        super().__init__(reason)


class BudgetPersistenceError(RuntimeError):
    """Budget journal failure must escape model/Agent business-error handling."""


@dataclass
class _Call:
    tracker: "BudgetTracker"
    total_tokens: int | None = None


class BudgetTracker:
    def __init__(self, limits: RunBudget, cancel: Event | None = None, *, clock: Callable[[], float] | None = None, on_change=None):
        self.limits = RunBudget.model_validate(limits.model_dump())
        self.cancel = cancel if cancel is not None else Event()
        self.clock = clock if clock is not None else monotonic
        self.started = self.clock()
        self.lock = RLock()
        self.logical_calls = self.http_attempts = self.format_repairs = self.node_executions = 0
        self.known_tokens = self.unknown_usage_calls = self.in_flight_calls = 0
        self.repeated_review_count = 0
        self.review_fingerprint = None
        self.clarification_rounds = 0
        self.stop_reason: StopReason | None = None
        self.on_change = on_change

    @classmethod
    def restore(cls, snapshot, cancel=None, *, review_fingerprint=None, on_change=None):
        saved = BudgetSnapshot.model_validate(snapshot)
        tracker = cls(saved.limits, cancel, on_change=on_change)
        tracker.started -= saved.elapsed_seconds  # Waiting/offline time is excluded.
        for name in ("logical_calls", "http_attempts", "format_repairs", "node_executions", "known_tokens", "unknown_usage_calls", "repeated_review_count", "clarification_rounds"):
            setattr(tracker, name, getattr(saved, name))
        tracker.unknown_usage_calls += saved.in_flight_calls  # Crash: never assume the request was free.
        tracker.review_fingerprint = review_fingerprint
        tracker.stop_reason = saved.stop_reason
        tracker._notify()
        return tracker

    def _notify(self):
        if self.on_change is not None:
            try:
                self.on_change(self.snapshot(), self.review_fingerprint)
            except Exception as error:
                raise BudgetPersistenceError("budget_journal_write_failed") from error

    def _stop(self, reason: StopReason) -> None:
        self.stop_reason = self.stop_reason or reason
        self._notify()
        raise RunStopped(self.stop_reason)

    def check(self) -> None:
        with self.lock:
            if self.stop_reason is not None:
                raise RunStopped(self.stop_reason)
            if self.cancel.is_set():
                self._stop("cancelled")
            if self.clock() - self.started >= self.limits.max_elapsed_seconds:
                self._stop("deadline")
            if self.limits.max_total_tokens is not None and self.accounted_tokens() > self.limits.max_total_tokens:
                self._stop("tokens")

    def accounted_tokens(self) -> int:
        return self.known_tokens + (self.unknown_usage_calls + self.in_flight_calls) * self.limits.tokens_reserved_per_call

    def reserve_node(self) -> None:
        with self.lock:
            self.check()
            if self.node_executions >= self.limits.max_node_executions:
                self._stop("node_executions")
            self.node_executions += 1
            self._notify()

    def reserve_call(self, repair: bool = False) -> _Call:
        with self.lock:
            self.check()
            if self.logical_calls >= self.limits.max_logical_calls:
                self._stop("logical_calls")
            if repair and self.format_repairs >= self.limits.max_format_repairs:
                self._stop("format_repairs")
            if self.limits.max_total_tokens is not None and self.accounted_tokens() + self.limits.tokens_reserved_per_call > self.limits.max_total_tokens:
                self._stop("tokens")
            self.logical_calls += 1
            self.format_repairs += int(repair)
            self.in_flight_calls += 1
            self._notify()
            return _Call(self)

    def settle(self, call: _Call) -> None:
        with self.lock:
            self.in_flight_calls -= 1
            if call.total_tokens is None:
                self.unknown_usage_calls += 1
            else:
                self.known_tokens += call.total_tokens
            self._notify()

    def reserve_http(self) -> float:
        with self.lock:
            self.check()
            if self.http_attempts >= self.limits.max_http_attempts:
                self._stop("http_attempts")
            self.http_attempts += 1
            self._notify()
            return max(0.001, self.limits.max_elapsed_seconds - (self.clock() - self.started))

    def observe_review(self, fingerprint: str | None) -> None:
        with self.lock:
            self.repeated_review_count = self.repeated_review_count + 1 if fingerprint and fingerprint == self.review_fingerprint else (1 if fingerprint else 0)
            self.review_fingerprint = fingerprint
            self._notify()
            if self.limits.max_repeated_reviews is not None and self.repeated_review_count >= self.limits.max_repeated_reviews:
                self._stop("repeated_review")

    def consume_clarification(self):
        with self.lock:
            self.check()
            if self.clarification_rounds >= self.limits.max_clarification_rounds:
                self._stop("clarifications")
            self.clarification_rounds += 1
            self._notify()

    def snapshot(self) -> BudgetSnapshot:
        with self.lock:
            return BudgetSnapshot(
                limits=self.limits, logical_calls=self.logical_calls, http_attempts=self.http_attempts,
                format_repairs=self.format_repairs, node_executions=self.node_executions,
                known_tokens=self.known_tokens, unknown_usage_calls=self.unknown_usage_calls,
                unknown_tokens_reserved=self.unknown_usage_calls * self.limits.tokens_reserved_per_call,
                in_flight_calls=self.in_flight_calls, elapsed_seconds=max(0, self.clock() - self.started), stop_reason=self.stop_reason,
                repeated_review_count=self.repeated_review_count,
                clarification_rounds=self.clarification_rounds,
            )


_tracker: ContextVar[BudgetTracker | None] = ContextVar("workflow_budget", default=None)
_call: ContextVar[_Call | None] = ContextVar("workflow_budget_call", default=None)


def current_budget() -> BudgetTracker | None:
    return _tracker.get()


@contextmanager
def budget_scope(tracker: BudgetTracker) -> Iterator[BudgetTracker]:
    token = _tracker.set(tracker)
    try:
        yield tracker
    finally:
        _tracker.reset(token)


@contextmanager
def model_call(*, repair: bool = False) -> Iterator[None]:
    tracker = current_budget()
    if tracker is None or (_call.get() is not None and _call.get().tracker is tracker):
        yield
        return
    call = tracker.reserve_call(repair)
    token = _call.set(call)
    try:
        yield
    finally:
        try:
            tracker.settle(call)
        finally:
            _call.reset(token)


def report_token_usage(total: int | None) -> None:
    call = _call.get()
    if call is not None and type(total) is int and total >= 0:
        call.total_tokens = total


def budgeted_call(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with model_call():
            return method(*args, **kwargs)
    return wrapped


def invoke_model(client, prompt, *, schema=None, repair=False):
    """Also covers alternate clients; nested shared clients do not double count."""
    with model_call(repair=repair):
        result = client.invoke(prompt) if schema is None else client.invoke_structured(prompt, schema)
        usage = getattr(result, "usage_metadata", None)
        if isinstance(usage, dict):
            report_token_usage(usage.get("total_tokens"))
        return result
