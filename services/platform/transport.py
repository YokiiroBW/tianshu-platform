"""Deployment-fixed HTTPS only; environment secrets never cross JSON boundaries."""

import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

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
    try:
        tls = ssl.create_default_context(cafile=settings.get("ca_file"))
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                settings["base_url"].rstrip("/") + "/internal/v1/conversation/" + path,
                json=payload,
                headers={"Authorization": "Bearer " + token},
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
                return result
    except (aiohttp.ClientError, TimeoutError, OSError, ssl.SSLError):
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
    try:
        tls = ssl.create_default_context(cafile=settings.get("ca_file"))
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as session:
            async with session.post(
                url,
                json=ingest,
                headers={"Authorization": "Bearer " + token},
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
                return loads(b"".join(chunks))
    except (aiohttp.ClientError, TimeoutError, OSError, ssl.SSLError):
        # The request may have committed at Core. Retry the original semantic/key.
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
