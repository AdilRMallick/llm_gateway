from enum import StrEnum


class ErrorKind(StrEnum):
    timeout = "timeout"
    connection = "connection"
    server_error = "server_error"  # 5xx
    rate_limited = "rate_limited"  # 429
    client_error = "client_error"  # 4xx that is our fault
    bad_response = "bad_response"  # 2xx we could not parse


# Retrying a 400 just burns latency: the request is malformed and will stay
# malformed. Everything else is a transient condition worth another attempt,
# either on the same provider or the next one.
RETRYABLE = {
    ErrorKind.timeout,
    ErrorKind.connection,
    ErrorKind.server_error,
    ErrorKind.rate_limited,
    ErrorKind.bad_response,
}


class ProviderError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        http_status: int | None = None,
        retry_after_s: float | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def __str__(self) -> str:  # shows up in failover_reason
        status = f" http={self.http_status}" if self.http_status else ""
        return f"{self.kind}{status}: {self.message}"


class NoProviderAvailable(Exception):
    """Every provider in the policy order was tried and none succeeded."""

    def __init__(self, message: str, attempts: list):
        super().__init__(message)
        self.attempts = attempts
