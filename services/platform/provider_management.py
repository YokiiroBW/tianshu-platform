"""Same-origin administrator adapter for the private provider owner and trusted gateway."""

import asyncio
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .auth import secret
from .contracts import Fault, require
from .provider_catalog import validate_test_diagnostic


class ProviderTestFault(Fault):
    def __init__(self, code, status, diagnostic=None):
        super().__init__(code, status)
        self.diagnostic = validate_test_diagnostic(diagnostic)


def validate_configuration(config, *, existing=True):
    if config is None:
        return
    require(
        isinstance(config, dict)
        and set(config) == {"directory", "gateway_url", "gateway_token_env"},
        "invalid_input",
        400,
    )
    require(
        isinstance(config["directory"], str) and Path(config["directory"]).is_absolute(),
        "invalid_input",
        400,
    )
    address = config["gateway_url"]
    require(isinstance(address, str) and len(address) <= 2048, "invalid_input", 400)
    try:
        parsed = urlsplit(address)
        port = parsed.port
    except ValueError:
        raise Fault("invalid_input", 400) from None
    require(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and (port is None or 0 < port <= 65535)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and not address.endswith("/")
        and "%" not in address
        and "\\" not in address,
        "invalid_input",
        400,
    )
    name = config["gateway_token_env"]
    require(
        isinstance(name, str)
        and name.isascii()
        and name.isidentifier()
        and secret(name) is not None,
        "dependency_unavailable",
        503,
    )
    if existing:
        from .provider_catalog import ProviderCatalog

        ProviderCatalog.verify_existing(config["directory"])


def _client_id(body):
    value = body.get("client_id")
    require(isinstance(value, str), "invalid_input", 400)
    try:
        require(str(uuid.UUID(value)) == value, "invalid_input", 400)
    except ValueError:
        raise Fault("invalid_input", 400) from None
    return value


