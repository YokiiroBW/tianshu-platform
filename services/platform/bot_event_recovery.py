"""Only a verified Core nonexecution receipt permits replay of an inbound event."""

import uuid

from .contracts import digest
from .transport import CoreFault, core_web_call


async def confirmed(platform, data, actors, key):
    """Only a positive, exact Core receipt allows replay to repair mapping/ack.

    Core's existing durable idempotency key returns the same admission without
    another collection/model call. A missing/unavailable receipt stays unknown.
    """
    response = await core_web_call(
        platform.settings.get("core"),
        "ingest-receipt",
        {
            "schema_version": 1,
            "request_id": "receipt:" + uuid.uuid4().hex,
            "idempotency_key": key,
            "semantic_digest": digest(
                {"schema_version": 1, "input": data, "target_actor_ids": actors}
            ),
        },
        platform.contracts,
        None,
    )
    return response["found"]


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
