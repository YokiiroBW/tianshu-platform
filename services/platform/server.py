"""Loopback rehearsal HTTP, exposing only published 1.0.0 operations."""

import asyncio
import hmac
import ipaddress
import os
import sqlite3
import time
import uuid

from aiohttp import web

from . import diagnostics, diagnostics_config, model_origin_renewal, runtime_health
from .bot_delivery import PREFIX as DELIVERY_PREFIX
from .bot_delivery import ROUTES as DELIVERY_ROUTES
from .contracts import Fault, loads, require
from .memory_proofs import ISSUE as PROOF_ISSUE
from .memory_proofs import VERIFY as PROOF_VERIFY
from .qq_admin import CHECK_PATH as QQ_ADMIN_CHECK
from .service import registered_credentials
from .service_credentials import PATH as SERVICE_CREDENTIALS
from .web_console import CONSOLE_AUTH

PLATFORM = web.AppKey("platform", object)
BODY = web.RequestKey("body", dict)
NATIVE_SNAPSHOT = "/internal/v1/model-config/native/snapshot"
PROVIDER_SELECT = "/internal/v1/provider-self-service/select"
PROVIDER_RUNTIME = "/internal/v1/provider-self-service/runtime"
BOT_PATHS = frozenset(
    {
        "/internal/v1/bot/events",
        "/internal/v1/bot/events/status",
        "/internal/v1/bot/heartbeat",
        "/internal/v1/bot/replies/claim",
        "/internal/v1/bot/replies/status",
        "/internal/v1/bot/replies/ack",
    }
)
REPLY_STATUS = "/internal/v1/conversation/reply-status"
OBSERVATION_VERIFY = "/internal/v2/observation-source/verify"
OBSERVATION_ADMIN_STATUS = "/internal/v2/observation-admin/status"
OBSERVATION_ADMIN_ENROLL = "/internal/v2/observation-admin/enroll-default"
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


def create_console(platform):
    from .web_console import WebConsole

    return WebConsole(platform)


