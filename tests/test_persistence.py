"""Actual temporary disk snapshots and AppTest recovery; Mock Agents, no API."""

from hashlib import sha256
import json
from pathlib import Path
from typing import IO
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest

from workflow_studio.config import Settings
from workflow_studio.errors import CorruptRunError, InvalidResultError, RunConflictError, RunNotFoundError, StorageWriteError, UnsafeStoragePathError
from workflow_studio.state import RequirementsOutput, DesignOutput, ModuleDesign, CodingOutput, CodeFile, WorkflowResult
from workflow_studio.state.review import ReviewOutput
from workflow_studio.storage import RunStore
from workflow_studio.storage.paths import APPLICATION_ROOT
from workflow_studio.web.app import SESSION_KEY
from workflow_studio.web.controller import PageState, collect_updates, start_task
from workflow_studio.web.persistence import open_history, save_page
from workflow_studio.workflows import run_workflow

REQUEST = "使用 Python 标准库开发整数加法工具，保留中文说明并提供测试建议。"
APP = APPLICATION_ROOT / "app.py"


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    req = RequirementsOutput(project_name="加法工具", summary="本地整数加法", functional_requirements=["两个整数求和"], non_functional_requirements=[], constraints=["仅标准库"], assumptions=[], acceptance_criteria=["2+3=5"])
    design = DesignOutput(architecture_overview="单模块", technology_stack=["Python"], modules=[ModuleDesign(name="calculator", responsibility="加法", dependencies=[], requirement_ids=["FR-1"])], data_models=[], interfaces=["add(a,b)"], project_structure="calculator.py", design_decisions=["保持简单"], preserved_constraints=req.constraints)
    code = CodingOutput(files=[CodeFile(path="calculator.py", language="python", content="def add(a: int, b: int) -> int:\n    return a + b\n", description="加法")], instructions="手动导入 add；本系统未执行", dependencies=[], test_suggestions=["2+3=5"], limitations=["未执行测试"])
    review = ReviewOutput(passed=True, score=90, issues=[], suggestions=[], summary="模型静态意见，未运行测试")
    result = {}
    for key, method, output in [("requirements", "analyze_requirements", req), ("design", "design_system", design), ("coding", "generate_code", code), ("review", "review_code", review)]:
        result[key] = MagicMock(return_value=output)
        monkeypatch.setattr(f"workflow_studio.workflows.nodes.{method}", result[key])
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="test-only-key", deepseek_model="mock-model"))
    return result


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "outputs")


@pytest.fixture
def result(agents: dict[str, MagicMock]) -> WorkflowResult:
    return run_workflow(REQUEST)


def test_save_complete_utf8_roundtrip_and_idempotence(store: RunStore, result: WorkflowResult) -> None:
    directory = store.save(result, REQUEST, "mock-model", 1.5)
    assert {path.name for path in directory.iterdir()} == {"requirements.json", "design.json", "code.json", "events.json", "metadata.json"}
    assert "加法工具" in (directory / "requirements.json").read_text(encoding="utf-8")
    loaded = store.load(result.run_id)
    assert loaded.result == result
    assert loaded.metadata.user_request == REQUEST and loaded.metadata.elapsed_seconds == 1.5
    assert loaded.metadata.status == "completed" and loaded.metadata.ended_at is not None
    before = (directory / "metadata.json").read_bytes()
    assert store.save(result, REQUEST, "mock-model", 1.5) == directory
    assert (directory / "metadata.json").read_bytes() == before
    assert store.list_runs().entries[0].run_id == result.run_id


def test_save_partial_failed_result(store: RunStore, agents: dict[str, MagicMock]) -> None:
    agents["design"].side_effect = RuntimeError("private-provider-key")
    result = run_workflow(REQUEST)
    directory = store.save(result, REQUEST, "mock-model", 0.5)
    assert json.loads((directory / "design.json").read_text()) is None
    assert json.loads((directory / "code.json").read_text()) is None
    loaded = store.load(result.run_id)
    assert loaded.result.status == "failed" and loaded.result.requirements is not None
    assert loaded.result.design is None and len(loaded.result.events) == 4
    assert "private-provider-key" not in (directory / "metadata.json").read_text()


def test_save_review_history_and_issues_status(store: RunStore, agents: dict[str, MagicMock]) -> None:
    agents["review"].return_value = ReviewOutput(passed=False, score=20, issues=["遗漏边界情况"], suggestions=["补充边界处理"], summary="静态模型意见")
    result = run_workflow(REQUEST, review_enabled=True, max_revisions=1)
    directory = store.save(result, REQUEST, "mock-model", 2.0)
    assert (directory / "review.json").is_file()
    loaded = store.load(result.run_id)
    assert loaded.result == result and loaded.result.status == "completed_with_issues"
    assert len(loaded.result.review_history) == 2 and loaded.result.revision_count == 1


