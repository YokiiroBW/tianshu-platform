"""Trusted application AssetLink reads. No HTTP route, ACL replica or asset cache."""

import asyncio
import copy
import re
import sqlite3
import ssl
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from . import diagnostics
from .asset_page_config import page_configuration
from .auth import secret
from .contracts import Fault, canonical, loads, require

OPERATIONS = {"libraries.list", "libraries.get", "entries.browse", "entries.get", "assets.search"}
REQUEST_LIMIT = 65_536
RESPONSE_LIMIT = 1_048_576
DEADLINE = 5


def validate_connections(settings, principals):
    """Validate the registered asset connections without a store, a client or a read.

    Extracted so the deployment-time question - "is this connection table usable?" - has exactly
    one answer: `Assets` asks it when it is assembled, and the rolling preflight asks the same
    function without constructing a client or opening a database.
    """
    connections = copy.deepcopy(settings.get("asset_connections", {}))
    require(isinstance(connections, dict), "invalid_input", 400)
    envs = [p["token_env"] for p in principals.values()]
    if settings.get("core"):
        envs.append(settings["core"]["token_env"])
    for connection in connections.values():
        require(
            isinstance(connection, dict)
            and set(connection) == {"endpoint", "ca_file", "token_env"},
            "invalid_input",
            400,
        )
        url = urlsplit(connection["endpoint"])
        require(
            url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and url.path == "/assetlink/v1/control"
            and not url.query
            and not url.fragment
            and not any(c.isspace() or ord(c) < 32 for c in connection["endpoint"]),
            "invalid_input",
            400,
        )
        require(Path(connection["ca_file"]).is_absolute(), "invalid_input", 400)
        require(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", connection["token_env"]),
            "invalid_input",
            400,
        )
        envs.append(connection["token_env"])
    require(len(envs) == len(set(envs)), "invalid_input", 400)
    return connections


def validate_page(page, principals, connections, contracts):
    """The read-only asset page's narrowing, refused where it is configured.

    The rule itself lives in the neutral `asset_page_config` module, which knows nothing about this
    client or about the page: an application client must not depend on one of its consumers, and
    the page adapter imports the same rule from the same place. This function only gives that rule
    the two facts it needs - and it needs no store to do it, which is what lets the preflight
    reach the same verdict as assembly.
    """
    if page is None:
        return None
    return page_configuration(page, principals, connections, contracts.check)


