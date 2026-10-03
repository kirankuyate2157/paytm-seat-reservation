from typing import Any


class ApiError(Exception):
    """Domain/HTTP error rendered in the standard error envelope."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None, reason: str | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.reason = reason


class Decline(ApiError):
    """Clean 4xx domain outcome of a reservation attempt (never a 5xx)."""

    def __init__(self, reason: str, message: str, status: int = 409, details: dict | None = None):
        super().__init__(status, "RESERVATION_DECLINED", message, details, reason)


def envelope(err: ApiError, correlation_id: str) -> dict[str, Any]:
    error: dict[str, Any] = {"code": err.code, "message": err.message, "details": err.details}
    if err.reason:
        error["reason"] = err.reason
    return {"error": error, "correlation_id": correlation_id}
