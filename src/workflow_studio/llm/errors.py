"""Application-facing LLM exceptions with credential-safe messages."""


class LLMError(Exception):
    """Base class for errors callers can handle without SDK dependencies."""


class LLMConfigurationError(LLMError, ValueError):
    """Missing credentials, invalid settings or client initialization failure."""


class LLMNetworkError(LLMError):
    """Connection, DNS, proxy or TLS failure after configured SDK retries."""


class LLMTimeoutError(LLMNetworkError):
    """The request timed out after configured SDK retries."""


class LLMAuthenticationError(LLMError):
    """The provider rejected credentials or denied access."""


class LLMRateLimitError(LLMError):
    """The provider rate limited the request after configured SDK retries."""


class LLMCallError(LLMError):
    """Other provider/model errors or an invalid response."""


class LLMStructuredOutputUnsupported(LLMCallError):
    """The configured provider/model explicitly does not support structured output."""


class LLMStructuredOutputError(LLMCallError):
    """A structured response was received but could not satisfy the schema."""


class LLMTruncatedOutputError(LLMCallError):
    """Provider reported incomplete output, even if a parsed object is available."""
