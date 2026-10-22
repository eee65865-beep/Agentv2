"""No network: actual pinned SDK + mock transport verifies bounded retries."""

import logging
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile

import httpx2
from openai import AuthenticationError, RateLimitError
import pytest
from pydantic import ValidationError

from workflow_studio.config import Settings
from workflow_studio.diagnostics import configure_logging, log_failure
from workflow_studio.errors import ExportLimitError, UnsafeCodePathError, friendly_error
from workflow_studio.exporters.documents import code_zip, requirements_markdown
from workflow_studio.llm.retry_policy import DeepSeekRetryClient
from workflow_studio.state import CodingOutput, CodeFile, RequirementsOutput


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_nontransient_http_never_retries_even_when_advised(status: int) -> None:
    with DeepSeekRetryClient(api_key="private-test-key", max_retries=2) as client:
        response = httpx2.Response(status, headers={"x-should-retry": "true"}, request=httpx2.Request("POST", "https://api.deepseek.com"))
        assert not client._should_retry(response)


def test_authentication_is_one_attempt_with_real_sdk_mock_transport() -> None:
    calls = []
    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(401, headers={"x-should-retry": "true"}, json={"error": {"message": "private-provider-body"}})
    with DeepSeekRetryClient(api_key="private-test-key", max_retries=2, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
        with pytest.raises(AuthenticationError):
            client.chat.completions.create(model="mock-model", messages=[{"role": "user", "content": "mock"}])
    assert len(calls) == 1


@pytest.mark.parametrize("recover", [False, True])
def test_rate_limit_retries_are_bounded_and_can_recover(recover: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openai._base_client.time.sleep", lambda _: None)
    calls = []
    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if not recover or len(calls) < 3:
            return httpx2.Response(429, json={"error": {"message": "limited"}})
        return httpx2.Response(200, json={"id": "mock", "object": "chat.completion", "created": 1, "model": "mock-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    with DeepSeekRetryClient(api_key="private-test-key", max_retries=2, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
        if recover:
            assert client.chat.completions.create(model="mock-model", messages=[]).choices[0].message.content == "ok"
        else:
            with pytest.raises(RateLimitError):
                client.chat.completions.create(model="mock-model", messages=[])
    assert len(calls) == 3


def test_retry_budget_has_a_hard_configuration_cap() -> None:
    assert Settings(deepseek_max_retries=5).deepseek_max_retries == 5
    with pytest.raises(ValidationError):
        Settings(deepseek_max_retries=6)


def test_friendly_messages_never_echo_unknown_exception_body() -> None:
    for error in [RuntimeError("sk-private-test-key"), "sk-private-test-key", "Bearer private-credential"]:
        assert "private" not in friendly_error(error)
    assert "Pydantic" in friendly_error(ValidationError.from_exception_data("safe", []))


def test_technical_log_preserves_context_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="workflow_studio.diagnostics"):
        try:
            raise RuntimeError("private-body-with-api-key")
        except RuntimeError as error:
            log_failure("agent_execution", error, run_id="not-valid-private-id", agent="design_agent")
    assert "RuntimeError" in caplog.text and "agent=design_agent" in caplog.text
    assert "frames=" in caplog.text and "unavailable" in caplog.text
    assert "private" not in caplog.text


def test_utf8_file_logging_is_idempotent(tmp_path: Path) -> None:
    namespace = logging.getLogger("workflow_studio")
    before = list(namespace.handlers)
    try:
        configure_logging(tmp_path / "logs")
        configure_logging(tmp_path / "logs")
        new = [handler for handler in namespace.handlers if handler not in before]
        assert len(new) == 1
        log_failure("save_result", PermissionError(13, "private-file-detail"))
        for handler in new:
            handler.flush()
        text = (tmp_path / "logs" / "workflow_studio.log").read_text(encoding="utf-8")
        assert "PermissionError" in text and "errno=13" in text
        assert "private-file-detail" not in text
    finally:
        for handler in list(namespace.handlers):
            if handler not in before:
                namespace.removeHandler(handler)
                handler.close()


def example_code() -> CodingOutput:
    return CodingOutput(files=[CodeFile(path="app.py", language="python", content="raise RuntimeError('must not execute')\n", description="文本代码")], instructions="仅手工运行", dependencies=[], test_suggestions=[], limitations=["未执行"])


@pytest.mark.parametrize("path", ["../app.py", "/app.py", "C:/app.py", "nested/../../app.py"])
def test_zip_unsafe_paths_are_classified(path: str) -> None:
    code = example_code()
    code.files[0].path = path
    with pytest.raises(UnsafeCodePathError):
        code_zip(code)


def test_zip_conflicts_and_file_count_limit() -> None:
    code = example_code()
    code.files.append(code.files[0].model_copy(update={"path": "APP.py"}))
    with pytest.raises(ValueError):
        code_zip(code)
    code.files = [CodeFile(path=f"file{index}.txt", language="text", content="safe", description="文本") for index in range(21)]
    with pytest.raises(ExportLimitError):
        code_zip(code)


def test_markdown_and_zip_byte_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    req = RequirementsOutput(project_name="Demo", summary="test", functional_requirements=["one"], non_functional_requirements=[], constraints=[], assumptions=[], acceptance_criteria=["works"])
    monkeypatch.setattr("workflow_studio.exporters.documents.MAX_MARKDOWN_BYTES", 1)
    with pytest.raises(ExportLimitError):
        requirements_markdown(req)
    monkeypatch.setattr("workflow_studio.exporters.documents.MAX_ZIP_BYTES", 1)
    with pytest.raises(ExportLimitError):
        code_zip(example_code())


def test_zip_is_memory_only_and_generated_code_never_runs() -> None:
    code = example_code()
    with ZipFile(BytesIO(code_zip(code))) as archive:
        assert archive.namelist() == ["app.py"]
        assert archive.read("app.py").decode() == code.files[0].content


def test_actual_sdk_timeout_retry_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai import APITimeoutError
    monkeypatch.setattr("openai._base_client.time.sleep", lambda _: None)
    calls = []
    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise httpx2.ReadTimeout("private-timeout-detail", request=request)
    with DeepSeekRetryClient(api_key="private-test-key", max_retries=2, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
        with pytest.raises(APITimeoutError):
            client.chat.completions.create(model="mock-model", messages=[])
    assert len(calls) == 3
