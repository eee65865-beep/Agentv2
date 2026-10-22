"""Versioned durable sessions: native resume, explicit retry/replan attempts."""

from datetime import datetime, timezone
from uuid import uuid4

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from workflow_studio.budget import BudgetSnapshot, BudgetTracker, RunBudget, budget_scope
from workflow_studio.config import Settings, load_settings
from workflow_studio.llm.options import model_profile, thinking_mode
from workflow_studio.state.requirements import OpenQuestion, RequirementsOutput
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.review_policy import evaluate_review
from workflow_studio.state.workflow import create_initial_state, validate_state
from workflow_studio.storage.checkpoints import CheckpointConflictError, CheckpointError, CheckpointStore
from workflow_studio.storage.paths import canonical_run_id
from workflow_studio.workflows.clarification import build_durable_workflow, validate_answers
from workflow_studio.workflows.nodes import WorkflowNodeError
from workflow_studio.workflows.runner import WorkflowStreamPacket

WORKFLOW_VERSION = "durable-v1"
TERMINAL = {"completed", "completed_with_issues", "failed", "stopped"}
PROFILE_KEYS = set(Settings.model_fields) - {"deepseek_api_key"}


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    session_id: str
    active_attempt: str
    workflow_version: str = WORKFLOW_VERSION
    profile: dict
    thinking_enabled: bool | None = None
    created_at: str


class AttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    attempt_id: str
    session_id: str
    run_id: str
    parent_attempt: str | None = None
    entry_node: str = "requirements_agent"
    status: str = "ready"
    initial_state: dict
    final_state: dict | None = None
    budget: BudgetSnapshot
    review_fingerprint: str | None = None
    resume_count: int = Field(default=0, ge=0)


