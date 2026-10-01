"""Thin web boundary using the existing authenticated console and its session proof."""

import asyncio
import hmac
from urllib.parse import urlsplit

from aiohttp import web

from ..contracts import loads, require
from ..web_console import WebConsole, CONSOLE_AUTH, AUTH_SUCCEEDED

PREFIX = "/api/web/relationships/"


class RelationshipConsole(WebConsole):
    async def route(self, request):
        if not request.path.startswith(PREFIX):
            return await super().route(request)
        require(self.config is not None, "web_not_configured", 503)
        require(
            request.host == urlsplit(self.config["origin"]).netloc
            and "Authorization" not in request.headers
            and request.headers.get("Sec-Fetch-Site", "same-origin") in {"same-origin", "none"},
            "forbidden",
            403,
        )
        require(request.method == "POST", "not_found", 404)
        require(request.headers.getall("Origin", []) == [self.config["origin"]], "forbidden", 403)
        _, session = self.session(request)
        require(
            session is not None
            and session["authenticated"]
            and await self.platform.local_work.run(self.session_valid, session),
            "session_expired",
            401,
        )
        csrf = request.headers.get("X-CSRF-Token", "")
        require(csrf.isascii() and hmac.compare_digest(csrf, session["csrf"]), "forbidden", 403)
        require(
            request.content_type == "application/json"
            and request.headers.get("Content-Encoding", "identity") == "identity",
            "invalid_input",
            400,
        )
        data = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.content.iter_chunked(4096):
                data.extend(chunk)
                require(len(data) <= 16384, "budget_exceeded", 413)
        body = loads(bytes(data))
        request[CONSOLE_AUTH] = AUTH_SUCCEEDED
        return web.json_response(
            await self.platform.relationships.route(
                self, request.path[len(PREFIX) :], body, session
            )
        )