class ProviderManagement:
    def __init__(self, platform, console):
        self.platform = platform
        self.console = console
        self.catalog = platform.provider_catalog
        self.config = platform.settings.get("provider_self_service")

    def _gate(self, session):
        require(self.console.session_valid(session), "session_expired", 401)
        self._quick_gate(session)

    def _quick_gate(self, session):
        require(self.console.session_live(session), "session_expired", 401)
        self.console.models._gate(session)

    def _view(self, session):
        self._gate(session)
        return self.catalog.view()

    async def route(self, path, body, session):
        require(self.catalog is not None, "management_disabled", 403)
        self._quick_gate(session)
        operation = path.removeprefix("/api/web/providers/")
        if operation == "view":
            require(body == {}, "invalid_input", 400)
            return await self.platform.local_work.run(self._view, session)
        if operation == "save":
            allowed = {
                "client_id",
                "provider_id",
                "expected_revision",
                "name",
                "protocol",
                "base_url",
                "model_id",
                "enabled",
                "api_key",
            }
            require(
                set(body) <= allowed
                and {"client_id", "name", "protocol", "base_url", "model_id", "enabled"}
                <= set(body),
                "invalid_input",
                400,
            )
            _client_id(body)
            return await self.platform.local_work.run(
                self._write, session, self.catalog.save, **body
            )
        if operation in {"clear-key", "delete"}:
            require(
                set(body) == {"client_id", "provider_id", "expected_revision"}, "invalid_input", 400
            )
            _client_id(body)
            method = self.catalog.clear_key if operation == "clear-key" else self.catalog.delete
            return await self.platform.local_work.run(self._write, session, method, **body)
        if operation == "default":
            require(
                set(body)
                == {"client_id", "provider_id", "expected_revision", "expected_default_revision"},
                "invalid_input",
                400,
            )
            _client_id(body)
            return await self.platform.local_work.run(
                self._write, session, self.catalog.set_default, **body
            )
        if operation == "function":
            require(
                set(body)
                == {
                    "client_id",
                    "function_id",
                    "provider_id",
                    "expected_revision",
                    "expected_binding_revision",
                },
                "invalid_input",
                400,
            )
            _client_id(body)
            return await self.platform.local_work.run(
                self._write, session, self.catalog.functions.set_binding, **body
            )
        if operation in {"models", "test"}:
            required = {"provider_id", "expected_revision"}
            if operation == "test":
                required.add("client_id")
                _client_id(body)
            require(set(body) == required, "invalid_input", 400)
            if operation == "test":
                claim = await self.platform.local_work.run(
                    self._write, session, self.catalog.claim_test, **body
                )
                if claim["state"] == "pending":
                    raise Fault("result_unknown", 409)
                if claim["state"] == "settled":
                    replay = claim["result"]
                    if "error" in replay:
                        raise Fault(replay["error"], replay["status"])
                    return replay
            context = await self.platform.local_work.run(
                self._context, session, body["provider_id"], body["expected_revision"]
            )
            try:
                result = await self._gateway(operation, context)
            except Fault as fault:
                if operation == "test":
                    outcome = {
                        "authentication_failed": "authentication_failed",
                        "endpoint_failed": "endpoint_failed",
                        "model_not_found": "model_not_found",
                    }.get(fault.code, "unknown")
                    await self.platform.local_work.run(
                        self.catalog.finish_test,
                        client_id=body["client_id"],
                        provider_id=context.provider_id,
                        expected_revision=context.revision,
                        outcome=outcome,
                        error=fault.code,
                        status=fault.status,
                        diagnostic=getattr(fault, "diagnostic", None),
                    )
                raise
            await self.platform.local_work.run(self._gate, session)
            if operation == "models":
                return {
                    "provider_id": context.provider_id,
                    "revision": context.revision,
                    "models": result["models"],
                }
            return await self.platform.local_work.run(
                self.catalog.finish_test,
                client_id=body["client_id"],
                provider_id=context.provider_id,
                expected_revision=context.revision,
                outcome="succeeded",
            )
        raise Fault("not_found", 404)

    def _write(self, session, method, **kwargs):
        self._gate(session)
        return method(**kwargs)

    def _context(self, session, provider_id, revision):
        self._gate(session)
        return self.catalog.execution_context(provider_id, revision)

    async def _gateway(self, operation, context):
        config = self.config
        token = secret(config["gateway_token_env"])
        require(token is not None, "dependency_unavailable", 503)
        body = {
            "provider_id": context.provider_id,
            "revision": context.revision,
            "protocol": context.protocol,
            "base_url": context.base_url,
            "model_id": context.model_id,
            "api_key": context.api_key,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(
                timeout=timeout,
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
            ) as client:
                async with client.post(
                    config["gateway_url"] + "/internal/v1/provider-self-service/" + operation,
                    json=body,
                    headers={"Authorization": "Bearer " + token},
                    allow_redirects=False,
                ) as response:
                    raw = await response.content.read(1_048_577)
                    require(len(raw) <= 1_048_576, "dependency_unavailable", 503)
                    document = json.loads(raw)
                    if response.status != 200:
                        code = document.get("code") if isinstance(document, dict) else None
                        allowed = {
                            "authentication_failed",
                            "endpoint_failed",
                            "model_not_found",
                            "enumeration_unsupported",
                            "connection_failed",
                            "timed_out",
                            "upstream_invalid",
                            "upstream_rejected",
                        }
                        raise ProviderTestFault(
                            code if code in allowed else "dependency_unavailable",
                            response.status if code in allowed else 503,
                            document.get("provider_diagnostic") if code in allowed else None,
                        )
                    if operation == "test":
                        require(document == {"outcome": "succeeded"}, "dependency_unavailable", 503)
                    else:
                        models = document.get("models") if isinstance(document, dict) else None
                        require(
                            isinstance(models, list)
                            and len(models) <= 4096
                            and all(isinstance(m, str) and 0 < len(m) <= 512 for m in models),
                            "dependency_unavailable",
                            503,
                        )
                    return document
        except asyncio.CancelledError:
            raise
        except Fault:
            raise
        except TimeoutError:
            raise Fault("timed_out", 504) from None
        except aiohttp.ClientError:
            raise Fault("connection_failed", 503) from None
        except (ValueError, TypeError):
            raise Fault("upstream_invalid", 502) from None
