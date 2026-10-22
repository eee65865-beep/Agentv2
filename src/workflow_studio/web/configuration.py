"""Expose only safe display metadata; never put credentials in UI state."""

from dataclasses import dataclass
from pydantic import ValidationError

from workflow_studio.config import load_settings


@dataclass(frozen=True)
class ConfigurationView:
    model_name: str
    configured: bool
    error: str | None = None
    thinking_enabled: bool = False


def get_configuration() -> ConfigurationView:
    try:
        settings = load_settings()
    except (ValidationError, OSError):
        return ConfigurationView("配置无效", False, "配置读取失败，请检查 .env 中的模型、超时和重试参数。")
    return ConfigurationView(settings.deepseek_model, settings.deepseek_api_key is not None, thinking_enabled=settings.deepseek_thinking == "enabled")
