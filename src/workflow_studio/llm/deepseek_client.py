"""Shared synchronous DeepSeek factory, error translation and safe telemetry.

Future Agents should obtain this client via get_llm() and invoke it directly.
The underlying SDK owns retries; this module does not add a second retry loop.
"""

import logging
from time import perf_counter
from typing import Mapping, TypeVar

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAIError,
    LengthFinishReasonError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from workflow_studio.config import Settings, load_settings
from workflow_studio.budget import BudgetPersistenceError, RunStopped, budgeted_call, report_token_usage
from workflow_studio.llm.errors import (
    LLMAuthenticationError,
    LLMCallError,
    LLMConfigurationError,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMStructuredOutputError,
    LLMStructuredOutputUnsupported,
    LLMTruncatedOutputError,
)

from workflow_studio.llm.retry_policy import DeepSeekRetryClient
from workflow_studio.llm.options import current_model_profile, current_thinking_mode
from workflow_studio.llm.telemetry import record_llm_call
from workflow_studio.state.telemetry import LLMCallRecord, TokenUsage

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=BaseModel)


def _translate_error(error: Exception) -> LLMError:
    """Never expose raw provider messages, request bodies or headers."""
    if isinstance(error, LLMTruncatedOutputError):
        return error
    if isinstance(error, LengthFinishReasonError):
        return LLMTruncatedOutputError("DeepSeek output reached its token limit; incomplete output was rejected.")
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return LLMAuthenticationError("DeepSeek authentication/access failed (401/403). Check DEEPSEEK_API_KEY and account permissions.")
    if isinstance(error, RateLimitError):
        return LLMRateLimitError("DeepSeek rate limit reached (429). Reduce concurrency and retry later.")
    if isinstance(error, APITimeoutError):
        return LLMTimeoutError("DeepSeek request timed out. Check the network or increase DEEPSEEK_TIMEOUT.")
    if isinstance(error, APIConnectionError):
        return LLMNetworkError("Cannot connect to DeepSeek. Check network, proxy, TLS and DEEPSEEK_BASE_URL.")
    if isinstance(error, APIStatusError):
        return LLMCallError(f"DeepSeek model call failed (HTTP {error.status_code}). Check model availability, request settings and provider status.")
    if isinstance(error, OpenAIError):
        return LLMCallError("DeepSeek SDK could not complete the model call. Check provider status and request settings.")
    return LLMCallError("DeepSeek model call failed or returned an invalid response.")


def _unsupported_structured_feature(error: Exception) -> bool:
    """Inspect provider metadata without logging its potentially sensitive body."""
    if not isinstance(error, APIStatusError) or error.status_code not in (400, 422):
        return False
    body = error.body
    if not isinstance(body, dict):
        return False
    detail = body.get("error", body)
    if not isinstance(detail, dict):
        return False
    parameter = detail.get("param")
    code = detail.get("code")
    description = str(detail.get("message", "")).lower()
    feature = parameter in ("tools", "tool_choice", "response_format") or any(
        word in description for word in ("tool", "function calling", "response_format", "structured output")
    )
    unsupported = code in ("unsupported_parameter", "unsupported_feature", "unsupported_value") or any(
        word in description for word in ("not supported", "unsupported", "does not support")
    )
    return feature and unsupported


def _reject_truncated(message: AIMessage) -> None:
    """A parseable response is still invalid if the provider reports truncation."""
    metadata = message.response_metadata
    if metadata.get("finish_reason") in ("length", "max_tokens", "max_output_tokens") or metadata.get("status") == "incomplete":
        raise LLMTruncatedOutputError("DeepSeek output reached its token limit; incomplete output was rejected.")


