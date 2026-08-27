"""Safe provider error taxonomy exposed across runtime boundaries."""

from __future__ import annotations


class ProviderException(RuntimeError):
    """Base provider failure that never embeds an upstream response body."""

    code = "provider_error"

    def __init__(
        self,
        safe_message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.retryable = retryable
        self.retry_after = retry_after
        self.provider_request_id = provider_request_id


class AuthenticationError(ProviderException):
    code = "authentication_error"


class RateLimitError(ProviderException):
    code = "rate_limit_error"


class ContextLengthError(ProviderException):
    code = "context_length_error"


class InvalidRequestError(ProviderException):
    code = "invalid_request_error"


class ProviderUnavailable(ProviderException):
    code = "provider_unavailable"


ProviderUnavailableError = ProviderUnavailable


class ProviderTimeoutError(ProviderException):
    code = "provider_timeout"


class ProviderResponseError(ProviderException):
    code = "provider_response_error"


class UnsupportedCapabilityError(ProviderException):
    code = "unsupported_capability"


class StructuredOutputError(ProviderException):
    code = "structured_output_error"
