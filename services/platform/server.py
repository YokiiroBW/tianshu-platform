"""Loopback rehearsal HTTP, exposing only published 1.0.0 operations."""

import asyncio
import hmac
import ipaddress
import os
import sqlite3
import time
import uuid

from aiohttp import web

from . import diagnostics, diagnostics_config, runtime_health
from .contracts import Fault, loads, require
from .web_console import WebConsole

PLATFORM = web.AppKey("platform", object)
BODY = web.RequestKey("body", dict)
NATIVE_SNAPSHOT = "/internal/v1/model-config/native/snapshot"
# The two probes are recognised here, inside the serving boundary and ahead of the static
# dispatcher, so neither can ever fall through to the single-page application.
LIVE_PATH = "/health/live"
READY_PATH = "/health/ready"
PROBE_PATHS = frozenset({LIVE_PATH, READY_PATH})
# model-protocol/v1 owns its envelope and one HTTP status per code; local codes are translated.
NATIVE_ERRORS = {
    "invalid_input": (400, "invalid_input"),
    "budget_exceeded": (413, "payload_too_large"),
    "payload_too_large": (413, "payload_too_large"),
    "unauthorized": (401, "unauthorized"),
    "forbidden": (403, "forbidden"),
    "not_found": (404, "not_found"),
    "version_conflict": (409, "version_conflict"),
    "idempotency_conflict": (409, "idempotency_conflict"),
    "state_reference_unsupported": (409, "state_reference_unsupported"),
    "unsupported_operation": (501, "unsupported_operation"),
    "unsupported_version": (400, "unsupported_version"),
    "queue_full": (429, "queue_full"),
    "dependency_unavailable": (503, "dependency_unavailable"),
    "timeout": (408, "timeout"),
    "result_unknown": (502, "result_unknown"),
}


