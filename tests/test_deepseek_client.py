"""Mock-only LLM tests: no paid provider requests."""

import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from langchain_core.messages import AIMessage
from openai import (
    APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError,
    BadRequestError, OpenAIError, PermissionDeniedError, RateLimitError,
)
from pydantic import SecretStr

from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.config import Settings
from workflow_studio.llm.deepseek_client import DeepSeekClient, get_llm
from workflow_studio.llm.errors import (
    LLMAuthenticationError, LLMCallError, LLMConfigurationError, LLMError,
    LLMNetworkError, LLMRateLimitError, LLMTimeoutError,
    LLMStructuredOutputError, LLMStructuredOutputUnsupported, LLMTruncatedOutputError,
)

SECRET = "unit-test-only-secret"


@pytest.fixture(autouse=True)
def mock_chat_deepseek() -> Iterator[MagicMock]:
    # All tests replace the constructor, so none can create a provider client.
    with patch("workflow_studio.llm.deepseek_client.ChatDeepSeek") as constructor:
        yield constructor


@pytest.fixture
def settings() -> Settings:
    return Settings(
        deepseek_api_key=SecretStr(SECRET), deepseek_model="configured-test-model",
        deepseek_timeout=17, deepseek_max_retries=3,
    )


def test_factory_passes_supported_parameters(settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    client = get_llm(settings)
    kwargs = mock_chat_deepseek.call_args.kwargs
    assert isinstance(client, DeepSeekClient)
    assert kwargs["model"] == "configured-test-model"
    assert kwargs["api_key"].get_secret_value() == SECRET
    assert kwargs["base_url"] == "https://api.deepseek.com/"
    assert kwargs["timeout"] == 17
    assert kwargs["max_retries"] == 3
    assert kwargs["temperature"] == 0.2
    mock_chat_deepseek.return_value.invoke.assert_not_called()


def test_missing_key_fails_before_construction(mock_chat_deepseek: MagicMock) -> None:
    with pytest.raises(LLMConfigurationError, match="DEEPSEEK_API_KEY"):
        get_llm(Settings())
    mock_chat_deepseek.assert_not_called()


def test_factory_uses_configuration_loader(settings: Settings) -> None:
    with patch("workflow_studio.llm.deepseek_client.load_settings", return_value=settings) as loader:
        assert get_llm().model_name == settings.deepseek_model
        loader.assert_called_once_with()


def test_invalid_loaded_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_TIMEOUT", "invalid")
    with pytest.raises(LLMConfigurationError, match="Invalid DeepSeek configuration"):
        get_llm()


def test_initialization_error_is_sanitized(settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    mock_chat_deepseek.side_effect = ValueError(SECRET)
    with pytest.raises(LLMConfigurationError) as info:
        get_llm(settings)
    assert SECRET not in str(info.value)
    assert info.value.__suppress_context__


def test_success_logs_allowlisted_usage(settings: Settings, mock_chat_deepseek: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    message = AIMessage(content="private-response", usage_metadata={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12})
    mock_chat_deepseek.return_value.invoke.return_value = message
    client = get_llm(settings)
    with caplog.at_level(logging.INFO, logger="workflow_studio.llm.deepseek_client"):
        result = client.invoke("private-prompt")
    assert result is message
    mock_chat_deepseek.return_value.invoke.assert_called_once_with("private-prompt")
    for expected in ["model=configured-test-model", "elapsed_seconds=", "status=success", "input_tokens=7", "output_tokens=5", "total_tokens=12"]:
        assert expected in caplog.text
    for private in [SECRET, "private-prompt", "private-response"]:
        assert private not in caplog.text


@pytest.mark.parametrize(("metadata", "expected"), [
    ({"token_usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6, "secret": SECRET}}, "total_tokens=6"),
    ({}, "total_tokens=None"),
    ({"token_usage": {"total_tokens": SECRET}}, "total_tokens=None"),
])
def test_optional_usage_metadata(metadata: dict[str, object], expected: str, settings: Settings, mock_chat_deepseek: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    mock_chat_deepseek.return_value.invoke.return_value = AIMessage(content="ok", response_metadata=metadata)
    with caplog.at_level(logging.INFO, logger="workflow_studio.llm.deepseek_client"):
        get_llm(settings).invoke("hello")
    assert expected in caplog.text
    assert SECRET not in caplog.text


def status_error(error_type: type[APIStatusError], code: int) -> APIStatusError:
    response = httpx.Response(code, request=httpx.Request("POST", "https://example.invalid/chat/completions"))
    return error_type(SECRET, response=response, body={"detail": SECRET})


@pytest.mark.parametrize(("error", "expected"), [
    (status_error(AuthenticationError, 401), LLMAuthenticationError),
    (status_error(PermissionDeniedError, 403), LLMAuthenticationError),
    (status_error(RateLimitError, 429), LLMRateLimitError),
    (APITimeoutError(request=httpx.Request("POST", "https://example.invalid")), LLMTimeoutError),
    (APIConnectionError(message=SECRET, request=httpx.Request("POST", "https://example.invalid")), LLMNetworkError),
    (status_error(BadRequestError, 400), LLMCallError),
    (status_error(APIStatusError, 503), LLMCallError),
    (OpenAIError(SECRET), LLMCallError),
    (RuntimeError(SECRET), LLMCallError),
])
def test_error_categories_and_safe_logs(error: Exception, expected: type[LLMError], settings: Settings, mock_chat_deepseek: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    mock_chat_deepseek.return_value.invoke.side_effect = error
    with caplog.at_level(logging.INFO, logger="workflow_studio.llm.deepseek_client"):
        with pytest.raises(expected) as info:
            get_llm(settings).invoke("private-prompt")
    assert SECRET not in str(info.value)
    assert SECRET not in caplog.text
    assert "private-prompt" not in caplog.text
    assert f"status={expected.__name__}" in caplog.text
    assert "elapsed_seconds=" in caplog.text
    assert info.value.__suppress_context__
    # The wrapper never layers another retry loop over the SDK.
    mock_chat_deepseek.return_value.invoke.assert_called_once()


def test_invalid_response_is_model_error(settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    mock_chat_deepseek.return_value.invoke.return_value = "invalid-response"
    with pytest.raises(LLMCallError):
        get_llm(settings).invoke("hello")


@pytest.fixture
def structured_result() -> RequirementsOutput:
    return RequirementsOutput(
        project_name="Test", summary="Task management", functional_requirements=["Create task"],
        non_functional_requirements=[], constraints=[], assumptions=[], acceptance_criteria=["Task is listed"],
    )


def test_structured_output_and_telemetry(settings: Settings, mock_chat_deepseek: MagicMock, structured_result: RequirementsOutput, caplog: pytest.LogCaptureFixture) -> None:
    raw = AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})
    runner = mock_chat_deepseek.return_value.with_structured_output.return_value
    runner.invoke.return_value = {"raw": raw, "parsed": structured_result, "parsing_error": None}
    with caplog.at_level(logging.INFO, logger="workflow_studio.llm.deepseek_client"):
        result = get_llm(settings).invoke_structured("private-prompt", RequirementsOutput)
    assert result is structured_result
    mock_chat_deepseek.return_value.with_structured_output.assert_called_once_with(
        RequirementsOutput, method="function_calling", include_raw=True,
    )
    assert "total_tokens=30" in caplog.text
    assert "status=success" in caplog.text
    assert "private-prompt" not in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("result", [
    {"raw": AIMessage(content="private"), "parsed": None, "parsing_error": ValueError(SECRET)},
    {"raw": AIMessage(content=""), "parsed": {}, "parsing_error": None},
    "invalid-envelope",
])
def test_structured_invalid_output(result: object, settings: Settings, mock_chat_deepseek: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    mock_chat_deepseek.return_value.with_structured_output.return_value.invoke.return_value = result
    with caplog.at_level(logging.INFO, logger="workflow_studio.llm.deepseek_client"):
        with pytest.raises(LLMStructuredOutputError) as info:
            get_llm(settings).invoke_structured("hello", RequirementsOutput)
    assert SECRET not in str(info.value)
    assert SECRET not in caplog.text
    assert "status=LLMStructuredOutputError" in caplog.text


def test_structured_local_unsupported(settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    mock_chat_deepseek.return_value.with_structured_output.side_effect = NotImplementedError(SECRET)
    with pytest.raises(LLMStructuredOutputUnsupported):
        get_llm(settings).invoke_structured("hello", RequirementsOutput)


def test_structured_remote_unsupported(settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    error = status_error(BadRequestError, 400)
    error.body = {"error": {"param": "tools", "code": "unsupported_parameter", "message": SECRET}}
    mock_chat_deepseek.return_value.with_structured_output.return_value.invoke.side_effect = error
    with pytest.raises(LLMStructuredOutputUnsupported) as info:
        get_llm(settings).invoke_structured("hello", RequirementsOutput)
    assert SECRET not in str(info.value)


@pytest.mark.parametrize(("error", "expected"), [
    (status_error(BadRequestError, 400), LLMCallError),
    (status_error(AuthenticationError, 401), LLMAuthenticationError),
    (status_error(RateLimitError, 429), LLMRateLimitError),
    (APIConnectionError(request=httpx.Request("POST", "https://example.invalid")), LLMNetworkError),
])
def test_structured_transport_errors_keep_category(error: Exception, expected: type[Exception], settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    mock_chat_deepseek.return_value.with_structured_output.return_value.invoke.side_effect = error
    with pytest.raises(expected) as info:
        get_llm(settings).invoke_structured("hello", RequirementsOutput)
    assert not isinstance(info.value, LLMStructuredOutputUnsupported)


def test_structured_runner_unsupported(settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    mock_chat_deepseek.return_value.with_structured_output.return_value.invoke.side_effect = NotImplementedError(SECRET)
    with pytest.raises(LLMStructuredOutputUnsupported):
        get_llm(settings).invoke_structured("hello", RequirementsOutput)


@pytest.mark.parametrize("metadata", [
    {"finish_reason": "length"}, {"finish_reason": "max_tokens"},
    {"finish_reason": "max_output_tokens"}, {"status": "incomplete"},
])
@pytest.mark.parametrize("structured", [False, True])
def test_provider_truncation_never_succeeds(metadata: dict[str, str], structured: bool, settings: Settings, mock_chat_deepseek: MagicMock, structured_result: RequirementsOutput, caplog: pytest.LogCaptureFixture) -> None:
    raw = AIMessage(content="valid looking text", response_metadata=metadata)
    client = get_llm(settings)
    mock_chat_deepseek.return_value.invoke.return_value = raw
    mock_chat_deepseek.return_value.with_structured_output.return_value.invoke.return_value = {
        "raw": raw, "parsed": structured_result, "parsing_error": None,
    }
    with caplog.at_level(logging.INFO, logger="workflow_studio.llm.deepseek_client"):
        with pytest.raises(LLMTruncatedOutputError):
            if structured:
                client.invoke_structured("hello", RequirementsOutput)
            else:
                client.invoke("hello")
    assert "status=LLMTruncatedOutputError" in caplog.text


def test_sdk_length_error_is_classified(settings: Settings, mock_chat_deepseek: MagicMock) -> None:
    from openai import LengthFinishReasonError
    mock_chat_deepseek.return_value.with_structured_output.return_value.invoke.side_effect = LengthFinishReasonError(completion=MagicMock())
    with pytest.raises(LLMTruncatedOutputError):
        get_llm(settings).invoke_structured("hello", RequirementsOutput)
