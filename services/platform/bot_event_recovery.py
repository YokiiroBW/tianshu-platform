"""Preserve the actual dispatch outcome without inventing a nonexecution receipt."""

from .transport import CoreFault


def retryable(error):
    return (
        getattr(error, "source_not_started", False)
        or isinstance(error, CoreFault)
        and error.document.get("execution_state") == "not_started"
        and error.document.get("retryable") is True
    ) and error.code in {"queue_full", "dependency_unavailable", "timeout"}


def public_state(state):
    if state == "retryable":
        return {"state": "not_started", "retryable": True}
    return {"state": state}