def create_app(platform, probe=None):
    console = WebConsole(platform)
    native_open = platform.native_config_http is True
    health = probe if probe is not None else runtime_health.Probe(platform.health)
    ready_token_env = diagnostics_config.resolve_ready_token_env(platform.settings)

    def sealed(response):
        # Every answer this boundary produces is uncacheable and never sniffed.
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return response

    async def probe_endpoint(request):
        """Answer one probe without admitting it, logging it or writing anything at all.

        The readiness credential is read from the environment on every request, so a rotation
        takes effect immediately and never depends on a value cached at startup. A server with no
        configured token says so; a caller with a missing or wrong one gets 401. The body is the
        closed document plus, for a failure, one fixed code - never a path or a configured value.
        """
        require(request.method in {"GET", "HEAD"}, "not_found", 404)
        if request.path == LIVE_PATH:
            return sealed(web.json_response(health.live()))
        token = os.environ.get(ready_token_env)
        if token is None:
            return sealed(web.json_response({"code": "dependency_unavailable"}, status=503))
        supplied = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not supplied.startswith(prefix) or not hmac.compare_digest(
            supplied[len(prefix) :], token
        ):
            return sealed(web.json_response({"code": "unauthorized"}, status=401))
        document = await health.ready()
        status = 200 if document["status"] == "ready" else 503
        return sealed(web.json_response(document, status=status))

    @web.middleware
    async def boundary(request, handler):
        # A closed native port stays undiscoverable: it answers with the contract's own opaque 404.
        native = request.path == NATIVE_SNAPSHOT
        request_id = "request:" + uuid.uuid4().hex
        correlation = None
        context = None
        admitted = False
        unexpected = False
        started = time.monotonic()

        async def dispatch(request):
            nonlocal request_id
            if not request.path.startswith("/internal/"):
                return await console.handle(request)
            # Browser sessions never authorize service RPCs.
            require("Origin" not in request.headers and "Cookie" not in request.headers)
            require(len(request.headers.getall("Authorization", [])) == 1, "unauthorized", 401)
            require(request.content_type == "application/json", "invalid_input", 400)
            require(
                request.headers.get("Content-Encoding", "identity") == "identity",
                "invalid_input",
                400,
            )
            try:
                async with asyncio.timeout(5):
                    raw = await request.read()
            except TimeoutError:
                raise Fault("timeout", 408) from None
            body = loads(raw)
            require(isinstance(body, dict), "invalid_input", 400)
            # Correlation is adopted only after schema validation; malformed input stays opaque.
            schema = {
                "/internal/v1/origins/resolve": "common#origin_resolve_request",
                "/internal/v1/model-config/snapshot": "model#config_request",
                "/internal/v1/conversation/send": "conversation#send_request",
            }.get(request.path)
            if request.path == "/internal/v1/source-access/read":
                schema = {
                    "input": "sources#input_access_request",
                    "current": "sources#current_access_request",
                }.get(body.get("operation"))
            if native and native_open:
                schema = "model-protocol#config_request"
            require(request.method == "POST" and schema is not None, "not_found", 404)
            platform.contracts.check(schema, body)
            request_id = body.get("query", body.get("command", body))["request_id"]
            request[BODY] = body
            return await handler(request)

        try:
            if platform.auth.mode == "local_rehearsal":
                require(request.remote and ipaddress.ip_address(request.remote).is_loopback)
            else:
                require(request.secure)
            if request.path in PROBE_PATHS:
                # The explicit read-only exception to full logging: a probe is never admitted,
                # never emits an event and never causes a write of any kind.
                return await probe_endpoint(request)
            correlation = diagnostics.adopt_correlation(request.headers.get(diagnostics.HEADER))
            if not diagnostics.admittable():
                # New business is refused while the log cannot be written. Work already in flight
                # keeps its own result; only the next request is turned away.
                response = error("dependency_unavailable", 503, request_id, native)
            else:
                context = diagnostics.use_correlation(correlation)
                admitted = diagnostics.accept(correlation)
                if admitted:
                    response = await dispatch(request)
                else:
                    response = error("dependency_unavailable", 503, request_id, native)
        except web.HTTPRequestEntityTooLarge:
            response = error("budget_exceeded", 413, request_id, native)
        except Fault as exc:
            response = error(exc.code, exc.status, request_id, native)
        except (sqlite3.Error, OSError):
            response = error("dependency_unavailable", 503, request_id, native)
        except (ValueError, TypeError, KeyError, RecursionError):
            response = error("invalid_input", 400, request_id, native)
        except Exception:
            # An exception this product did not anticipate is still answered with a code the
            # published error contract allows, and recorded as the one fixed internal code: no
            # exception text, type or traceback reaches either the wire or the log.
            unexpected = True
            response = error("dependency_unavailable", 503, request_id, native)
        except asyncio.CancelledError:
            # Cancellation is this request's terminal state, so it is recorded here and only here:
            # one place owns request lifecycle, and a cancelled request never produces a response.
            # The emit does not await, so it is safe on a task that is being torn down.
            if admitted and correlation is not None:
                diagnostics.event(
                    "http.request.finished",
                    "INFO",
                    "cancelled",
                    correlation_id=correlation,
                    duration_ms=max(0.0, (time.monotonic() - started) * 1000.0),
                )
            raise
        finally:
            if context is not None:
                diagnostics.reset_correlation(context)
        if admitted and correlation is not None:
            diagnostics.finish(
                response.status,
                max(0.0, (time.monotonic() - started) * 1000.0),
                correlation,
                error_code="internal_error" if unexpected else None,
            )
        sealed(response)
        if correlation is not None:
            response.headers[diagnostics.HEADER] = correlation
        return response

    def error(code, status, request_id, native):
        if native:
            # The native contract fixes both the code set and one status per code.
            status, code = NATIVE_ERRORS.get(code, (503, "dependency_unavailable"))
            body = {
                "contract": "model-protocol/v1",
                "schema_version": 1,
                "request_id": request_id,
                "code": code,
                "execution_state": "not_started",
                "retryable": False,
            }
            platform.contracts.check("model-protocol#error", body)
            return web.json_response(body, status=status)
        body = {
            "schema_version": 1,
            "request_id": request_id,
            "code": code,
            "execution_state": "not_started",
            "retryable": status == 503,
        }
        platform.contracts.check("common#error", body)
        return web.json_response(body, status=status)

    async def resolve(request):
        return web.json_response(
            await asyncio.to_thread(
                platform.origins.resolve, request.headers["Authorization"], request[BODY]
            )
        )

    async def snapshot(request):
        return web.json_response(
            await asyncio.to_thread(
                platform.models.snapshot, request.headers["Authorization"], request[BODY]
            )
        )

    async def native_snapshot(request):
        return web.json_response(
            await asyncio.to_thread(
                platform.models.native_snapshot, request.headers["Authorization"], request[BODY]
            )
        )

    async def source_access(request):
        return web.json_response(
            await asyncio.to_thread(
                platform.sources.read, request.headers["Authorization"], request[BODY]
            )
        )

    async def send(request):
        require(console.config is not None, "dependency_unavailable", 503)
        return web.json_response(
            await asyncio.to_thread(
                console.sender.send, request.headers["Authorization"], request[BODY]
            )
        )

    app = web.Application(middlewares=[boundary], client_max_size=1_048_576)
    app[PLATFORM] = platform
    app.router.add_post("/internal/v1/origins/resolve", resolve)
    app.router.add_post("/internal/v1/model-config/snapshot", snapshot)
    if native_open:
        # Default closed; only an explicit deployment setting registers the native port.
        app.router.add_post(NATIVE_SNAPSHOT, native_snapshot)
    app.router.add_post("/internal/v1/source-access/read", source_access)
    app.router.add_post("/internal/v1/conversation/send", send)
    app.router.add_route("*", "/{path:.*}", console.handle)
    return app
