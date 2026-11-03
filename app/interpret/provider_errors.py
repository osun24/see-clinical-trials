"""Sanitized provider diagnostics; no request prompts, headers, or keys."""
from openai import APITimeoutError, APIConnectionError, APIStatusError, RateLimitError
from app.models.errors import ServiceError


def provider_error(exc):
    body = getattr(exc, "body", {})
    body = body if isinstance(body, dict) else {}
    error = body.get("error", body)
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    detail = {"provider_code": code, "provider_type": error.get("type"),
              "request_id": getattr(exc, "request_id", None), "status": getattr(exc, "status_code", None)}
    if isinstance(exc, APITimeoutError):
        return ServiceError("llm_timeout", "The planning request timed out after bounded retries.", detail)
    if isinstance(exc, APIConnectionError):
        return ServiceError("llm_unavailable", "The planning provider could not be reached.", detail)
    if isinstance(exc, RateLimitError):
        if code in {"insufficient_quota", "billing_hard_limit_reached"}:
            return ServiceError("llm_quota_exceeded", "The configured API project has insufficient quota or billing allowance.", detail)
        return ServiceError("llm_rate_limited", "The planning provider rate limit was reached.", detail)
    if getattr(exc, "status_code", None) in {401, 403}:
        return ServiceError("llm_misconfigured", "The planning provider rejected authentication or permissions.", detail)
    if getattr(exc, "status_code", None) in {400, 422}:
        return ServiceError("llm_invalid_request", "The provider rejected the generated request or response schema.", detail)
    return ServiceError("llm_unavailable", "The planning provider returned an unsuccessful response.", detail)
