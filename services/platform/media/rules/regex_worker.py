"""Regex worker: the only process that ever compiles or runs a user-supplied expression.

This file is started by :mod:`services.platform.media.rules.regex_process` as
``sys.executable -I -u <absolute path to this file>``. It is deliberately the smallest possible
program:

* it imports the standard library only (``json``, ``re``, ``sys``) — no platform configuration, no
  server package, no network client, no filesystem access, no clock;
* it speaks one JSON object per line on stdin/stdout and never writes source text anywhere else;
* its only secrets are the ones handed to it on that pipe, and it holds no compiled pattern after a
  ``forget`` command.

The hard budgets (3 s handshake, 50 ms per call, 5 s per request) belong to the parent, which kills
this process when one of them expires: ``re`` cannot be interrupted from inside, so the parent's
``terminate``/``kill`` *is* the timeout mechanism.

Protocol (one response line per request line, ``seq`` echoed verbatim):

* ``{"seq": N, "op": "compile", "pattern": P, "flags": F}`` →
  ``{"seq": N, "ok": true, "handle": N}`` or ``{"seq": N, "ok": false, "code": "invalid_regex"}``
* ``{"seq": N, "op": "search", "handle": H, "text": T}`` →
  ``{"seq": N, "ok": true, "matched": bool}`` or ``{"seq": N, "ok": false, "code": "unknown_handle"}``
* ``{"seq": N, "op": "forget", "handle": H}`` → ``{"seq": N, "ok": true}``

A malformed line is refused with ``{"seq": null, "ok": false, "code": "invalid_request"}`` and never
echoes its content. Closing stdin ends the loop and the process exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys

PATTERN_MAX = 512
FLAGS_MAX = 0x7FFFFFFF


def _send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _refuse(seq: object, code: str) -> None:
    _send({"seq": seq, "ok": False, "code": code})


def _compile(command: dict[str, object], seq: object, cache: dict[int, re.Pattern[str]]) -> None:
    pattern = command.get("pattern")
    flags = command.get("flags")
    handle = command.get("seq")
    if not isinstance(pattern, str) or len(pattern) > PATTERN_MAX:
        _refuse(seq, "invalid_request")
        return
    if type(flags) is not int or not 0 <= flags <= FLAGS_MAX:
        _refuse(seq, "invalid_request")
        return
    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        # The expression itself is never echoed: the caller only needs to know that it is refused.
        _refuse(seq, "invalid_regex")
        return
    if type(handle) is not int:
        _refuse(seq, "invalid_request")
        return
    cache[handle] = compiled
    _send({"seq": seq, "ok": True, "handle": handle})


def _search(command: dict[str, object], seq: object, cache: dict[int, re.Pattern[str]]) -> None:
    handle = command.get("handle")
    text = command.get("text")
    if type(handle) is not int or not isinstance(text, str):
        _refuse(seq, "invalid_request")
        return
    compiled = cache.get(handle)
    if compiled is None:
        _refuse(seq, "unknown_handle")
        return
    _send({"seq": seq, "ok": True, "matched": compiled.search(text) is not None})


def _forget(command: dict[str, object], seq: object, cache: dict[int, re.Pattern[str]]) -> None:
    handle = command.get("handle")
    if type(handle) is not int:
        _refuse(seq, "invalid_request")
        return
    cache.pop(handle, None)
    _send({"seq": seq, "ok": True})


def handle_line(line: str, cache: dict[int, re.Pattern[str]]) -> None:
    try:
        command = json.loads(line)
    except ValueError:
        _refuse(None, "invalid_request")
        return
    if not isinstance(command, dict):
        _refuse(None, "invalid_request")
        return
    seq = command.get("seq")
    operation = command.get("op")
    if operation == "compile":
        _compile(command, seq, cache)
    elif operation == "search":
        _search(command, seq, cache)
    elif operation == "forget":
        _forget(command, seq, cache)
    else:
        _refuse(seq, "invalid_request")


def main() -> int:
    cache: dict[int, re.Pattern[str]] = {}
    _send({"ready": True, "protocol": 1, "pid": os.getpid()})
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        handle_line(line, cache)


if __name__ == "__main__":  # pragma: no cover - exercised as a real subprocess
    sys.exit(main())
