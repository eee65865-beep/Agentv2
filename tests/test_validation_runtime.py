"""Offline protocol tests. No candidate program is executed on the host."""

from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tarfile
from threading import Event
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from workflow_studio.state.coding import CodeFile, CodingOutput
from workflow_studio.state.review import ReviewAssessmentV2
from workflow_studio.state.review_policy import bind_review
from workflow_studio.state.workflow import create_initial_state, validate_state
from workflow_studio.validation import ValidationLimits, ValidationReport, validate_artifact, validation_quality
from workflow_studio.validation.docker import BackendError, Capture, DockerBackend, capture
from workflow_studio.validation.evaluation import candidate_result, evaluate_candidates
from workflow_studio.validation.review import review_validated
from workflow_studio.validation.store import ValidationStore
from workflow_studio.validation.suites import SUITES, get_suite

IMAGE = "python@sha256:" + "a" * 64


@pytest.fixture
def result():
    code = CodingOutput(files=[CodeFile(path="main.py", language="python", content="print('fixture; not executed on host')", description="测试夹具")], instructions="JSON CLI", dependencies=[], test_suggestions=[], limitations=[])
    return candidate_result(code, get_suite("add-cli"))


class FakeBackend:
    environment = IMAGE + ";sha256:" + "b" * 64

    def __init__(self, result=None, prepare_error=None, storage=True):
        self.override, self.prepare_error, self.storage = result, prepare_error, storage
        self.calls = []

    def prepare(self, config, remaining, cancel=None):
        assert config.is_dir() and remaining > 0
        if self.prepare_error:
            raise BackendError(self.prepare_error)

    def run_case(self, suite, case, submission, config, limits, remaining, cancel=None):
        assert submission != config and not (submission / ".env").exists()
        self.calls.append((suite.id, case.id))
        return (self.override or Capture(0, case.expected.encode()), self.storage)


def test_pass_is_independent_of_model_and_complete(result):
    backend = FakeBackend()
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek", side_effect=AssertionError("no model")):
        report = validate_artifact(result, "add-cli", backend=backend)
    assert report.status == "passed" and len(report.cases) == 6
    assert validation_quality(result, report) == "passed"
    assert result.budget is None and result.status == "completed"
    assert len(backend.calls) == 6
    assert "fixture" not in report.model_dump_json()


@pytest.mark.parametrize("captured,status,reason", [
    (Capture(0, b'{"sum":99}'), "failed", "assertion_failed"),
    (Capture(0, b'{"sum":true}'), "failed", "assertion_failed"),
    (Capture(0, b'{"sum":5,"sum":6}'), "failed", "invalid_json"),
    (Capture(0, b'NaN'), "failed", "invalid_json"),
    (Capture(0, b'not json private-api-key'), "failed", "invalid_json"),
    (Capture(1, b'private-api-key'), "failed", "program_exit"),
    (Capture(125, b'private-api-key'), "failed", "program_exit"),
    (Capture(None, b'', "runtime_error"), "error", "runtime_error"),
    (Capture(137, b'', "resource_limit"), "failed", "resource_limit"),
    (Capture(None, b'', "deadline"), "timeout", "deadline"),
    (Capture(None, b'', "output_limit"), "failed", "output_limit"),
    (Capture(None, b'', "cancelled"), "error", "cancelled"),
    (Capture(0, b'', "cleanup_failed"), "error", "cleanup_failed"),
])
def test_outcome_classification_and_no_raw_logs(result, captured, status, reason):
    report = validate_artifact(result, "add-cli", backend=FakeBackend(captured))
    assert report.status == status and report.cases[0].reason == reason
    assert "private-api-key" not in report.model_dump_json()


@pytest.mark.parametrize("reason,status", [("backend_unavailable", "not_run"), ("image_unconfigured", "not_run"), ("deadline", "timeout"), ("cancelled", "error"), ("runtime_error", "error")])
def test_no_backend_never_falls_back_to_host(result, reason, status):
    with patch("subprocess.Popen", side_effect=AssertionError("host execution forbidden")):
        report = validate_artifact(result, "add-cli", backend=FakeBackend(prepare_error=reason))
    assert report.status == status and not report.cases


def test_missing_layout_dependencies_and_manual_cases_do_not_start_backend(result):
    backend = MagicMock()
    assert validate_artifact(result, "multi-module", backend=backend).reason == "unsupported_layout"
    assert validate_artifact(result, "requirement-conflict", backend=backend).reason == "manual_required"
    assert validate_artifact(result, "add-cli", backend=backend, limits=ValidationLimits(max_cases=1)).reason == "case_limit"
    code = result.code.model_copy(update={"dependencies": ["requests"]})
    assert validate_artifact(result.model_copy(update={"code": code}), "add-cli", backend=backend).reason == "unsupported_dependencies"
    file = result.code.files[0].model_copy(update={"content": "import requests"})
    code = result.code.model_copy(update={"files": [file]})
    assert validate_artifact(result.model_copy(update={"code": code}), "add-cli", backend=backend).reason == "unsupported_dependencies"
    backend.prepare.assert_not_called()


