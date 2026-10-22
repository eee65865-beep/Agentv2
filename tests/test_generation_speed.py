"""Validate latency settings through the real adapter and SDK, without network."""

import json
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from workflow_studio.config import Settings, load_settings
from workflow_studio.llm.deepseek_client import DeepSeekClient
from workflow_studio.llm.retry_policy import DeepSeekRetryClient


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize("thinking", ["disabled", "enabled"])
@pytest.mark.parametrize("structured", [False, True])
def test_thinking_setting_reaches_actual_request(
    thinking: str, structured: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        message: dict[str, object] = {"role": "assistant", "content": "ok"}
        if structured:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "mock-call", "type": "function", "function": {
                    "name": "Answer", "arguments": json.dumps({"answer": "ok"}),
                }},
            ]}
        return httpx2.Response(200, json={
            "id": "mock", "object": "chat.completion", "created": 1,
            "model": "configured-model", "choices": [{"index": 0,
                "message": message, "finish_reason": "tool_calls" if structured else "stop"}],
        })

    with DeepSeekRetryClient(
        api_key="mock-only-key", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    ) as root:
        monkeypatch.setattr("workflow_studio.llm.deepseek_client.DeepSeekRetryClient", lambda **kwargs: root)
        settings = Settings.model_validate({
            "deepseek_api_key": SecretStr("mock-only-key"),
            "deepseek_model": "configured-model", "deepseek_thinking": thinking,
        })
        client = DeepSeekClient(settings)
        if structured:
            assert client.invoke_structured("mock", Answer).answer == "ok"
        else:
            assert client.invoke("mock").text == "ok"
    assert len(requests) == 1
    assert requests[0]["thinking"] == {"type": thinking}
    assert requests[0]["model"] == "configured-model"


def test_thinking_config_precedence_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env: Path = tmp_path / ".env"
    env.write_text("DEEPSEEK_THINKING=enabled\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    assert load_settings(env).deepseek_thinking == "enabled"
    monkeypatch.setenv("DEEPSEEK_THINKING", "disabled")
    assert load_settings(env).deepseek_thinking == "disabled"
    monkeypatch.setenv("DEEPSEEK_THINKING", "invalid")
    with pytest.raises(ValidationError):
        load_settings(env)


def test_thinking_override_resets_after_exception() -> None:
    from workflow_studio.llm.options import thinking_mode, current_thinking_mode
    with thinking_mode(True):
        assert current_thinking_mode() == "enabled"
        with pytest.raises(RuntimeError):
            with thinking_mode(False):
                assert current_thinking_mode() == "disabled"
                raise RuntimeError("mock failure")
        assert current_thinking_mode() == "enabled"
    assert current_thinking_mode() is None
    with pytest.raises(ValueError):
        with thinking_mode("enabled"):  # type: ignore[arg-type]
            pytest.fail("invalid option should be rejected")