class Assets:
    def __init__(self, store, auth, settings):
        self.store, self.auth = store, auth
        self.connections = validate_connections(settings, auth.principals)
        # The variables that belong to *other* identities: this client never sends one of their
        # values, whichever name it was pointed at. A connection's own credential is deliberately
        # not in this list - it is the one value that connection is supposed to send - and the
        # uniqueness of all the names together is what the validator above already enforces.
        self.other_token_envs = tuple(
            [p["token_env"] for p in auth.principals.values()]
            + ([settings["core"]["token_env"]] if settings.get("core") else [])
        )

    def configure_page(self, page, contracts):
        """Validate the read-only asset page's narrowing at deployment time, not at read time.

        The page may only ever expose connections this identity is already bound to; an impossible
        page is refused here, where it is configured, rather than at read time.
        """
        return validate_page(page, self.auth.principals, self.connections, contracts)

    def authorize(self, header, connection_id):
        # A busy local authority must not consume another five seconds after HTTPS.
        with self.store.connect(write=True, timeout=0) as db:
            _, principal = self.auth.authenticate(header, db, "asset.read")
            require(connection_id in principal.get("asset_connections", []))
        require(connection_id in self.connections)
        return self.connections[connection_id]

    async def read(self, header, data):
        """Return only this call's result; cancellation propagates without a late result.

        Callers must replace their previous result on *every* return. On cancellation
        clear it in their finally handler. Local correlation exists before auth/parsing.
        """
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        upstream_id = None
        # Timed from the start, but an outbound event is only ever emitted once a call was really
        # attempted: a refusal that happens before any socket is opened is not an outbound result.
        span = diagnostics.Span("asset_read")
        attempted = False
        try:
            if isinstance(data, bytes):
                require(len(data) <= REQUEST_LIMIT, "budget_exceeded", 413)
                data = loads(data)
            require(
                isinstance(data, dict) and set(data) == {"connection_id", "operation", "body"},
                "invalid_input",
                400,
            )
            connection_id = data["connection_id"]
            require(isinstance(connection_id, str), "invalid_input", 400)
            connection = self.authorize(header, connection_id)
            operation, body = data["operation"], data["body"]
            require(isinstance(operation, str) and operation in OPERATIONS, "forbidden", 403)
            require(isinstance(body, dict), "invalid_input", 400)
            if "page_size" in body:
                require(
                    type(body["page_size"]) is int and 1 <= body["page_size"] <= 100,
                    "invalid_input",
                    400,
                )
            payload = canonical(
                {
                    "message_type": "control.request",
                    "request_id": request_id,
                    "operation": operation,
                    "body": body,
                    "timeout_ms": 5000,
                }
            ).encode("utf-8")
            require(len(payload) <= REQUEST_LIMIT, "budget_exceeded", 413)
            token = secret(connection["token_env"])
            require(token is not None, "dependency_unavailable", 503)
            # Distinct environment names alone do not prevent accidental shared values.
            require(
                all(token != secret(name) for name in self.other_token_envs),
                "dependency_unavailable",
                503,
            )
            require(
                all(
                    key == connection_id or token != secret(other["token_env"])
                    for key, other in self.connections.items()
                ),
                "dependency_unavailable",
                503,
            )
            tls = ssl.create_default_context(cafile=connection["ca_file"])
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            remaining = DEADLINE - (time.monotonic() - started)
            require(remaining > 0, "deadline_exceeded", 504)
            attempted = True
            diagnostics.outbound("started")
            async with asyncio.timeout(remaining):
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=remaining),
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    auto_decompress=False,
                ) as session:
                    async with session.post(
                        connection["endpoint"],
                        data=payload,
                        ssl=tls,
                        allow_redirects=False,
                        headers={
                            "Authorization": "Bearer " + token,
                            "Content-Type": "application/json",
                            "Accept-Encoding": "identity",
                            **diagnostics.correlation_header(),
                        },
                    ) as response:
                        require(
                            response.content_type == "application/json", "invalid_upstream", 502
                        )
                        require(
                            not response.headers.get("Content-Encoding"), "invalid_upstream", 502
                        )
                        require(
                            response.content_length is None
                            or response.content_length <= RESPONSE_LIMIT,
                            "budget_exceeded",
                            413,
                        )
                        chunks, size = [], 0
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            require(size <= RESPONSE_LIMIT, "budget_exceeded", 413)
                            chunks.append(chunk)
                        try:
                            result = loads(b"".join(chunks))
                        except Fault:
                            raise Fault("invalid_upstream", 502) from None
                        require(isinstance(result, dict), "invalid_upstream", 502)
                        upstream_id = result.get("request_id")
                        require(upstream_id in (request_id, "unknown"), "invalid_upstream", 502)
                        # Recheck local revocation and credential rotation before delivery.
                        self.authorize(header, connection_id)
                        require(secret(connection["token_env"]) == token, "unauthorized", 401)
                        await asyncio.sleep(0)
                        require(time.monotonic() - started < DEADLINE, "deadline_exceeded", 504)
                        if response.status != 200:
                            require(
                                result.get("message_type") == "error"
                                and isinstance(result.get("error"), dict),
                                "invalid_upstream",
                                502,
                            )
                            code = result["error"].get("code")
                            require(
                                isinstance(code, str)
                                and re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", code),
                                "invalid_upstream",
                                502,
                            )
                            require(
                                response.status in {400, 401, 403, 404, 409, 413, 429, 503, 504},
                                "invalid_upstream",
                                502,
                            )
                            raise Fault(code, response.status)
                        require(
                            upstream_id == request_id
                            and result.get("message_type") == "control.result"
                            and result.get("ok") is True
                            and isinstance(result.get("body"), dict),
                            "invalid_upstream",
                            502,
                        )
                        diagnostics.outbound("succeeded", duration_ms=span.elapsed() * 1000.0)
                        return {
                            "ok": True,
                            "request_id": request_id,
                            "upstream_request_id": upstream_id,
                            "status": 200,
                            "body": result["body"],
                            "representation": "index_metadata",
                            "original_available": "not_verified",
                        }
        except asyncio.CancelledError:
            # A cancelled read is reported as cancelled and nothing else: cancellation stops this
            # caller's wait, and no claim is made about what the peer did with the request.
            if attempted:
                diagnostics.outbound("cancelled", duration_ms=span.elapsed() * 1000.0)
            raise
        except TimeoutError:
            failure = Fault("deadline_exceeded", 504)
        except (aiohttp.ClientError, OSError, ssl.SSLError, sqlite3.Error):
            failure = Fault("dependency_unavailable", 503)
        except (ValueError, TypeError, RecursionError):
            failure = Fault("invalid_input", 400)
        except Fault as exc:
            failure = exc
        if attempted:
            if failure.code in ("deadline_exceeded", "timeout"):
                diagnostics.outbound(
                    "timed_out", duration_ms=span.elapsed() * 1000.0, error_code="timeout"
                )
            else:
                diagnostics.outbound(
                    "failed",
                    duration_ms=span.elapsed() * 1000.0,
                    error_code=diagnostics.safe_code(failure.code),
                )
        # Never echo an untrusted remote message, body, token or arbitrary correlation.
        return {
            "ok": False,
            "request_id": request_id,
            "upstream_request_id": upstream_id if upstream_id in (request_id, "unknown") else None,
            "status": failure.status,
            "code": failure.code,
            "body": None,
            "clear_previous": True,
            "retry": "manual",
        }