@pytest.mark.parametrize("run_id", ["../src", "/tmp/run", "C:/run", "not-a-uuid", "", "../../.env"])
def test_bad_run_id_before_io(run_id: str, store: RunStore, result: WorkflowResult) -> None:
    with pytest.raises(UnsafeStoragePathError):
        store.load(run_id)
    result.run_id = run_id
    with pytest.raises(UnsafeStoragePathError):
        store.save(result, REQUEST, "mock-model", 1)
    assert not store.root.exists()


@pytest.mark.parametrize("name", ["", "src", "tests", ".git", ".venv", ".streamlit"])
def test_application_source_cannot_be_storage_root(name: str) -> None:
    with pytest.raises(UnsafeStoragePathError):
        RunStore(APPLICATION_ROOT / name)


def test_existing_run_is_never_overwritten(store: RunStore, result: WorkflowResult) -> None:
    directory = store.save(result, REQUEST, "mock-model", 1)
    old_code = (directory / "code.json").read_bytes()
    changed = result.model_copy(deep=True)
    changed.code.files[0].content = "def add(a: int, b: int) -> int:\n    return a + b + 1\n"
    with pytest.raises(RunConflictError):
        store.save(changed, REQUEST, "mock-model", 1)
    assert (directory / "code.json").read_bytes() == old_code


def test_invalid_result_rejected_before_directory_creation(store: RunStore, result: WorkflowResult) -> None:
    result.code.files[0].path = "../src/workflow_studio/config.py"
    with pytest.raises(InvalidResultError):
        store.save(result, REQUEST, "mock-model", 1)
    assert not store.root.exists()


def test_missing_run_and_missing_file_are_distinct(store: RunStore, result: WorkflowResult) -> None:
    with pytest.raises(RunNotFoundError):
        store.load(str(uuid4()))
    directory = store.save(result, REQUEST, "mock-model", 1)
    # Move the fixture's file aside, preserving it rather than deleting it.
    (directory / "code.json").rename(directory / "code.backup")
    with pytest.raises(CorruptRunError):
        store.load(result.run_id)


@pytest.mark.parametrize("damage", ["metadata_json", "hash", "utf8", "duplicate_keys", "schema_path"])
def test_corrupt_json_hash_and_schema_are_rejected(damage: str, store: RunStore, result: WorkflowResult) -> None:
    directory = store.save(result, REQUEST, "mock-model", 1)
    metadata = json.loads((directory / "metadata.json").read_text())
    if damage == "metadata_json":
        (directory / "metadata.json").write_text("{private-key", encoding="utf-8")
    elif damage == "duplicate_keys":
        (directory / "metadata.json").write_text('{"run_id":"a","run_id":"b"}', encoding="utf-8")
    else:
        data = (directory / "code.json").read_bytes()
        if damage == "hash":
            data += b" "
        elif damage == "utf8":
            data = b"\xff"
        else:
            payload = json.loads(data)
            payload["files"][0]["path"] = "../../app.py"
            data = json.dumps(payload).encode()
        (directory / "code.json").write_bytes(data)
        if damage != "hash":
            metadata["sha256"]["code.json"] = sha256(data).hexdigest()
            (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CorruptRunError):
        store.load(result.run_id)
    listing = store.list_runs()
    assert not listing.entries and listing.problems[0].run_id == result.run_id