class DurableView(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thread_id: str
    attempt_id: str
    run_id: str
    checkpoint_id: str
    status: str
    state: dict
    attempts: list[dict]
    budget: BudgetSnapshot
    model_name: str
    thinking_enabled: bool | None

    @property
    def result(self):
        return WorkflowResult.from_state(self.state) if self.state["status"] in TERMINAL else None


def _write(store, table, record):
    if table == "sessions":
        checked = SessionRecord.model_validate(record.model_dump())
        args = (checked.session_id, checked.model_dump_json())
        query = "INSERT OR REPLACE INTO sessions VALUES (?,?)"
    else:
        checked = AttemptRecord.model_validate(record.model_dump())
        args = (checked.attempt_id, checked.session_id, checked.model_dump_json())
        query = "INSERT OR REPLACE INTO attempts VALUES (?,?,?)"
    if len(args[-1].encode("utf-8")) > 8 * 1024 * 1024:
        raise CheckpointError("session_record_too_large")
    with store.lock, store.db:
        store.db.execute(query, args)


def _records(store, session_id):
    canonical_run_id(session_id)
    with store.lock:
        row = store.db.execute("SELECT payload FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise CheckpointError("session_not_found")
        session = SessionRecord.model_validate_json(row[0])
        row = store.db.execute("SELECT payload FROM attempts WHERE id=? AND session=?", (session.active_attempt, session_id)).fetchone()
    if row is None:
        raise CheckpointError("active_attempt_missing")
    attempt = AttemptRecord.model_validate_json(row[0])
    if session.workflow_version != WORKFLOW_VERSION:
        raise CheckpointError("unsupported_workflow_version")
    if set(session.profile) != PROFILE_KEYS:
        raise CheckpointError("invalid_persisted_model_profile")
    settings = Settings.model_validate(session.profile)
    if settings.deepseek_base_url.username or settings.deepseek_base_url.password or settings.deepseek_base_url.query:
        raise CheckpointError("model_url_must_not_embed_secrets")
    return session, attempt


def _graph(store, attempt):
    return build_durable_workflow(store, review_enabled=attempt.initial_state["review_enabled"], entry_node=attempt.entry_node)


def _config(attempt):
    return {"configurable": {"thread_id": attempt.attempt_id}, "recursion_limit": 100}


def inspect_durable(store, thread_id):
    session, attempt = _records(store, thread_id)
    snapshot = _graph(store, attempt).get_state(_config(attempt))
    if not snapshot.values:
        raise CheckpointError("missing_initial_checkpoint")
    state = validate_state(attempt.final_state if attempt.final_state is not None else snapshot.values)
    with store.lock:
        records = store.db.execute("SELECT payload FROM attempts WHERE session=? ORDER BY rowid", (thread_id,)).fetchall()
    attempts = [AttemptRecord.model_validate_json(row[0]) for row in records]
    return DurableView(
        thread_id=thread_id, attempt_id=attempt.attempt_id, run_id=attempt.run_id,
        checkpoint_id=snapshot.config["configurable"]["checkpoint_id"], status=attempt.status, state=state,
        attempts=[{"attempt_id": item.attempt_id, "run_id": item.run_id, "parent_attempt": item.parent_attempt, "status": item.status} for item in attempts],
        budget=attempt.budget,
        model_name=session.profile["deepseek_model"], thinking_enabled=session.thinking_enabled,
    )


def list_durable(store, limit=100):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("invalid_session_list_limit")
    with store.lock:
        rows = store.db.execute("SELECT id FROM sessions ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    values = []
    for (identifier,) in rows:
        try:
            values.append(inspect_durable(store, identifier))
        except (CheckpointError, ValueError):
            continue
    return values


def _initialize_attempt(store, session, state, *, parent=None, entry_node="requirements_agent", ledger=None, review_fingerprint=None):
    tracker = BudgetTracker(RunBudget()) if ledger is None else BudgetTracker.restore(ledger, review_fingerprint=review_fingerprint)
    initial = validate_state({**state, "budget": tracker.snapshot().model_dump(mode="json")})
    attempt = AttemptRecord(attempt_id=str(uuid4()), session_id=session.session_id, run_id=initial["run_id"], parent_attempt=parent,
                            entry_node=entry_node, initial_state=initial, budget=tracker.snapshot(), review_fingerprint=review_fingerprint)
    # Publish the session pointer only after its initial checkpoint exists.
    _graph(store, attempt).update_state(_config(attempt), initial, as_node="__start__")
    _write(store, "attempts", attempt)
    session.active_attempt = attempt.attempt_id
    _write(store, "sessions", session)
    return attempt


def create_durable(store, user_request, *, review_enabled=False, max_revisions=1, budget=None, thinking_enabled=None):
    from workflow_studio.workflows.runner import _execution
    initial, tracker = _execution(user_request, review_enabled, max_revisions, budget, None)
    settings = load_settings()
    profile = settings.model_dump(mode="json")  # API key is excluded by Settings.
    if settings.deepseek_base_url.username or settings.deepseek_base_url.password or settings.deepseek_base_url.query:
        raise CheckpointError("model_url_must_not_embed_secrets")
    if thinking_enabled is not None and type(thinking_enabled) is not bool:
        raise ValueError("invalid_thinking_option")
    session = SessionRecord(session_id=str(uuid4()), active_attempt="", profile=profile, thinking_enabled=thinking_enabled,
                            created_at=datetime.now(timezone.utc).isoformat())
    with store.claim(session.session_id):
        _initialize_attempt(store, session, initial, ledger=tracker.snapshot())
    return inspect_durable(store, session.session_id)


def _check_expected(view, expected_checkpoint_id):
    if not isinstance(expected_checkpoint_id, str) or view.checkpoint_id != expected_checkpoint_id:
        raise CheckpointConflictError("checkpoint_changed_refresh_before_resume")


def stream_durable(store, thread_id, *, expected_checkpoint_id, answers=None, cancel_event=None):
    """Run pending tasks/Command.resume under one cross-process session lock."""
    with store.claim(thread_id):
        session, attempt = _records(store, thread_id)
        view = inspect_durable(store, thread_id)
        _check_expected(view, expected_checkpoint_id)
        if attempt.status in TERMINAL:
            raise CheckpointConflictError("terminal_attempt_requires_explicit_retry_or_new_task")
        if view.state["status"] == "waiting":
            questions = [OpenQuestion.model_validate(item) for item in view.state["awaiting_questions"]]
            validate_answers(questions, answers)
            input_value = Command(resume=answers)
        elif answers is not None:
            raise ValueError("task_is_not_waiting_for_answers")
        else:
            input_value = None
        def journal(snapshot, fingerprint):
            attempt.budget, attempt.review_fingerprint = snapshot, fingerprint
            _write(store, "attempts", attempt)
        tracker = BudgetTracker.restore(attempt.budget, cancel_event, review_fingerprint=attempt.review_fingerprint, on_change=journal)
        attempt.resume_count += 1
        attempt.status = "running"
        _write(store, "attempts", attempt)
        graph, config = _graph(store, attempt), _config(attempt)
        latest = view.state
        stream = graph.stream(input_value, config, stream_mode=["custom", "values"], durability="sync")
        try:
            while True:
                with budget_scope(tracker), model_profile(session.profile), thinking_mode(session.thinking_enabled):
                    try:
                        kind, payload = next(stream)
                    except StopIteration:
                        break
                if kind == "custom":
                    from workflow_studio.state.workflow import WorkflowEvent
                    yield WorkflowStreamPacket(kind="event", event=WorkflowEvent.model_validate(payload))
                else:
                    # Values streaming also emits LangGraph's interrupt envelope.
                    payload = {key: value for key, value in payload.items() if key != "__interrupt__"}
                    if not payload:
                        continue
                    latest = validate_state(payload)
                    yield WorkflowStreamPacket(kind="state", state=latest)
        except WorkflowNodeError as error:
            latest = error.state
            yield WorkflowStreamPacket(kind="state", state=latest)
        finally:
            with budget_scope(tracker):
                stream.close()
            attempt.budget = tracker.snapshot()
            attempt.status = latest["status"] if latest["status"] in TERMINAL | {"waiting"} else "running"
            if latest["status"] in TERMINAL:
                attempt.final_state = latest
            _write(store, "attempts", attempt)


def execute_durable(store, thread_id, *, expected_checkpoint_id, answers=None, cancel_event=None):
    for _ in stream_durable(store, thread_id, expected_checkpoint_id=expected_checkpoint_id, answers=answers, cancel_event=cancel_event):
        pass
    return inspect_durable(store, thread_id)


def fork_durable_attempt(store, thread_id, *, expected_checkpoint_id, replan=False):
    """Never rewrite a terminal run. Child run keeps upstream data and spent budget."""
    with store.claim(thread_id):
        session, previous = _records(store, thread_id)
        view = inspect_durable(store, thread_id)
        _check_expected(view, expected_checkpoint_id)
        old = view.state
        if previous.budget.stop_reason is not None:
            raise CheckpointConflictError("stopped_budget_cannot_be_reset_by_retry")
        if replan:
            if old["status"] != "completed_with_issues" or old["review"] is None:
                raise CheckpointConflictError("replan_requires_unresolved_review")
            from workflow_studio.state.review import ReviewOutput
            review = ReviewOutput.model_validate(old["review"])
            if evaluate_review(review).action != "manual_required":
                raise CheckpointConflictError("review_does_not_require_replanning")
            entry = "design_agent"
        else:
            if old["status"] != "failed":
                raise CheckpointConflictError("retry_requires_failed_attempt")
            entry = old["current_agent"]
            if entry not in {"requirements_agent", "design_agent", "coding_agent", "review_agent"}:
                pending = _graph(store, previous).get_state(_config(previous)).next
                entry = pending[0] if len(pending) == 1 else ""
            if entry not in {"requirements_agent", "design_agent", "coding_agent", "review_agent"}:
                raise CheckpointConflictError("failure_has_no_safe_retry_boundary")
        state = create_initial_state(old["user_request"], review_enabled=old["review_enabled"], max_revisions=max(0, old["max_revisions"] - old["revision_count"]))
        state["status"] = "running"
        state["clarifications"] = old["clarifications"]
        if entry != "requirements_agent":
            state["requirements"] = old["requirements"]
        elif state["clarifications"] and old["requirements"] is not None:
            state["requirements"] = old["requirements"]
        if entry in {"coding_agent", "review_agent"}:
            state["design"] = old["design"]
        if entry == "review_agent":
            state["code"] = old["code"]
        if replan:
            state["design_feedback"] = review.model_dump_json()
            if any(item.category == "requirement" for item in review.issue_details if item.severity in ("blocker", "major")):
                req = RequirementsOutput.model_validate(state["requirements"])
                identifier = max((int(item.id.split("-")[1]) for item in req.open_questions), default=0) + 1
                question = OpenQuestion(id=f"Q-{identifier}", question="请补充或纠正这些审查涉及的需求：" + "; ".join(review.issues)[:1800], blocking=True, requirement_ids=[])
                req.open_questions = [question]
                state["requirements"] = req.model_dump(mode="json")
                entry = "clarification_prepare"
        _initialize_attempt(store, session, validate_state(state), parent=previous.attempt_id, entry_node=entry, ledger=previous.budget, review_fingerprint=previous.review_fingerprint)
    return inspect_durable(store, thread_id)


async def aexecute_durable(store, thread_id, **kwargs):
    import asyncio
    return await asyncio.to_thread(execute_durable, store, thread_id, **kwargs)