def test_case_limit_and_cancel(result):
    cancel = Event()
    cancel.set()
    backend = FakeBackend()
    report = validate_artifact(result, "add-cli", backend=backend, cancel=cancel)
    assert report.status == "error" and report.reason == "cancelled" and not backend.calls
    for changes in ({"case_seconds": 0.0}, {"total_seconds": 301.0}, {"max_cases": 33}, {"max_output_bytes": 999999}):
        with pytest.raises(ValidationError):
            ValidationLimits(**changes)


def test_no_cases_and_unconfigured_image_cannot_pass(result):
    from workflow_studio.validation.suites import Suite
    with patch("workflow_studio.validation.service.get_suite", return_value=Suite("empty", "空集", "没有用例", ())):
        report = validate_artifact(result, "empty", backend=MagicMock())
        assert report.status == "not_run" and report.reason == "no_cases"
    assert validate_artifact(result, "add-cli", backend=DockerBackend(image="")).reason == "image_unconfigured"


def test_pass_cannot_be_empty_or_incomplete(result):
    report = validate_artifact(result, "add-cli", backend=FakeBackend())
    for changes in ({"cases": []}, {"environment": "unavailable"}, {"expected_cases": 7}, {"command": ["sh"]}):
        with pytest.raises(ValidationError):
            ValidationReport.model_validate({**report.model_dump(), **changes})


def test_stale_code_context_suite_and_run_are_rejected(result):
    report = validate_artifact(result, "add-cli", backend=FakeBackend())
    for changes in ({"artifact_digest": "0" * 64}, {"context_digest": "0" * 64}, {"suite_digest": "0" * 64}, {"run_id": "00000000-0000-0000-0000-000000000001"}):
        stale = report.model_copy(update=changes)
        assert validation_quality(result, stale) == "stale"
        with patch("workflow_studio.validation.review.review_code") as reviewer:
            with pytest.raises(ValueError):
                review_validated(result, stale)
            reviewer.assert_not_called()


def test_static_model_pass_cannot_override_failed_execution(result):
    report = validate_artifact(result, "add-cli", backend=FakeBackend(Capture(0, b"{}")))
    assessment = ReviewAssessmentV2(contract_version=2, passed=True, score=100, issue_details=[], suggestions=[], summary="仅静态意见，未自行执行测试。")
    static = bind_review(assessment, result.requirements, result.design, result.code)
    with patch("workflow_studio.validation.review.review_code", return_value=static) as reviewer:
        joint = review_validated(result, report)
    assert not joint.accepted and joint.report.status == "failed"
    assert reviewer.call_args.kwargs["validation_report"] is report


def test_real_review_prompt_consumes_bound_report(result):
    from workflow_studio.agents.review_agent import review_code
    report = validate_artifact(result, "add-cli", backend=FakeBackend())
    assessment = ReviewAssessmentV2(contract_version=2, passed=True, score=90, issue_details=[], suggestions=[], summary="静态检查；执行结果见独立报告。")
    with patch("workflow_studio.agents.review_agent.get_llm"), patch("workflow_studio.agents.review_agent.invoke_model", return_value=assessment) as invoke:
        review_code(result.requirements, result.design, result.code, validation_report=report)
    assert report.report_id in str(invoke.call_args.args[1])


def test_store_roundtrip_idempotency_and_corruption(result, tmp_path):
    store = ValidationStore(tmp_path / "evidence")
    report = validate_artifact(result, "add-cli", backend=FakeBackend())
    path = store.save(report)
    original = path.read_bytes()
    assert store.load(report.run_id, report.report_id) == report
    assert store.save(report) == path and path.read_bytes() == original
    with pytest.raises(ValueError, match="conflict"):
        store.save(report.model_copy(update={"elapsed_seconds": report.elapsed_seconds + 1}))
    path.write_text("broken", encoding="utf-8")
    assert store.list(report.run_id) == []
    with pytest.raises(ValueError):
        store.load("../escape", report.report_id)


def test_fixed_evaluation_denominator_and_no_candidates(result, tmp_path):
    empty = evaluate_candidates(tmp_path, backend=FakeBackend())
    assert empty.metrics["tasks"] == 10 and empty.metrics["not_run"] == 10 and empty.metrics["pass_rate_all_tasks"] == 0
    for suite in SUITES:
        (tmp_path / (suite.id + ".json")).write_text(result.code.model_dump_json(), encoding="utf-8")
    report = evaluate_candidates(tmp_path, backend=FakeBackend())
    # multi-module fixture intentionally lacks calculator.py; conflict is manual.
    assert report.metrics["passed"] == 8 and report.metrics["not_run"] == 2
    assert report.metrics["pass_rate_all_tasks"] == 0.8
    assert len({suite.digest for suite in SUITES}) == 10


