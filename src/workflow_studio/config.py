"""Explicit configuration loading without network calls or import side effects."""

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator


class Settings(BaseModel):
    """Validated DeepSeek configuration; a key is optional during initialization."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    deepseek_api_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    deepseek_base_url: HttpUrl = Field(default="https://api.deepseek.com", validate_default=True)
    deepseek_model: str = Field(default="deepseek-flash", min_length=1)
    deepseek_thinking: Literal["disabled", "enabled"] = Field(default="disabled", description="关闭思考以优先降低演示延迟；enabled 用于更深入的分析。")
    deepseek_temperature: float = Field(default=0.2, ge=0, le=2)
    deepseek_timeout: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    deepseek_max_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("deepseek_api_key", mode="before")
    @classmethod
    def normalize_api_key(cls, value: object) -> object:
        """Treat a blank key in the example file as unconfigured."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("deepseek_model", mode="before")
    @classmethod
    def normalize_model(cls, value: object) -> object:
        """Reject whitespace-only model names through min_length validation."""
        return value.strip() if isinstance(value, str) else value

    def require_api_key(self) -> SecretStr:
        """Fail clearly when a future LLM service requires credentials."""
        if self.deepseek_api_key is None:
            raise ValueError("Set DEEPSEEK_API_KEY in the environment or .env before using the LLM.")
        return self.deepseek_api_key


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Load defaults < explicit dotenv file < process environment.

    Relative paths use the current directory. None disables dotenv loading.
    Reading configuration never modifies os.environ or searches parent folders.
    """
    values: dict[str, object] = {}
    file_values = dotenv_values(env_file, interpolate=False) if env_file is not None else {}
    for name in Settings.model_fields:
        variable = name.upper()
        if variable in os.environ:
            values[name] = os.environ[variable]
        elif variable in file_values and file_values[variable] is not None:
            values[name] = file_values[variable]
    return Settings.model_validate(values)
