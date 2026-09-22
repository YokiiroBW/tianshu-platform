"""Deployment-fixed HTTPS only; environment secrets never cross JSON boundaries."""

import asyncio
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from . import diagnostics
from .auth import secret
from .contracts import Fault, loads, require


class CoreFault(Fault):
    def __init__(self, document, status):
        super().__init__(document["code"], status)
        self.document = document


async def core_web_call(settings, path, payload, contracts, schema):
    require(path in {"web-snapshot", "cancel", "ingest-actors"}, "invalid_input", 400)
    _, timeout = core_settings(settings)
    token = secret(settings["token_env"])
    require(token is not None, "dependency_unavailable", 503)
    span = diagnostics.Span("core_web_call")
    await diagnostics.outbound("started")
    sent = False
    try:
        tls = ssl.create_default_context(cafile=settings.get("ca_file"))
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            sent = True
            async with session.post(
                settings["base_url"].rstrip("/") + "/internal/v1/conversation/" + path,
                json=payload,
                # The same legal correlation travels with the existing call; the body, the
                # credential and the authorisation are untouched.
                headers={
                    "Authorization": "Bearer " + token,
                    **diagnostics.correlation_header(),
                },
                ssl=tls,
                allow_redirects=False,
            ) as response:
                require(response.content_type == "application/json", "dependency_unavailable", 503)
                chunks, total = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    require(total <= 1048576, "budget_exceeded", 413)
                    chunks.append(chunk)
                result = loads(b"".join(chunks))
                if response.status != 200:
                    contracts.check("common#error", result)
                    require(
                        result["request_id"]
                        == payload.get("query", payload.get("command"))["request_id"],
                        "dependency_unavailable",
                        503,
                    )
                    raise CoreFault(result, response.status)
                contracts.check(schema, result)
                await diagnostics.outbound("succeeded", duration_ms=span.elapsed() * 1000.0)
                return result
    except Fault as exc:
        await diagnostics.outbound(
            "failed",
            duration_ms=span.elapsed() * 1000.0,
            error_code=diagnostics.safe_code(exc.code),
        )
        raise
    except TimeoutError:
        await diagnostics.outbound(
            "timed_out", duration_ms=span.elapsed() * 1000.0, error_code="timeout"
        )
        raise Fault("dependency_unavailable", 503) from None
    except asyncio.CancelledError:
        # Cancellation is a terminal state of this call, not the absence of one: the started event
        # already exists, so leaving it without an end would misreport a call that is over as one
        # still in flight. The fixed code says whether anything could have left this process, and
        # nothing is retried or claimed about the remote result either way. The terminal record is
        # confirmed within the terminal bound, and its obligation belongs to the sink before the
        # wait starts, so a second cancellation ends this call without ending the record.
        await diagnostics.outbound(
            "cancelled",
            duration_ms=span.elapsed() * 1000.0,
            error_code=None if sent else "outbound_not_sent",
        )
        raise
    except (aiohttp.ClientError, OSError, ssl.SSLError):
        await diagnostics.outbound(
            "failed",
            duration_ms=span.elapsed() * 1000.0,
            error_code="dependency_unavailable",
        )
        raise Fault("dependency_unavailable", 503) from None


def core_settings(settings):
    require(isinstance(settings, dict), "dependency_unavailable", 503)
    require(
        set(settings) <= {"base_url", "token_env", "ca_file", "timeout_seconds"},
        "invalid_input",
        400,
    )
    require(
        "ca_file" not in settings or Path(settings["ca_file"]).is_absolute(), "invalid_input", 400
    )
    url = urlsplit(settings["base_url"])
    require(
        url.scheme == "https"
        and bool(url.hostname)
        and url.username is None
        and url.password is None
        and url.path in {"", "/"}
        and not url.query
        and not url.fragment,
        "invalid_input",
        400,
    )
    require(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", settings["token_env"]), "invalid_input", 400)
    timeout = settings.get("timeout_seconds", 10)
    require(type(timeout) in (int, float) and 0 < timeout <= 60, "invalid_input", 400)
    return settings["base_url"].rstrip("/") + "/internal/v1/conversation/ingest-actors", timeout


async def core_post(settings, ingest, contracts=None):
    if contracts is not None:
        return await core_web_call(
            settings, "ingest-actors", ingest, contracts, "sources#fanout_response"
        )
    url, timeout = core_settings(settings)
    token = secret(settings["token_env"])
    require(token is not None, "dependency_unavailable", 503)
    span = diagnostics.Span("core_post")
    await diagnostics.outbound("started")
    sent = False
    try:
        tls = ssl.create_default_context(cafile=settings.get("ca_file"))
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            sent = True
            async with session.post(
                url,
                json=ingest,
                headers={
                    "Authorization": "Bearer " + token,
                    **diagnostics.correlation_header(),
                },
                ssl=tls,
                allow_redirects=False,
            ) as response:
                require(response.status == 200, "dependency_unavailable", 503)
                require(response.content_type == "application/json", "dependency_unavailable", 503)
                chunks, total = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    require(total <= 1_048_576, "budget_exceeded", 413)
                    chunks.append(chunk)
                result = loads(b"".join(chunks))
                await diagnostics.outbound("succeeded", duration_ms=span.elapsed() * 1000.0)
                return result
    except Fault as exc:
        await diagnostics.outbound(
            "failed",
            duration_ms=span.elapsed() * 1000.0,
            error_code=diagnostics.safe_code(exc.code),
        )
        raise
    except TimeoutError:
        await diagnostics.outbound(
            "timed_out", duration_ms=span.elapsed() * 1000.0, error_code="timeout"
        )
        raise Fault("dependency_unavailable", 503) from None
    except asyncio.CancelledError:
        # Same rule as the web call above: the started event gets its end, confirmed within the
        # terminal bound with the obligation owned by the sink, and a cancelled call is never
        # reported as one that is still running.
        await diagnostics.outbound(
            "cancelled",
            duration_ms=span.elapsed() * 1000.0,
            error_code=None if sent else "outbound_not_sent",
        )
        raise
    except (aiohttp.ClientError, OSError, ssl.SSLError):
        # The request may have committed at Core. Retry the original semantic/key.
        await diagnostics.outbound(
            "failed",
            duration_ms=span.elapsed() * 1000.0,
            error_code="dependency_unavailable",
        )
        raise Fault("dependency_unavailable", 503) from None


def server_tls(settings):
    require(
        isinstance(settings, dict) and set(settings) == {"certificate_file", "private_key_file"},
        "invalid_input",
        400,
    )
    require(all(Path(value).is_absolute() for value in settings.values()), "invalid_input", 400)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(settings["certificate_file"], settings["private_key_file"])
    return context
