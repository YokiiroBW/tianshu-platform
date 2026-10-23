"""Closed actor wire errors without changing existing Platform error contracts."""

from aiohttp import web

PATH = "/internal/v1/actor-capabilities/operate"
EXECUTION = web.RequestKey("actor_capability_execution", str)


def error_body(code, status, request_id, execution="not_started"):
    aliases = {
        "permission_denied": "forbidden",
        "session_expired": "unauthorized",
        "revision_conflict": "version_conflict",
        "conflict": "version_conflict",
        "budget_exceeded": "capacity_exceeded",
        "too_many_requests": "capacity_exceeded",
        "instance_disabled": "capability_unavailable",
        "outcome_unknown": "unknown",
        "operation_in_progress": "capability_unavailable",
    }
    allowed = {
        "invalid_input",
        "unauthorized",
        "forbidden",
        "not_found",
        "version_conflict",
        "idempotency_conflict",
        "dependency_unavailable",
        "capability_unavailable",
        "capacity_exceeded",
        "unknown",
    }
    code = aliases.get(code, code)
    if code not in allowed:
        code, status = "dependency_unavailable", 503
    if code == "unknown":
        execution = "unknown"
    return {
        "schema_version": 1,
        "request_id": request_id,
        "code": code,
        "execution": execution,
        "retryable": False,
    }, status