def test_docker_prepare_rejects_wrong_platform_digest_or_missing_limits(tmp_path):
    info = {"OSType": "linux", "Warnings": [], "MemoryLimit": True, "SwapLimit": True, "PidsLimit": True, "CPUCfsQuota": True, "SecurityOptions": ["name=seccomp,profile=builtin"]}
    image = {"Os": "linux", "RepoDigests": [IMAGE], "Id": "sha256:" + "b" * 64}
    for invalid in ({**info, "OSType": "windows"}, {**info, "MemoryLimit": False}, {**info, "SecurityOptions": []}):
        with patch("workflow_studio.validation.docker.capture", return_value=Capture(0, json.dumps(invalid).encode())):
            with pytest.raises(BackendError):
                DockerBackend(IMAGE, executable="docker").prepare(tmp_path, 10)
    with patch("workflow_studio.validation.docker.capture", side_effect=[Capture(0, json.dumps(info).encode()), Capture(0, json.dumps(image).encode())]):
        backend = DockerBackend(IMAGE, executable="docker")
        backend.prepare(tmp_path, 10)
        assert IMAGE in backend.environment
    with pytest.raises(BackendError):
        DockerBackend("python:latest")


def test_image_configuration_precedence_does_not_mutate_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFLOW_VALIDATION_IMAGE", raising=False)
    path = tmp_path / "settings.env"
    path.write_text("WORKFLOW_VALIDATION_IMAGE=" + IMAGE + "\nDEEPSEEK_API_KEY=private", encoding="utf-8")
    assert DockerBackend(env_file=path).image == IMAGE
    assert "WORKFLOW_VALIDATION_IMAGE" not in os.environ
    monkeypatch.setenv("WORKFLOW_VALIDATION_IMAGE", "")
    assert DockerBackend(env_file=path).image == ""


def test_docker_flags_no_secret_env_and_cleanup_even_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-pass-to-cli")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted:2375")
    backend = DockerBackend(IMAGE, executable="docker")
    calls = []
    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return Capture(None, b"", "deadline") if "start" in argv else Capture(0, b"id")
    with patch("workflow_studio.validation.docker.capture", side_effect=fake):
        captured, _ = backend.run_case(get_suite("add-cli"), get_suite("add-cli").cases[0], tmp_path / "submission", tmp_path / "config", ValidationLimits(), 10)
    argv = calls[0][0]
    for flag in ("--pull=never", "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=128m", "--memory-swap=128m", "--cpus=0.5", "--pids-limit=32"):
        assert flag in argv
    assert "rm" in calls[-1][0] and "--force" in calls[-1][0]
    assert captured.reason == "deadline" and calls[-1][1]["cancel"] is None
    assert "DEEPSEEK_API_KEY" not in calls[0][1]["env"] and "DOCKER_HOST" not in calls[0][1]["env"]
    assert "--host" in argv and "tcp://untrusted:2375" not in argv
    assert "shell" not in argv and len([x for x in argv if "type=bind" in x]) == 1


def test_storage_file_is_parsed_without_extracting_untrusted_tar():
    suite = get_suite("todo-storage")
    buffer = BytesIO()
    content = json.dumps(json.loads(suite.cases[0].stdin)["items"]).encode()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo("todos.json")
        entry.size = len(content)
        archive.addfile(entry, BytesIO(content))
    assert DockerBackend._check_storage(buffer.getvalue(), suite.cases[0])
    assert not DockerBackend._check_storage(b"invalid archive", suite.cases[0])


def test_storage_tmpfs_is_read_before_stopping_container(tmp_path):
    suite = get_suite("todo-storage")
    buffer = BytesIO()
    content = json.dumps(json.loads(suite.cases[0].stdin)["items"]).encode()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo("todos.json")
        entry.size = len(content)
        archive.addfile(entry, BytesIO(content))
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        if "inspect" in argv:
            return Capture(0, json.dumps({"Status": "running", "Running": True, "ExitCode": 0, "Error": ""}).encode())
        if "exec" in argv:
            return Capture(0, suite.cases[0].expected.encode())
        if "cp" in argv:
            return Capture(0, buffer.getvalue())
        return Capture(0, b"id")
    with patch("workflow_studio.validation.docker.capture", side_effect=fake):
        value, storage_ok = DockerBackend(IMAGE, executable="docker").run_case(suite, suite.cases[0], tmp_path, tmp_path, ValidationLimits(), 10)
    assert value.exit_code == 0 and storage_ok
    assert "exec" in calls[2] and "cp" in calls[-2] and "rm" in calls[-1]
    assert "import time;time.sleep(300)" in calls[0]


