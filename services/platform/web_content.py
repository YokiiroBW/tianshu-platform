"""Session-bound transfer to Knowledge's authoritative private content service.

This adapter holds no second body store. Every descriptor, upload and original read
derives the current operator's real scope; Knowledge owns versions and access grants.
"""

import asyncio
import hashlib
import ssl
import uuid
from contextlib import asynccontextmanager

import aiohttp

from .auth import secret
from .contracts import Fault, loads, require

PREFIX = "/api/web/content/"
OWNER = "/internal/v1/knowledge/content/"
MAX_BYTES = 32 * 1024 * 1024
RESPONSES = {
    "uploads": "upload_response",
    "upload-status": "upload_response",
    "acquire": "acquire_response",
    "read": "read_response",
    "access": "access_response",
}


class WebContent:
    def __init__(self, platform, console):
        self.p, self.console = platform, console

    @asynccontextmanager
    async def principal(self, actor, request_id, session):
        async with self.console.life_management.scoped_actor(
            actor, session, require_identity=True
        ) as (origin, scope):
            require(
                scope["person_id"] is not None and scope["conversation_id"] is not None,
                "memory_identity_not_ready",
                503,
            )
            yield {
                "kind": "user",
                "query": {
                    "schema_version": 1,
                    "request_id": request_id,
                    "origin": {"assertion_ref": origin["assertion_ref"]},
                },
                "scope": scope,
            }

    def config(self):
        config = self.p.settings.get("web_knowledge")
        require(config is not None and config.get("enabled") is True, "dependency_unavailable", 503)
        token = secret(config["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        return config, token

    async def call(self, operation, payload=None, *, raw=None, headers=None, upload_id=None):
        require(operation in set(RESPONSES) | {"original", "upload"}, "invalid_input", 400)
        config, token = self.config()
        target = "uploads/" + upload_id if operation == "upload" else operation
        tls = ssl.create_default_context(cafile=config.get("ca_file"))
        try:
            async with (
                aiohttp.ClientSession(
                    trust_env=False, timeout=aiohttp.ClientTimeout(total=30)
                ) as client,
                client.request(
                    "PUT" if raw is not None else "POST",
                    config["base_url"].rstrip("/") + OWNER + target,
                    json=payload if raw is None else None,
                    data=raw,
                    headers={"Authorization": "Bearer " + token, **(headers or {})},
                    ssl=tls,
                    allow_redirects=False,
                ) as response,
            ):
                answer = bytearray()
                limit = MAX_BYTES if operation in {"original", "read"} else 1024 * 1024
                async for chunk in response.content.iter_chunked(65536):
                    answer.extend(chunk)
                    require(len(answer) <= limit, "budget_exceeded", 413)
                if response.status != 200:
                    doc = loads(bytes(answer))
                    code = doc.get("code") if isinstance(doc, dict) else None
                    require(
                        code
                        in {
                            "invalid_input",
                            "unauthorized",
                            "forbidden",
                            "not_found",
                            "stale_content",
                            "version_conflict",
                            "scope_changed",
                            "idempotency_conflict",
                            "source_too_large",
                            "budget_exceeded",
                            "unsupported_format",
                            "unsupported_range",
                            "timeout",
                            "dependency_unavailable",
                            "log_unavailable",
                            "queue_full",
                        },
                        "invalid_upstream",
                        502,
                    )
                    raise Fault(code, response.status)
                if operation == "original":
                    checksum = response.headers.get("X-Content-SHA256")
                    require(checksum == hashlib.sha256(answer).hexdigest(), "invalid_upstream", 502)
                    outgoing = {
                        key: response.headers[key]
                        for key in (
                            "Content-Type",
                            "X-Content-SHA256",
                            "X-Source-SHA256",
                            "X-Content-Version",
                            "X-Content-Coverage",
                        )
                        if key in response.headers
                    }
                    outgoing["Cache-Control"] = "no-store"
                    outgoing["X-Content-Type-Options"] = "nosniff"
                    return bytes(answer), outgoing
                result = loads(bytes(answer))
                self.p.contracts.check(
                    "knowledge-content#"
                    + ("upload_response" if operation == "upload" else RESPONSES[operation]),
                    result,
                )
                expected_id = (
                    headers["X-Tianshu-Request-Id"]
                    if operation == "upload"
                    else payload["principal"]["query"]["request_id"]
                )
                require(result["request_id"] == expected_id, "invalid_upstream", 502)
                return result
        except (aiohttp.ClientError, TimeoutError, OSError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None

    async def route(self, operation, body, session):
        require(
            operation in set(RESPONSES) | {"original"} and isinstance(body, dict), "not_found", 404
        )
        require(
            set(body) == {"actor_id", "client_id", "value"} and isinstance(body["value"], dict),
            "invalid_input",
            400,
        )
        require(
            isinstance(body["client_id"], str) and 1 <= len(body["client_id"]) <= 128,
            "invalid_input",
            400,
        )
        async with self.principal(body["actor_id"], body["client_id"], session) as principal:
            require("principal" not in body["value"], "invalid_input", 400)
            payload = {**body["value"], "principal": principal}
            schema = {"uploads": "upload_request", "upload-status": "upload_status_request"}.get(
                operation, operation + "_request"
            )
            self.p.contracts.check("knowledge-content#" + schema, payload)
            return await self.call(operation, payload)

    async def upload(self, request, session):
        require(
            request.content_type == "application/octet-stream"
            and request.headers.get("Content-Encoding", "identity") == "identity",
            "invalid_input",
            415,
        )
        upload_id = request.path[len(PREFIX + "upload/") :]
        require(1 <= len(upload_id) <= 128 and "/" not in upload_id, "invalid_input", 400)
        actor = request.headers.get("X-Tianshu-Actor-Id", "")
        rid = request.headers.get("X-Tianshu-Request-Id", "upload:" + uuid.uuid4().hex)
        async with self.principal(actor, rid, session) as principal:
            status = await self.call(
                "upload-status", {"upload_id": upload_id, "principal": principal}
            )
            length = request.headers.get("Content-Length")
            require(
                length is None or length.isdigit() and int(length) == status["size"],
                "invalid_input",
                400,
            )
            data = bytearray()
            async with asyncio.timeout(30):
                async for chunk in request.content.iter_chunked(65536):
                    data.extend(chunk)
                    require(len(data) <= min(status["size"], MAX_BYTES), "source_too_large", 413)
            require(
                len(data) == status["size"]
                and hashlib.sha256(data).hexdigest() == status["sha256"],
                "invalid_input",
                400,
            )
            require(
                await self.p.local_work.run(self.console.session_valid, session),
                "session_expired",
                401,
            )
            return await self.call(
                "upload",
                raw=bytes(data),
                upload_id=upload_id,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Tianshu-Request-Id": rid,
                    "X-Tianshu-Assertion-Ref": principal["query"]["origin"]["assertion_ref"],
                },
            )