def test_interrupted_write_is_not_listed_and_can_retry(store: RunStore, result: WorkflowResult, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.open
    def blocked(path: Path, *args: object, **kwargs: object) -> IO[bytes] | IO[str]:
        if path.name == "design.json" and args and args[0] == "xb":
            raise PermissionError(13, "private-os-detail")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", blocked)
        with pytest.raises(StorageWriteError):
            store.save(result, REQUEST, "mock-model", 1)
    assert not (store.root / result.run_id).exists()
    assert not store.list_runs().entries
    assert any(path.name.startswith(".pending-") for path in store.root.iterdir())
    store.save(result, REQUEST, "mock-model", 1)
    assert store.load(result.run_id).result == result


def test_output_root_is_a_file_preserves_existing_data(tmp_path: Path, result: WorkflowResult) -> None:
    root = tmp_path / "occupied"
    root.write_text("keep-existing", encoding="utf-8")
    with pytest.raises(StorageWriteError):
        RunStore(root).save(result, REQUEST, "mock-model", 1)
    assert root.read_text() == "keep-existing"


def test_corrupt_one_run_does_not_hide_good_run(store: RunStore, result: WorkflowResult, agents: dict[str, MagicMock]) -> None:
    first = store.save(result, REQUEST, "mock-model", 1)
    second = run_workflow(REQUEST)
    store.save(second, REQUEST, "mock-model", 2)
    (first / "metadata.json").write_text("broken", encoding="utf-8")
    listing = store.list_runs()
    assert [entry.run_id for entry in listing.entries] == [second.run_id]
    assert len(listing.problems) == 1


def test_storage_size_limits_before_io(store: RunStore, result: WorkflowResult, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr("workflow_studio.storage.store.MAX_JSON_BYTES", 8)
        with pytest.raises(InvalidResultError):
            store.save(result, REQUEST, "mock-model", 1)
    with monkeypatch.context() as patch:
        patch.setattr("workflow_studio.storage.store.MAX_RUN_BYTES", 8)
        with pytest.raises(InvalidResultError):
            store.save(result, REQUEST, "mock-model", 1)
    assert not store.root.exists()


def test_linked_output_root_rejected(tmp_path: Path) -> None:
    target, link = tmp_path / "target", tmp_path / "link"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symlinks for this test")
    with pytest.raises(UnsafeStoragePathError):
        RunStore(link)


def test_junction_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    link = tmp_path / "junction"
    monkeypatch.setattr(Path, "is_junction", lambda path: path == link, raising=False)
    with pytest.raises(UnsafeStoragePathError):
        RunStore(link)


def test_web_load_history_without_api(store: RunStore, result: WorkflowResult, agents: dict[str, MagicMock]) -> None:
    store.save(result, REQUEST, "mock-model", 1.5)
    counts = {key: mock.call_count for key, mock in agents.items()}
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    at.selectbox(key="history_choice").select(result.run_id).run()
    at.button(key="open_history_selected").click().run()
    assert not at.exception
    page = at.session_state[SESSION_KEY]
    assert page.from_history and page.result == result
    assert page.elapsed == 1.5 and page.persisted_run_id == result.run_id
    assert any("def add" in code.value for code in at.code)
    at.run()
    at.button(key="refresh_history").click().run()
    assert counts == {key: mock.call_count for key, mock in agents.items()}


def test_web_missing_id_then_recovers(store: RunStore, result: WorkflowResult, agents: dict[str, MagicMock]) -> None:
    store.save(result, REQUEST, "mock-model", 1)
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    at.text_input(key="history_run_id").input(str(uuid4())).run()
    at.button(key="open_history_id").click().run()
    assert any("找不到" in error.value for error in at.error)
    at.text_input(key="history_run_id").input(result.run_id).run()
    at.button(key="open_history_id").click().run()
    assert not at.exception and at.session_state[SESSION_KEY].history_error is None
    assert at.session_state[SESSION_KEY].result == result
    agents["requirements"].assert_called_once()


def test_web_save_failure_then_retry_does_not_call_agents(agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    original = RunStore.save
    with monkeypatch.context() as patch:
        patch.setattr(RunStore, "save", MagicMock(side_effect=StorageWriteError("private-disk-message")))
        at = AppTest.from_file(str(APP), default_timeout=10).run()
        at.text_area(key="request_input").input(REQUEST).run()
        at.button(key="start_workflow").click().run()
        page = at.session_state[SESSION_KEY]
        page.task.thread.join(timeout=10)
        at.run()
        page = at.session_state[SESSION_KEY]
        assert page.result.status == "completed" and page.persistence_error
        assert any("保存失败" in warning.value for warning in at.warning)
        assert "private-disk-message" not in str(at)
        assert page.exports and not page.persisted_run_id
    at.button(key="retry_save").click().run()
    assert not at.exception
    page = at.session_state[SESSION_KEY]
    assert page.persistence_error is None and page.persisted_run_id == page.result.run_id
    for key in ("requirements", "design", "coding"):
        agents[key].assert_called_once()


def test_failed_history_open_preserves_current_page(store: RunStore, result: WorkflowResult) -> None:
    store.save(result, REQUEST, "mock-model", 1)
    page = PageState()
    assert open_history(page, result.run_id)
    current = page.result
    assert not open_history(page, "../.env")
    assert page.result is current and page.history_error


def test_store_never_writes_generated_app_source(store: RunStore, result: WorkflowResult) -> None:
    before = APP.read_bytes()
    result.code.files[0].path = "app.py"
    result.code.files[0].content = "raise RuntimeError('must not execute')\n"
    directory = store.save(result, REQUEST, "mock-model", 1)
    assert APP.read_bytes() == before
    assert not (directory / "app.py").exists()
    assert "must not execute" in (directory / "code.json").read_text(encoding="utf-8")


def test_actual_windows_junction_rejected(tmp_path: Path) -> None:
    import os
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows-specific junction test")
    target, junction = tmp_path / "junction_target", tmp_path / "junction_link"
    target.mkdir()
    process = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "New-Item", "-ItemType", "Junction", "-Path", str(junction), "-Target", str(target)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    if process.returncode != 0:
        pytest.skip("Windows does not permit creating the fixture junction")
    assert junction.is_junction()
    with pytest.raises(UnsafeStoragePathError):
        RunStore(junction)


def test_history_still_loads_without_api_key(store: RunStore, result: WorkflowResult, agents: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    store.save(result, REQUEST, "mock-model", 1)
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings())
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert at.button(key="start_workflow").disabled
    at.text_input(key="history_run_id").input(result.run_id).run()
    at.button(key="open_history_id").click().run()
    assert not at.exception and at.session_state[SESSION_KEY].result == result
    agents["requirements"].assert_called_once()
