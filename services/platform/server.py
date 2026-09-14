"""Loopback rehearsal HTTP, exposing only published 1.0.0 operations."""

import asyncio
import ipaddress
import sqlite3
import uuid

from aiohttp import web

from .contracts import Fault, loads, require
from .web_console import WebConsole

PLATFORM = web.AppKey("platform", object)
BODY = web.RequestKey("body", dict)


def create_app(platform):
    console = WebConsole(platform)

    @web.middleware
    async def boundary(request, handler):
        request_id = "request:" + uuid.uuid4().hex
        try:
            if platform.auth.mode == "local_rehearsal":
                require(request.remote and ipaddress.ip_address(request.remote).is_loopback)
            else:
                require(request.secure)
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
            require(request.method == "POST" and schema is not None, "not_found", 404)
            platform.contracts.check(schema, body)
            request_id = body.get("query", body.get("command", body))["request_id"]
            request[BODY] = body
            response = await handler(request)
        except web.HTTPRequestEntityTooLarge:
            response = error("budget_exceeded", 413, request_id)
        except Fault as exc:
            response = error(exc.code, exc.status, request_id)
        except (sqlite3.Error, OSError):
            response = error("dependency_unavailable", 503, request_id)
        except (ValueError, TypeError, KeyError, RecursionError):
            response = error("invalid_input", 400, request_id)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return response

    def error(code, status, request_id):
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
    app.router.add_post("/internal/v1/source-access/read", source_access)
    app.router.add_post("/internal/v1/conversation/send", send)
    app.router.add_route("*", "/{path:.*}", console.handle)
    return app