def _token_counts(message: AIMessage | None) -> tuple[int | None, int | None, int | None]:
    """Read only allowlisted integer counts, never log arbitrary metadata."""
    if message is None:
        return None, None, None
    usage: Mapping[str, object]
    if message.usage_metadata:
        usage = message.usage_metadata
        keys = ("input_tokens", "output_tokens", "total_tokens")
    else:
        raw = message.response_metadata.get("token_usage", {})
        usage = raw if isinstance(raw, dict) else {}
        keys = ("prompt_tokens", "completion_tokens", "total_tokens")

    def count(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return count(keys[0]), count(keys[1]), count(keys[2])


class DeepSeekClient:
    """Thin shared client wrapping ChatDeepSeek's synchronous invoke interface."""

    def __init__(self, settings: Settings) -> None:
        """Build an SDK client without sending a request."""
        try:
            key = settings.require_api_key()
        except ValueError:
            raise LLMConfigurationError("Missing DEEPSEEK_API_KEY. Set it in the process environment or the project .env file (dot, not comma).") from None
        self.model_name = settings.deepseek_model
        try:
            root_client = DeepSeekRetryClient(api_key=key.get_secret_value(), base_url=str(settings.deepseek_base_url), timeout=settings.deepseek_timeout, max_retries=settings.deepseek_max_retries)
            self._llm = ChatDeepSeek(
                root_client=root_client, client=root_client.chat.completions,
                model=settings.deepseek_model,
                api_key=key,
                base_url=str(settings.deepseek_base_url),
                temperature=settings.deepseek_temperature,
                extra_body={"thinking": {"type": settings.deepseek_thinking}},
                timeout=settings.deepseek_timeout,
                max_retries=settings.deepseek_max_retries,
            )
        except Exception:
            raise LLMConfigurationError("Cannot initialize DeepSeek client. Check configuration and installed SDK versions.") from None

    @budgeted_call
    def invoke(self, prompt: LanguageModelInput) -> AIMessage:
        """Call DeepSeek, returning the original message and token metadata.

        Logs model, elapsed wall time (including SDK retries), status and available
        token counts. Prompts, responses, keys and raw errors are never logged.
        Async invocation can later follow the same policy using ChatDeepSeek.ainvoke.
        """
        started = perf_counter()
        message: AIMessage | None = None
        status = "success"
        try:
            message = self._llm.invoke(prompt)
            if not isinstance(message, AIMessage):
                message = None
                raise TypeError("Expected an AIMessage")
            _reject_truncated(message)
            return message
        except (RunStopped, BudgetPersistenceError) as error:
            status = type(error).__name__
            raise
        except Exception as error:
            translated = _translate_error(error)
            status = type(translated).__name__
            raise translated from None
        finally:
            self._log_call(message, started, status)

    def _log_call(self, message: AIMessage | None, started: float, status: str) -> None:
        """Use the same credential-safe telemetry for plain and structured calls."""
        input_tokens, output_tokens, total_tokens = _token_counts(message)
        report_token_usage(total_tokens if total_tokens is not None else input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None)
        elapsed = perf_counter() - started
        record_llm_call(LLMCallRecord(
            model_name=self.model_name, elapsed_seconds=elapsed, status=status,
            usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens),
        ))
        logger.info(
            "llm_call model=%s elapsed_seconds=%.3f status=%s input_tokens=%s output_tokens=%s total_tokens=%s",
            self.model_name, elapsed, status,
            input_tokens, output_tokens, total_tokens,
        )

    @budgeted_call
    def invoke_structured(self, prompt: LanguageModelInput, schema: type[OutputT]) -> OutputT:
        """Prefer LangChain tool-based output, retaining raw messages for telemetry.

        Parsing and explicit unsupported-feature errors are distinguished so an
        Agent may fall back to validated JSON. Transport/auth errors stay intact.
        """
        started = perf_counter()
        message: AIMessage | None = None
        status = "success"
        try:
            try:
                runnable = self._llm.with_structured_output(
                    schema, method="function_calling", include_raw=True,
                )
            except NotImplementedError:
                raise LLMStructuredOutputUnsupported("Structured output is not supported by this model interface.") from None
            result = runnable.invoke(prompt)
            if not isinstance(result, dict):
                raise LLMStructuredOutputError("Structured response did not match the expected envelope.")
            raw = result.get("raw")
            message = raw if isinstance(raw, AIMessage) else None
            if message is not None:
                _reject_truncated(message)
            if result.get("parsing_error") is not None or result.get("parsed") is None:
                raise LLMStructuredOutputError("Structured response did not satisfy the requested schema.")
            parsed = result["parsed"]
            try:
                return schema.model_validate(parsed)
            except ValidationError:
                raise LLMStructuredOutputError("Structured response did not satisfy the requested schema.") from None
        except (RunStopped, BudgetPersistenceError) as error:
            status = type(error).__name__
            raise
        except (LLMStructuredOutputError, LLMStructuredOutputUnsupported) as error:
            status = type(error).__name__
            raise
        except Exception as error:
            if isinstance(error, NotImplementedError) or _unsupported_structured_feature(error):
                translated: LLMError = LLMStructuredOutputUnsupported("Provider/model does not support the requested structured output feature.")
            else:
                translated = _translate_error(error)
            status = type(translated).__name__
            raise translated from None
        finally:
            self._log_call(message, started, status)


def get_llm(settings: Settings | None = None) -> DeepSeekClient:
    """Create a configured shared client; no request is made until invoke().

    Supplying Settings is useful for tests and applications with explicit paths.
    Otherwise the existing loader reads the current directory's .env.
    """
    try:
        resolved = settings if settings is not None else load_settings()
    except ValidationError:
        raise LLMConfigurationError("Invalid DeepSeek configuration. Check model, URL, temperature, timeout and retry count in .env/environment.") from None
    profile = current_model_profile()
    if profile is not None:
        resolved = Settings.model_validate({**resolved.model_dump(), **profile, "deepseek_api_key": resolved.deepseek_api_key})
    override = current_thinking_mode()
    if override is not None:
        resolved = resolved.model_copy(update={"deepseek_thinking": override})
    return DeepSeekClient(resolved)
