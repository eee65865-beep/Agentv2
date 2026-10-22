"""Offline initialization check: python -m workflow_studio."""

from pydantic import ValidationError

from workflow_studio.config import load_settings


def main() -> int:
    """Validate configuration without calling DeepSeek or printing credentials."""
    try:
        settings = load_settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors(include_input=False))
        print(f"Configuration invalid; check: {fields}")
        return 1
    print("Multi-Agent Workflow Studio: initialization OK.")
    status = "configured" if settings.deepseek_api_key is not None else "not configured (optional at this stage)"
    print(f"DeepSeek API key: {status}")
    print("Offline configuration check only; no API calls were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