def create_app(platform, probe=None, *, console=None, public=False):
    console = console or create_console(platform)
    require(not public or console.access.current is not None, "invalid_input", 400)
    native_open = platform.native_config_http is True
    renewal_open = model_origin_renewal.enabled(platform.settings)
    health = probe if probe is not None else runtime_health.Probe(platform.health)
    ready_token_env = diagnostics_config.resolve_ready_token_env(platform.settings)
    # Every variable another registered identity or peer reads. The names are static deployment
    # configuration, but their *values* are read per request: a rotation must be seen immediately,
    # and a value that has become the same as a business credential must stop being accepted.
    business_names = registered_credentials(platform.settings)

    def sealed(response):
        # Every answer this boundary produces is uncacheable and never sniffed.
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return response

    def ready_token_state():
        """The readiness credential as it is *now*: `(value, problem)`.

        `problem` is a fixed code, never the value and never a path. A credential that is absent,
        empty, blank or currently equal to a registered business credential is a server-side
        configuration fault and answers 503; a caller that simply cannot present it answers 401.
        The comparison is over bytes because `hmac.compare_digest` refuses non-ASCII text, and a
        probe carrying a Unicode token must get the same opaque refusal as any other wrong one.
        """
        value = os.environ.get(ready_token_env)
        if not value or not value.strip():
            return None, "dependency_unavailable"
        candidate = value.encode("utf-8")
        for name in business_names:
            other = os.environ.get(name)
            if other and hmac.compare_digest(candidate, other.encode("utf-8")):
                # Reusing an identity's own secret as the probe credential would let anyone who
                # holds that identity's token read the readiness document as the probe.
                return None, "dependency_unavailable"
        return value, None

    async def probe_endpoint(request):
        """Answer one probe without admitting it, logging it or writing anything at all.

        The readiness credential is read from the environment on every request, so a rotation
        takes effect immediately and never depends on a value cached at startup. A server with no
        usable token says so; a caller with a missing or wrong one gets 401. The body is the
        closed document plus, for a failure, one fixed code - never a path or a configured value.
        """
        require(request.method in {"GET", "HEAD"}, "not_found", 404)
        if request.path == LIVE_PATH:
            return sealed(web.json_response(health.live()))
        token, problem = ready_token_state()
        if problem is not None:
            return sealed(web.json_response({"code": problem}, status=503))
        supplied = request.headers.get("Authorization", "")
        prefix = "Bearer "
        presented = supplied[len(prefix) :] if supplied.startswith(prefix) else ""
        if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), token.encode("utf-8")
        ):
            return sealed(web.json_response({"code": "unauthorized"}, status=401))
        document = await health.ready()
        status = 200 if document["status"] == "ready" else 503
        return sealed(web.json_response(document, status=status))

    @web.middleware
    async def boundary(request, handler):
        # A closed native port stays undiscoverable: it answers with the contract's own opaque 404.
        native = request.path == NATIVE_SNAPSHOT
        web_error = request.path.startswith("/api/web/")
        internal = request.path.startswith("/internal/")
        request_id = "request:" + uuid.uuid4().hex
        correlation = None
        context = None
        admitted = False
        unexpected = False
        # What this exchange actually demonstrates about authorisation. It starts as "nothing" and
        # is only ever set by the two facts that are real evidence: a credential was presented and
        # the request got past the code that checks it, or the answer refused one.
        auth = diagnostics.AUTH_NOT_ATTEMPTED
        reached_handler = False
        started = time.monotonic()

        async def dispatch(request):
            nonlocal request_id, reached_handler
            if not request.path.startswith("/internal/"):
                require(public or console.access.current is None, "not_found", 404)
                reached_handler = True
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
                    limit = (
                        45 * 1024 * 1024
                        if request.path == DELIVERY_PREFIX + "send"
                        else 1024 * 1024
                    )
                    if request.path == DELIVERY_PREFIX + "send":
                        with platform.store.connect() as db:
                            _, principal = platform.auth.authenticate(
                                request.headers["Authorization"], db, "dialogue.send"
                            )
                            require(
                                principal["kind"] == "service"
                                and principal["service"] == "companion"
                            )
                    chunks = bytearray()
                    async for chunk in request.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        require(len(chunks) <= limit, "budget_exceeded", 413)
                    raw = bytes(chunks)
            except TimeoutError:
                raise Fault("timeout", 408) from None
            body = loads(raw)
            require(isinstance(body, dict), "invalid_input", 400)
            # Correlation is adopted only after schema validation; malformed input stays opaque.
            schema = {
                "/internal/v1/origins/resolve": "common#origin_resolve_request",
                "/internal/v1/model-config/snapshot": "model#config_request",
                "/internal/v1/conversation/send": "conversation#send_request",
                REPLY_STATUS: "conversation#send_request",
                PROOF_ISSUE: "memory-context#issue_request",
                PROOF_VERIFY: "memory-context#proof_request",
            }.get(request.path)
            delivery_call = request.path in DELIVERY_ROUTES and hasattr(platform.bots, "delivery")
            if delivery_call:
                operation = request.path[len(DELIVERY_PREFIX) :]
                schema = (
                    "bot-delivery#"
                    + {
                        "send": "send_request",
                        "query": "lookup_request",
                        "cancel": "lookup_request",
                        "finalize": "finalize_request",
                        "context": "context_request",
                    }[operation]
                )
            if request.path == "/internal/v1/source-access/read":
                schema = {
                    "input": "sources#input_access_request",
                    "current": "sources#current_access_request",
                }.get(body.get("operation"))
            if native and native_open:
                schema = "model-protocol#config_request"
            renewal = request.path == model_origin_renewal.PATH and renewal_open
            provider_call = platform.provider_catalog is not None and request.path in {
                PROVIDER_SELECT,
                PROVIDER_RUNTIME,
            }
            bot_call = platform.bots.config is not None and request.path in BOT_PATHS
            observation_call = platform.bot_observation.catalog is not None and request.path in {
                OBSERVATION_VERIFY,
                OBSERVATION_ADMIN_STATUS,
                OBSERVATION_ADMIN_ENROLL,
            }
            qq_admin_call = request.path == QQ_ADMIN_CHECK
            credential_call = request.path == SERVICE_CREDENTIALS
            require(
                request.method == "POST"
                and (
                    schema is not None
                    or renewal
                    or provider_call
                    or bot_call
                    or observation_call
                    or qq_admin_call
                    or credential_call
                ),
                "not_found",
                404,
            )
            if renewal:
                require(len(raw) <= 4096, "budget_exceeded", 413)
                model_origin_renewal.validate_request(body)
            elif bot_call:
                require(
                    len(raw) <= (65536 if request.path == "/internal/v1/bot/events" else 4096),
                    "budget_exceeded",
                    413,
                )
            elif observation_call:
                require(
                    len(raw) <= (32768 if request.path == OBSERVATION_ADMIN_ENROLL else 4096),
                    "budget_exceeded",
                    413,
                )
            elif provider_call:
                require(len(raw) <= 4096, "budget_exceeded", 413)
            elif qq_admin_call:
                require(len(raw) <= 4096, "budget_exceeded", 413)
            elif credential_call:
                require(len(raw) <= 4096, "budget_exceeded", 413)
                check = (
                    platform.contracts.check_skill_credential
                    if body.get("purpose") == "companion.skills"
                    else platform.contracts.check_image_credential
                )
                check("request", body)
            else:
                platform.contracts.check(schema, body)
            if not provider_call and not bot_call:
                request_id = body.get("query", body.get("command", body)).get(
                    "request_id", request_id
                )
            request[BODY] = body
            # From here the handler authenticates the presented credential itself; everything
            # before this line is a pre-authentication refusal that proves nothing about identity.
            reached_handler = True
            return await handler(request)

        try:
            if public:
                require(
                    not request.path.startswith("/internal/") and request.path not in PROBE_PATHS,
                    "not_found",
                    404,
                )
                if console.access.current["mode"] == "https":
                    require(request.secure)
            elif platform.auth.mode == "local_rehearsal":
                require(request.remote and ipaddress.ip_address(request.remote).is_loopback)
            else:
                require(request.secure)
            if request.path in PROBE_PATHS:
                # The explicit read-only exception to full logging: a probe is never admitted,
                # never emits an event and never causes a write of any kind.
                return await probe_endpoint(request)
            correlation = diagnostics.adopt_correlation(request.headers.get(diagnostics.HEADER))
            if not diagnostics.admitted():
                # New business is refused while the log cannot be written. Work already in flight
                # keeps its own result; only the next request is turned away.
                response = error("dependency_unavailable", 503, request_id, native, web_error)
            else:
                context = diagnostics.use_correlation(correlation)
                # Admission waits for the accepting event's bytes without ever blocking the loop:
                # a slow disk costs this request its bound, not the whole process its heartbeat.
                admitted = await diagnostics.accept_async(correlation) is not None
                if admitted:
                    response = await dispatch(request)
                else:
                    response = error("dependency_unavailable", 503, request_id, native, web_error)
        except web.HTTPRequestEntityTooLarge:
            response = error("budget_exceeded", 413, request_id, native, web_error)
        except Fault as exc:
            response = error(exc.code, exc.status, request_id, native, web_error)
        except (sqlite3.Error, OSError):
            response = error("dependency_unavailable", 503, request_id, native, web_error)
        except (ValueError, TypeError, KeyError, RecursionError):
            response = error("invalid_input", 400, request_id, native, web_error)
        except Exception:
            # An exception this product did not anticipate is still answered with a code the
            # published error contract allows, and recorded as the one fixed internal code: no
            # exception text, type or traceback reaches either the wire or the log.
            unexpected = True
            response = error("dependency_unavailable", 503, request_id, native, web_error)
        except asyncio.CancelledError:
            # Cancellation is this request's terminal state, so it is recorded here and only here:
            # one place owns request lifecycle, and a cancelled request never produces a response.
            # The record is then confirmed within the terminal bound, and the obligation is handed
            # to the sink *before* the wait: a task that is cancelled a second time while it waits
            # cannot take the record with it, because the sink already owns it and will either
            # settle it or count it as unconfirmed. Nothing about the business result changes.
            if admitted and correlation is not None:
                sequence = diagnostics.event(
                    "http.request.finished",
                    "INFO",
                    "cancelled",
                    correlation_id=correlation,
                    duration_ms=max(0.0, (time.monotonic() - started) * 1000.0),
                )
                await diagnostics.settle_async(sequence)
            raise
        finally:
            if context is not None:
                diagnostics.reset_correlation(context)
        if response.status in (401, 403):
            auth = diagnostics.AUTH_REJECTED
        elif request.get(CONSOLE_AUTH) == diagnostics.AUTH_SUCCEEDED or (
            internal and reached_handler and request.headers.get("Authorization") is not None
        ):
            # Two facts, and only these two, are evidence that this request was authenticated: the
            # console verified a password or presented a live session it had already verified, or an
            # internal route really ran the code that authenticates the Bearer it was given. The
            # mere existence of a Cookie or an Authorization header is not evidence of anything, so
            # a public page carrying an unrelated cookie, an unknown path and a refusal that happens
            # before authentication all stay `not_attempted`.
            auth = diagnostics.AUTH_SUCCEEDED
        if admitted and correlation is not None:
            # The terminal events are confirmed durable, bounded, before the answer leaves. A sink
            # that cannot confirm them has already stopped admitting new work; the response itself
            # is the business result and is never rewritten, retried or rolled back because the
            # log could not keep up.
            await diagnostics.finish_async(
                response.status,
                max(0.0, (time.monotonic() - started) * 1000.0),
                correlation,
                error_code="internal_error" if unexpected else None,
                auth=auth,
            )
        sealed(response)
        if correlation is not None:
            response.headers[diagnostics.HEADER] = correlation
        return response

    def error(code, status, request_id, native, web_error=False):
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
            "execution_state": (
                "unknown"
                if web_error and code in {"timed_out", "connection_failed", "result_unknown"}
                else "not_started"
            ),
            "retryable": status == 503 and code not in {"result_unknown", "connection_failed"},
        }
        if not web_error:
            # Internal v1 remains the published closed code set. Browser management has its
            # own typed catalog/upstream errors and cannot alter that immutable package.
            if code not in {
                "invalid_input",
                "unauthorized",
                "forbidden",
                "not_found",
                "version_conflict",
                "scope_changed",
                "budget_exceeded",
                "queue_full",
                "timeout",
                "dependency_unavailable",
                "result_unknown",
                "idempotency_conflict",
                "cursor_expired",
                "unsupported_version",
            }:
                body["code"] = "dependency_unavailable"
                status = 503
                body["retryable"] = True
            platform.contracts.check("common#error", body)
        return web.json_response(body, status=status)

    async def resolve(request):
        return web.json_response(
            await platform.local_work.run(
                platform.origins.resolve, request.headers["Authorization"], request[BODY]
            )
        )

    async def snapshot(request):
        return web.json_response(
            await platform.local_work.run(
                platform.models.snapshot, request.headers["Authorization"], request[BODY]
            )
        )

    async def provider_select(request):
        return web.json_response(
            await platform.local_work.run(
                platform.provider_authority.select,
                request.headers.get("Authorization", ""),
                request[BODY],
            )
        )

    async def provider_runtime(request):
        return web.json_response(
            await platform.local_work.run(
                platform.provider_authority.runtime,
                request.headers.get("Authorization", ""),
                request[BODY],
            )
        )

    async def native_snapshot(request):
        return web.json_response(
            await platform.local_work.run(
                platform.models.native_snapshot, request.headers["Authorization"], request[BODY]
            )
        )

    async def renew_origin(request):
        return web.json_response(
            await platform.local_work.run(
                model_origin_renewal.renew,
                platform.origins,
                request.headers["Authorization"],
                request[BODY],
            )
        )

    async def source_access(request):
        return web.json_response(
            await platform.local_work.run(
                platform.sources.read, request.headers["Authorization"], request[BODY]
            )
        )

    async def observation_verify(request):
        return web.json_response(
            await platform.local_work.run(
                platform.bot_observation.verify,
                request.headers["Authorization"],
                request[BODY],
            )
        )

    async def qq_admin_check(request):
        return web.json_response(
            await platform.local_work.run(
                platform.qq_admin.check, request.headers.get("Authorization", ""), request[BODY]
            )
        )

    async def observation_admin_status(request):
        require(request[BODY] == {}, "invalid_input", 400)
        return web.json_response(
            await platform.local_work.run(
                platform.bot_observation.local_status, request.headers["Authorization"]
            )
        )

    async def observation_admin_enroll(request):
        return web.json_response(
            await platform.bot_observation.local_enroll_default(
                request.headers["Authorization"], request[BODY]
            )
        )

    async def send(request):
        body = request[BODY]
        if body["destination"]["namespace"] != "web":
            return web.json_response(
                await platform.local_work.run(
                    platform.bots.send, request.headers["Authorization"], body
                )
            )
        require(console.config is not None, "dependency_unavailable", 503)
        return web.json_response(
            await platform.local_work.run(
                console.sender.send, request.headers["Authorization"], body
            )
        )

    async def reply_status(request):
        return web.json_response(
            await platform.local_work.run(
                platform.bots.reply_status, request.headers["Authorization"], request[BODY]
            )
        )

    async def bot_route(request):
        path = request.path
        body = request[BODY]
        header = request.headers["Authorization"]
        if path == "/internal/v1/bot/events":
            return web.json_response(await platform.bots.event(header, body))
        operation = {
            "/internal/v1/bot/events/status": platform.bots.event_status,
            "/internal/v1/bot/heartbeat": platform.bots.heartbeat,
            "/internal/v1/bot/replies/claim": platform.bots.claim,
            "/internal/v1/bot/replies/status": platform.bots.claim_status,
            "/internal/v1/bot/replies/ack": platform.bots.ack,
        }[path]
        return web.json_response(await platform.local_work.run(operation, header, body))

    async def delivery_route(request):
        operation = request.path[len(DELIVERY_PREFIX) :]
        delivery = platform.bots.delivery
        if operation == "send":
            result = await platform.local_work.run(
                delivery.send, request.headers["Authorization"], request[BODY]
            )
        elif operation == "context":
            result = await platform.local_work.run(
                delivery.context, request.headers["Authorization"], request[BODY]
            )
        else:
            result = await platform.local_work.run(
                delivery.control, request.headers["Authorization"], request[BODY], operation
            )
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def proof_route(request):
        operation = (
            platform.memory_proofs.issue
            if request.path == PROOF_ISSUE
            else platform.memory_proofs.verify
        )
        result = await platform.local_work.run(
            operation, request.headers["Authorization"], request[BODY]
        )
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def credential_route(request):
        result = await platform.local_work.run(
            platform.service_credentials.resolve, request.headers["Authorization"], request[BODY]
        )
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    app = web.Application(middlewares=[boundary], client_max_size=45 * 1024 * 1024)
    app[PLATFORM] = platform
    if not public and platform.media.config is not None:

        async def media_context(app):
            await platform.media.start()
            try:
                yield
            finally:
                await platform.media.close()

        app.cleanup_ctx.append(media_context)

    if not public and platform.bot_adapters.catalog is not None:

        async def bot_pump_context(app):
            async def pump():
                while True:
                    try:
                        await platform.bot_adapters.pump_once()
                        await platform.bot_observation.pump_once()
                        await platform.qq_admin.flush_aliases()
                    except (Fault, OSError, sqlite3.Error):
                        pass
                    await asyncio.sleep(2)

            task = asyncio.create_task(pump())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        app.cleanup_ctx.append(bot_pump_context)
    if (
        not public
        and platform.bot_adapters.catalog is None
        and platform.settings.get("qq_alias_memory") is not None
    ):

        async def qq_alias_pump_context(app):
            async def pump():
                while True:
                    try:
                        await platform.qq_admin.flush_aliases()
                    except (Fault, OSError, sqlite3.Error):
                        pass
                    await asyncio.sleep(2)

            task = asyncio.create_task(pump())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        app.cleanup_ctx.append(qq_alias_pump_context)
    if not public and platform.role_runtime.config is not None:

        async def role_pump_context(app):
            async def pump():
                while True:
                    await platform.role_runtime.resume_pending()
                    await asyncio.sleep(5)

            task = asyncio.create_task(pump())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        app.cleanup_ctx.append(role_pump_context)
    if not public:
        app.router.add_post("/internal/v1/origins/resolve", resolve)
        app.router.add_post("/internal/v1/model-config/snapshot", snapshot)
        if platform.provider_catalog is not None:
            app.router.add_post(PROVIDER_SELECT, provider_select)
            app.router.add_post(PROVIDER_RUNTIME, provider_runtime)
        if renewal_open:
            app.router.add_post(model_origin_renewal.PATH, renew_origin)
        if native_open:
            # Default closed; only an explicit deployment setting registers the native port.
            app.router.add_post(NATIVE_SNAPSHOT, native_snapshot)
        app.router.add_post("/internal/v1/source-access/read", source_access)
        app.router.add_post(QQ_ADMIN_CHECK, qq_admin_check)
        if platform.bot_observation.catalog is not None:
            app.router.add_post(OBSERVATION_VERIFY, observation_verify)
            app.router.add_post(OBSERVATION_ADMIN_STATUS, observation_admin_status)
            app.router.add_post(OBSERVATION_ADMIN_ENROLL, observation_admin_enroll)
        app.router.add_post("/internal/v1/conversation/send", send)
        app.router.add_post(PROOF_ISSUE, proof_route)
        app.router.add_post(PROOF_VERIFY, proof_route)
        app.router.add_post(SERVICE_CREDENTIALS, credential_route)
        if hasattr(platform.bots, "delivery"):
            for path in DELIVERY_ROUTES:
                app.router.add_post(path, delivery_route)
        if platform.bots.config is not None:
            app.router.add_post(REPLY_STATUS, reply_status)
            for path in BOT_PATHS:
                app.router.add_post(path, bot_route)
    app.router.add_route("*", "/{path:.*}", console.handle)

    async def close_local_work(app):
        platform.local_work.close()

    if not public:
        app.on_cleanup.append(close_local_work)
    return app
