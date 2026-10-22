"""Exercise the smoke-test CLI entirely with a mocked application client."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from workflow_studio.llm.errors import LLMCallError, LLMConfigurationError
from workflow_studio.llm.smoke_test import SMOKE_INPUT, main, run_smoke_test


@pytest.fixture(autouse=True)
def block_real_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked() -> None:
        raise AssertionError("A unit test must not create a real API client")
    monkeypatch.setattr("workflow_studio.llm.smoke_test.get_llm", blocked)


def test_prompt_template_and_nonempty_response() -> None:
    client = MagicMock()
    client.invoke.return_value = AIMessage(content="DeepSeek API 连接成功。")
    assert run_smoke_test(client) == "DeepSeek API 连接成功。"
    client.invoke.assert_called_once()
    prompt = client.invoke.call_args.args[0]
    assert prompt.to_messages()[0].content == SMOKE_INPUT
    assert SMOKE_INPUT == "请只回答：DeepSeek API 连接成功。"


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_is_failure(text: str) -> None:
    client = MagicMock()
    client.invoke.return_value = AIMessage(content=text)
    with pytest.raises(LLMCallError, match="empty text"):
        run_smoke_test(client)


def test_cli_requires_explicit_run() -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_cli_mocked_success(capsys: pytest.CaptureFixture[str]) -> None:
    client = MagicMock()
    client.invoke.return_value = AIMessage(content="connected")
    with patch("workflow_studio.llm.smoke_test.get_llm", return_value=client):
        assert main(["--run"]) == 0
    assert "API smoke test passed: connected" in capsys.readouterr().out


def test_cli_configuration_failure(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("workflow_studio.llm.smoke_test.get_llm", side_effect=LLMConfigurationError("Missing DEEPSEEK_API_KEY")):
        assert main(["--run"]) == 1
    assert "LLMConfigurationError" in capsys.readouterr().out
