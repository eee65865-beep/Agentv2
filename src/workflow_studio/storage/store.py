"""Schema-checked UTF-8 snapshots, staged and published as complete directories."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from workflow_studio.diagnostics import log_failure
from workflow_studio.errors import CorruptRunError, InvalidResultError, RunConflictError, RunNotFoundError, StorageError, StorageReadError, StorageWriteError, UnsafeStoragePathError
from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.tracing import summarize_run
from workflow_studio.state.workflow import WorkflowEvent
from workflow_studio.storage.models import MAX_JSON_BYTES, MAX_RUN_BYTES, HistoryProblem, ReviewArchive, RunListing, RunMetadata, SavedRun
from workflow_studio.storage.paths import APPLICATION_ROOT, canonical_run_id, guard_no_links, safe_child, safe_root


def _encode(value: object) -> bytes:
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise InvalidResultError("json_file_size_limit")
    return data


def _unique_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid_json_constant")


def _decode(data: bytes) -> object:
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)


def _validated_result(result: WorkflowResult, request: str) -> WorkflowResult:
    if not isinstance(result, WorkflowResult):
        raise InvalidResultError("expected_workflow_result")
    canonical_run_id(result.run_id)
    # State boundary also revalidates nested mutable models and deduplicates events.
    state = {**result.model_dump(), "user_request": request, "current_agent": result.events[-1].agent_name if result.events else ""}
    checked = WorkflowResult.from_state(state)
    if result.events and (len({item.event_id for item in result.events}) != len(result.events) or result.events[-1].status != result.status):
        raise InvalidResultError("event_terminal_mismatch")
    return checked


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        try:
            self.root = safe_root(root if root is not None else APPLICATION_ROOT / "outputs")
        except OSError as error:
            log_failure("storage_root", error, agent="storage")
            raise StorageReadError("cannot_access_storage_root") from None

    def _read(self, folder: Path, name: str) -> bytes:
        target = safe_child(folder, name)
        with target.open("rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
                raise CorruptRunError("invalid_snapshot_file_size_or_type")
            data = file.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise CorruptRunError("snapshot_too_large")
        return data

    def _load_folder(self, folder: Path, run_id: str) -> SavedRun:
        metadata = RunMetadata.model_validate(_decode(self._read(folder, "metadata.json")))
        if metadata.run_id != run_id:
            raise CorruptRunError("run_id_mismatch")
        bodies: dict[str, object] = {}
        total = (folder / "metadata.json").stat().st_size
        for name, digest in metadata.sha256.items():
            data = self._read(folder, name)
            total += len(data)
            if total > MAX_RUN_BYTES or sha256(data).hexdigest() != digest:
                raise CorruptRunError("snapshot_integrity_or_size_error")
            bodies[name] = _decode(data)
        events = TypeAdapter(list[WorkflowEvent]).validate_python(bodies["events.json"])
        if len(events) > 512:
            raise CorruptRunError("too_many_events")
        archive = ReviewArchive.model_validate(bodies["review.json"]) if metadata.review_enabled else None
        result = WorkflowResult.model_validate({
            "run_id": run_id, "status": metadata.status, "error": metadata.error,
            "requirements": bodies["requirements.json"], "design": bodies["design.json"], "code": bodies["code.json"], "events": events,
            "review_enabled": metadata.review_enabled, "revision_count": metadata.revision_count,
            "max_revisions": metadata.max_revisions, "review_status": metadata.review_status,
            "review": archive.latest if archive else None, "review_history": archive.history if archive else [],
        })
        result = _validated_result(result, metadata.user_request)
        trace = summarize_run(result.events)
        if metadata.started_at != trace.started_at or metadata.ended_at != trace.ended_at:
            raise CorruptRunError("timeline_metadata_mismatch")
        return SavedRun(metadata, result)

    def load(self, run_id: str) -> SavedRun:
        run_id = canonical_run_id(run_id)
        try:
            guard_no_links(self.root)
            folder = safe_child(self.root, run_id)
            if not folder.is_dir():
                raise RunNotFoundError("run_not_found")
            return self._load_folder(folder, run_id)
        except StorageError:
            raise
        except PermissionError as error:
            log_failure("load_result", error, run_id=run_id, agent="storage")
            raise StorageReadError("history_read_permission_denied") from None
        except (OSError, ValueError, TypeError, RecursionError) as error:
            log_failure("load_result", error, run_id=run_id, agent="storage")
            raise CorruptRunError("cannot_read_or_validate_snapshot") from None

    def save(self, result: WorkflowResult, request: str, model_name: str, elapsed: float) -> Path:
        try:
            result = _validated_result(result, request)
            if len(result.events) > 512:
                raise InvalidResultError("too_many_events")
            files = {
                "requirements.json": _encode(result.requirements.model_dump(mode="json") if result.requirements else None),
                "design.json": _encode(result.design.model_dump(mode="json") if result.design else None),
                "code.json": _encode(result.code.model_dump(mode="json") if result.code else None),
                "events.json": _encode([event.model_dump(mode="json") for event in result.events]),
            }
            if result.review_enabled:
                files["review.json"] = _encode(ReviewArchive(latest=result.review, history=result.review_history).model_dump(mode="json"))
            trace = summarize_run(result.events)
            metadata = RunMetadata(
                run_id=result.run_id, status=result.status, error=result.error,
                saved_at=datetime.now(timezone.utc), started_at=trace.started_at, ended_at=trace.ended_at,
                elapsed_seconds=elapsed, user_request=request, model_name=model_name,
                review_enabled=result.review_enabled, revision_count=result.revision_count,
                max_revisions=result.max_revisions, review_status=result.review_status,
                sha256={name: sha256(data).hexdigest() for name, data in files.items()},
            )
            files["metadata.json"] = _encode(metadata.model_dump(mode="json"))
            if sum(map(len, files.values())) > MAX_RUN_BYTES:
                raise InvalidResultError("run_size_limit")
        except (ValidationError, ValueError, TypeError, AttributeError, RecursionError) as error:
            log_failure("validate_result", error, agent="storage")
            if isinstance(error, StorageError):
                raise
            raise InvalidResultError("invalid_snapshot_schema") from None
        try:
            guard_no_links(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            target = safe_child(self.root, result.run_id)
            if target.exists():
                existing = self.load(result.run_id)
                if existing.result == result and existing.metadata.user_request == request and existing.metadata.model_name == model_name and existing.metadata.elapsed_seconds == elapsed:
                    return target  # Idempotent: no rewrites, timestamps stay unchanged.
                raise RunConflictError("existing_run_is_not_overwritten")
            staging = safe_child(self.root, f".pending-{result.run_id}-{uuid4()}")
            staging.mkdir()
            for name, body in files.items():
                output = safe_child(staging, name)
                with output.open("xb") as file:
                    file.write(body)
                    file.flush()
                    os.fsync(file.fileno())
            # Validate the complete bytes before the run becomes visible.
            self._load_folder(staging, result.run_id)
            guard_no_links(target)
            if target.exists():
                raise RunConflictError("concurrent_run_id_conflict")
            try:
                staging.rename(target)
            except FileExistsError:
                raise RunConflictError("concurrent_run_id_conflict") from None
            return target
        except StorageError as error:
            log_failure("save_result", error, run_id=result.run_id, agent="storage")
            raise
        except OSError as error:
            log_failure("save_result", error, run_id=result.run_id, agent="storage")
            raise StorageWriteError("cannot_publish_result") from None
        except (ValueError, TypeError, RecursionError) as error:
            log_failure("validate_staged_result", error, run_id=result.run_id, agent="storage")
            raise InvalidResultError("staged_result_validation_failed") from None

    def list_runs(self, limit: int = 100) -> RunListing:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("history_limit_must_be_1_to_200")
        try:
            guard_no_links(self.root)
            if not self.root.exists():
                return RunListing((), ())
            paths = list(self.root.iterdir())
        except OSError as error:
            log_failure("list_runs", error, agent="storage")
            raise StorageReadError("history_root_cannot_be_read") from None
        entries: list[RunMetadata] = []
        problems: list[HistoryProblem] = []
        for path in paths:
            if path.name.startswith("."):
                continue  # Unpublished staging is never treated as a finished run.
            try:
                run_id = canonical_run_id(path.name)
            except UnsafeStoragePathError:
                continue
            try:
                entries.append(self.load(run_id).metadata)
            except StorageError as error:
                problems.append(HistoryProblem(run_id, type(error).__name__))
        entries.sort(key=lambda item: item.saved_at, reverse=True)
        return RunListing(tuple(entries[:limit]), tuple(problems))


def default_store() -> RunStore:
    return RunStore()
