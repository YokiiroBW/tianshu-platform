"""Trusted operator forwarding to Companion's original learning recovery owner."""

from .contracts import Fault, require
from .transport import management_call

PATH = "/internal/v1/memory/learning/recover"
FIELDS = frozenset(
    {
        "actor_id",
        "job_id",
        "request_id",
        "expected_attempts",
        "expected_failure_count",
        "model_request_id",
    }
)


def authorize(platform, header, body):
    segment_fields = (FIELDS - {"job_id"}) | {"segment_id"}
    require(isinstance(body, dict) and set(body) in (FIELDS, segment_fields), "invalid_input", 400)
    selector = "segment_id" if "segment_id" in body else "job_id"
    for field in ("actor_id", selector, "request_id"):
        platform.contracts.check("common#id", body[field])
    require(selector != "segment_id" or body["model_request_id"] is None, "invalid_input", 400)
    if body["model_request_id"] is not None:
        platform.contracts.check("common#id", body["model_request_id"])
    for field in ("expected_attempts", "expected_failure_count"):
        require(type(body[field]) is int and body[field] >= 0, "invalid_input", 400)
    with platform.store.connect() as db:
        operator, _ = platform.auth.authenticate(header, db, "role.manage", operator=True)
    role = platform.role_runtime.get(body["actor_id"])
    require(
        role is not None
        and role.get("operator") == operator
        and platform.role_runtime.active(body["actor_id"])
        and "memory.write" in role.get("capabilities", []),
        "forbidden",
        403,
    )


async def recover(platform, header, body, *, started):
    await platform.local_work.run(authorize, platform, header, body)
    require(platform.settings.get("core") is not None, "dependency_unavailable", 503)
    # A lost response cannot prove that Companion did not record the command.
    # No local retry: the operator repeats the identical original request_id.
    started.set()
    result = await management_call(
        platform.settings["core"],
        PATH,
        body,
        session_factory=platform.client_factories["companion"],
    )
    fields = {"request_id", "job_id", "state", "attempts", "failure_count", "model_request_id"}
    selector = "segment_id" if "segment_id" in body else "job_id"
    if selector == "segment_id":
        fields.add("segment_id")
    require(
        set(result) == fields
        and all(
            result[field] == body[field] for field in ("request_id", selector, "model_request_id")
        )
        and result["state"] in {"resumed", "completed", "invalidated"}
        and all(
            type(result[field]) is int and result[field] >= 0
            for field in ("attempts", "failure_count")
        ),
        "dependency_unavailable",
        503,
    )
    try:
        platform.contracts.check("common#id", result["job_id"])
    except Fault:
        raise Fault("dependency_unavailable", 503) from None
    return result
