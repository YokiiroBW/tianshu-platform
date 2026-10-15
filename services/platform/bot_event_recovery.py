"""Only a verified Core nonexecution receipt permits replay of an inbound event."""

from .transport import CoreFault


def retryable(error):
    return (
        isinstance(error, CoreFault)
        and error.document.get("execution_state") == "not_started"
        and error.document.get("retryable") is True
        and error.code in {"queue_full", "dependency_unavailable", "timeout"}
    )


def public_state(state):
    if state == "retryable":
        return {"state": "not_started", "retryable": True}
    return {"state": state}
