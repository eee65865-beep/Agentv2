"""Configuration behavior verified with isolated environment and temp files."""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from workflow_studio.__main__ import main
from workflow_studio.config import Settings, load_settings


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


def test_missing_env_and_key_are_allowed() -> None:
    settings = load_settings()
    assert settings.deepseek_api_key is None
    assert settings.deepseek_model == "deepseek-flash"
    assert settings.deepseek_temperature == 0.2
    assert settings.deepseek_timeout == 60.0
    assert settings.deepseek_max_retries == 2
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        settings.require_api_key()


def test_dotenv_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_MODEL=from-file\nDEEPSEEK_API_KEY=test-only-secret\nDEEPSEEK_TIMEOUT=12\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_MODEL", "from-process")
    before = dict(os.environ)
    settings = load_settings(env)
    assert settings.deepseek_model == "from-process"
    assert settings.deepseek_timeout == 12.0
    assert isinstance(settings.require_api_key(), SecretStr)
    assert "test-only-secret" not in repr(settings)
    assert "test-only-secret" not in settings.model_dump_json()
    assert dict(os.environ) == before


def test_none_disables_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_MODEL=from-file\n", encoding="utf-8")
    assert load_settings(None).deepseek_model == "deepseek-flash"


def test_blank_key_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")
    assert load_settings(None).deepseek_api_key is None


@pytest.mark.parametrize(("variable", "value"), [
    ("DEEPSEEK_TEMPERATURE", "3"), ("DEEPSEEK_TIMEOUT", "0"),
    ("DEEPSEEK_TIMEOUT", "nan"), ("DEEPSEEK_BASE_URL", "not-a-url"),
    ("DEEPSEEK_MODEL", "   "), ("DEEPSEEK_MAX_RETRIES", "-1"),
    ("DEEPSEEK_MAX_RETRIES", "abc"),
])
def test_invalid_configuration(variable: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError):
        load_settings(None)


def test_cli_success_without_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert main() == 0
    assert "initialization OK" in capsys.readouterr().out


def test_cli_validation_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DEEPSEEK_TIMEOUT", "invalid-test-value")
    assert main() == 1
    output = capsys.readouterr().out
    assert "deepseek_timeout" in output
    assert "invalid-test-value" not in output


def test_retry_count_from_dotenv(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_MAX_RETRIES=4\n", encoding="utf-8")
    assert load_settings(env).deepseek_max_retries == 4