@pytest.mark.parametrize("state,reason,exit_code", [
    ({"Status": "exited", "Running": False, "ExitCode": 125, "Error": ""}, None, 125),
    ({"Status": "exited", "Running": False, "ExitCode": 137, "Error": "", "OOMKilled": True}, "resource_limit", 137),
    ({"Status": "created", "Running": False, "ExitCode": 0, "Error": "cannot start"}, "runtime_error", 1),
])
def test_container_state_disambiguates_user_exit_and_start_failure(tmp_path, state, reason, exit_code):
    def fake(argv, **kwargs):
        if "inspect" in argv:
            return Capture(0, json.dumps(state).encode())
        return Capture(1 if "start" in argv else 0, b"{}")
    with patch("workflow_studio.validation.docker.capture", side_effect=fake):
        value, _ = DockerBackend(IMAGE, executable="docker").run_case(get_suite("add-cli"), get_suite("add-cli").cases[0], tmp_path, tmp_path, ValidationLimits(), 10)
    assert value.reason == reason and value.exit_code == exit_code


def test_bounded_capture_uses_only_trusted_test_scripts_and_kills_on_limits():
    # These are repository-owned fixture scripts, not generated candidates.
    env = {key: os.environ[key] for key in ("SystemRoot", "PATH") if key in os.environ}
    normal = capture([sys.executable, "-I", "-c", "print('ok')"], env=env, timeout=3)
    assert normal.stdout.strip() == b"ok" and normal.exit_code == 0
    flood = capture([sys.executable, "-I", "-c", "import sys;sys.stdout.write('x'*1000000)"], env=env, timeout=3, limit=1024)
    assert flood.reason == "output_limit" and len(flood.stdout) <= 1024
    slow = capture([sys.executable, "-I", "-c", "import time;time.sleep(5)"], env=env, timeout=0.1)
    assert slow.reason == "deadline"


def test_page_execution_is_explicit_and_rerender_does_not_repeat(result, monkeypatch):
    from workflow_studio.web.app import SESSION_KEY
    from workflow_studio.web.controller import PageState
    from workflow_studio.web import validation
    state = create_initial_state(get_suite("add-cli").contract)
    state.update(status="completed", requirements=result.requirements.model_dump(), design=result.design.model_dump(), code=result.code.model_dump(), run_id=result.run_id)
    page = PageState(result=result, snapshot=validate_state(state), request=get_suite("add-cli").contract)
    report = validate_artifact(result, "add-cli", backend=FakeBackend())
    action = MagicMock(return_value=report)
    monkeypatch.setattr(validation, "validate_artifact", action)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10)
    at.session_state[SESSION_KEY] = page
    at.run()
    assert not at.exception
    action.assert_not_called()
    at.button(key="validate_artifact").click().run()
    panel = at.session_state[validation.SESSION_KEY]
    panel.thread.join(timeout=10)
    at.run()
    assert not at.exception and panel.report.status == "passed"
    at.run()
    assert action.call_count == 1 and page.result is result


@pytest.mark.skipif(os.environ.get("WORKFLOW_VALIDATION_INTEGRATION") != "1", reason="explicit Docker integration opt-in not enabled")
def test_opt_in_real_container_checks_identity_filesystem_and_network(result):
    # Repository-owned canary. Runs only INSIDE the configured container.
    content = '''import json, os, socket, sys
assert os.getuid() == 65534
assert 'DEEPSEEK_API_KEY' not in os.environ
try:
    open('/submission/host-write-probe', 'w')
except OSError:
    pass
else:
    raise AssertionError('submission mount is writable')
sock = socket.socket()
sock.settimeout(0.2)
try:
    sock.connect(('1.1.1.1', 53))
except OSError:
    pass
else:
    raise AssertionError('network is reachable')
finally:
    sock.close()
d = json.load(sys.stdin)
a, b = d.get('a'), d.get('b')
print(json.dumps({'sum': a+b} if type(a) is int and type(b) is int else {'error':'invalid_input'}))
'''
    file = result.code.files[0].model_copy(update={"content": content})
    code = result.code.model_copy(update={"files": [file]})
    report = validate_artifact(result.model_copy(update={"code": code}), "add-cli")
    assert report.status == "passed", (report.status, report.reason, report.cases)
    content = "import json,sys\nd=json.load(sys.stdin)\nwith open('todos.json','w',encoding='utf-8') as f: json.dump(d['items'],f)\nwith open('todos.json',encoding='utf-8') as f: data=json.load(f)\nprint(json.dumps({'items':data}))\n"
    code = result.code.model_copy(update={"files": [file.model_copy(update={"content": content})]})
    report = validate_artifact(result.model_copy(update={"code": code}), "todo-storage")
    assert report.status == "passed", (report.status, report.reason, report.cases)
