"""Closed same-origin read requests; no caller-controlled selector or upstream."""

import time

from .contracts import require
from .web_flow_graph import CORRELATION, PREFIX


async def route(graph, path, body, session):
    require(isinstance(body, dict), "invalid_input", 400)
    operation = path.removeprefix(PREFIX) if path.startswith(PREFIX) else ""
    require(operation in {"relationships", "traces", "trace"}, "not_found", 404)
    if operation == "relationships":
        require(body == {}, "invalid_input", 400)
        return await graph.relationships(session)
    if operation == "traces":
        require(
            set(body) == {"minutes"}
            and type(body["minutes"]) is int
            and body["minutes"] in {5, 15, 60},
            "invalid_input",
            400,
        )
        return await graph.traces(body["minutes"])
    require(set(body) == {"correlation_id", "start_ns", "end_ns"}, "invalid_input", 400)
    correlation = body["correlation_id"]
    require(
        isinstance(correlation, str) and CORRELATION.fullmatch(correlation), "invalid_input", 400
    )
    for key in ("start_ns", "end_ns"):
        require(
            isinstance(body[key], str)
            and body[key].isascii()
            and body[key].isdigit()
            and 1 <= len(body[key]) <= 20,
            "invalid_input",
            400,
        )
    start, end = int(body["start_ns"]), int(body["end_ns"])
    now = time.time_ns()
    require(
        0 < start < end <= now + 1_000_000_000
        and end - start <= 3600 * 1_000_000_000
        and start >= now - 24 * 3600 * 1_000_000_000,
        "invalid_input",
        400,
    )
    return await graph.trace(correlation, start, end)
