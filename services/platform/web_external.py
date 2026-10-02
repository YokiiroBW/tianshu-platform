"""Administrator-only browser setup for two fixed read connectors, HA and AssetLink."""

import asyncio
import re
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography import x509

from .contracts import Fault, require
from .external_catalog import ExternalCatalog
from .home import ENTITY_PATTERN, KINDS, MAX_ENTITIES, Home
from .web_external_net import assert_pins, reviewed_pins

PREFIX = "/api/web/external/"
UNLOCK_TTL = 900
CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----", re.S)


def _url(value, kind, allow_http=False):
    require(isinstance(value, str) and 1 <= len(value) <= 2048, "invalid_input", 400)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise Fault("invalid_input", 400) from None
    require(
        parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and (port is None or 0 < port <= 65535)
        and not value.endswith("/")
        and "%" not in value
        and "\\" not in value
        and all(32 < ord(c) < 127 for c in value),
        "invalid_input",
        400,
    )
    if kind == "assets":
        require(
            parsed.scheme == "https" and parsed.path == "/assetlink/v1/control",
            "invalid_input",
            400,
        )
    else:
        require(
            parsed.scheme in ({"http", "https"} if allow_http else {"https"})
            and parsed.path in {"", "/"},
            "invalid_input",
            400,
        )
    return value


def _secret_change(value, *, ca=False):
    require(
        isinstance(value, dict) and value.get("action") in {"keep", "replace", "clear"},
        "invalid_input",
        400,
    )
    action = value["action"]
    require(
        set(value) == ({"action", "value"} if action == "replace" else {"action"}),
        "invalid_input",
        400,
    )
    if action == "replace":
        text = value["value"]
        if ca:
            require(isinstance(text, str) and 1 <= len(text) <= 16384, "invalid_input", 400)
            matches = list(CERTIFICATE.finditer(text))
            require(1 <= len(matches) <= 8, "invalid_input", 400)
            require(not CERTIFICATE.sub("", text).strip(), "invalid_input", 400)
            try:
                for match in matches:
                    x509.load_pem_x509_certificate(match.group().encode())
                ssl.create_default_context(cadata=text)
            except (ValueError, ssl.SSLError):
                raise Fault("invalid_input", 400) from None
        else:
            require(
                isinstance(text, str)
                and 24 <= len(text) <= 4096
                and all(33 <= ord(char) <= 126 for char in text),
                "invalid_input",
                400,
            )
    return value


def _value(kind, value):
    require(isinstance(value, dict), "invalid_input", 400)
    if kind == "assets":
        require(set(value) == {"enabled", "endpoint"}, "invalid_input", 400)
        require(type(value["enabled"]) is bool, "invalid_input", 400)
        _url(value["endpoint"], kind)
    else:
        require(
            set(value) == {"enabled", "base_url", "allow_private_http", "entities"},
            "invalid_input",
            400,
        )
        require(
            type(value["enabled"]) is bool and type(value["allow_private_http"]) is bool,
            "invalid_input",
            400,
        )
        _url(value["base_url"], kind, value["allow_private_http"])
        items = value["entities"]
        require(isinstance(items, list) and 1 <= len(items) <= MAX_ENTITIES, "invalid_input", 400)
        ids = set()
        for item in items:
            require(
                isinstance(item, dict)
                and {"entity_id", "label", "kind"}
                <= set(item)
                <= {"entity_id", "label", "kind", "unit"},
                "invalid_input",
                400,
            )
            entity_id = item["entity_id"]
            require(
                isinstance(entity_id, str)
                and re.fullmatch(ENTITY_PATTERN, entity_id)
                and item["kind"] in KINDS
                and entity_id.split(".")[0] == item["kind"]
                and entity_id not in ids,
                "invalid_input",
                400,
            )
            ids.add(entity_id)
            require(
                isinstance(item["label"], str)
                and 1 <= len(item["label"]) <= 64
                and item["label"].isprintable(),
                "invalid_input",
                400,
            )
            unit = item.get("unit")
            require(
                unit is None
                or (isinstance(unit, str) and 1 <= len(unit) <= 16 and unit.isprintable()),
                "invalid_input",
                400,
            )
    return value


class WebExternal:
    def __init__(self, platform, console):
        self.platform = platform
        self.console = console
        self.config = platform.settings.get("web_external")
        self.catalog = ExternalCatalog(self.config["directory"]) if self.config else None
        self.revision = self.catalog.snapshot()[0] if self.catalog else 0

    def _gate(self, session):
        require(self.catalog is not None, "external_not_configured", 503)
        require(self.console.session_valid(session), "session_expired", 401)
        principal = self.platform.auth.principals.get(self.console.config["principal"], {})
        require(
            principal.get("kind") == "operator"
            and "external.manage" in principal.get("actions", []),
            "external_manage_required",
            403,
        )

    def _unlocked(self, session):
        return session.get("external_unlock", 0) > self.console.clock()

    def sync(self, *, force=False):
        if not self.catalog:
            return
        revision, _ = self.catalog.snapshot()
        if revision == self.revision and not force:
            return
        next_home = Home(self.platform, self.console)
        self.revision = revision
        for session in self.console.sessions.values():
            session.pop("assets", None)
            session.pop("devices", None)
            session.pop("external_unlock", None)
        self.console.assets.reads.clear()
        self.console.home = next_home

    def _row(self, kind):
        revision, rows = self.catalog.snapshot()
        row = rows.get(kind)
        if row is not None:
            value = row["value"]
            assert_pins(
                value["endpoint" if kind == "assets" else "base_url"],
                value["pins"],
                self.config["allowed_cidrs"],
            )
        return revision, row

    def asset_connection(self):
        _, row = self._row("assets")
        require(row and row["value"]["enabled"], "external_not_configured", 503)
        require(row["credential"], "external_credential_missing", 503)
        return {
            "endpoint": row["value"]["endpoint"],
            "credential": row["credential"],
            "ca_pem": row["ca_pem"],
            "pins": row["value"]["pins"],
            "managed_revision": row["value"]["connection_revision"],
        }

    def asset_credential_present(self):
        _, row = self._row("assets")
        return bool(row and row["value"]["enabled"] and row["credential"])

    def home_value(self):
        _, row = self._row("home")
        if row is None:
            return None
        value = row["value"]
        return {
            **{
                key: item
                for key, item in value.items()
                if key not in {"pins", "connection_revision"}
            },
            "reviewed_addresses": value["pins"]["addresses"],
        }

    def home_generation(self):
        _, row = self._row("home")
        return row["value"]["connection_revision"] if row else None

    def home_connection(self):
        _, row = self._row("home")
        if row is None or not row["value"]["enabled"]:
            return None
        return {
            "credential": row["credential"],
            "ca_pem": row["ca_pem"],
            "pins": row["value"]["pins"],
            "managed_revision": row["value"]["connection_revision"],
        }

    def view(self, session):
        self._gate(session)
        self.sync()
        revision, rows = self.catalog.snapshot()
        result = {"revision": revision, "unlocked": self._unlocked(session)}
        for kind in ("assets", "home"):
            row = rows.get(kind)
            value = row["value"] if row else {}
            result[kind] = {
                "configured": row is not None,
                "enabled": value.get("enabled", False),
                "url": value.get("endpoint" if kind == "assets" else "base_url"),
                "credential_configured": bool(row and row["credential"]),
                "ca_configured": bool(row and row["ca_pem"]),
                "last_test": row["last_test"] if row else None,
            }
            if kind == "home":
                result[kind].update(
                    allow_private_http=value.get("allow_private_http", False),
                    entities=value.get("entities", []),
                )
        return result

    async def route(self, path, body, session):
        await self.platform.local_work.run(self._gate, session)
        require(isinstance(body, dict), "invalid_input", 400)
        name = path.removeprefix(PREFIX)
        if name == "view":
            require(body == {}, "invalid_input", 400)
            return await self.platform.local_work.run(self.view, session)
        if name == "unlock":
            require(set(body) == {"password"}, "invalid_input", 400)
            password = body["password"]
            require(isinstance(password, str) and 12 <= len(password) <= 256, "unauthorized", 401)
            console = self.console
            async with console.login_lock:
                console.failures = [t for t in console.failures if t > console.clock() - 60]
                require(len(console.failures) < 5, "too_many_requests", 429)
                valid = await asyncio.to_thread(console.verify_password, password)
                if not valid:
                    console.failures.append(console.clock())
                    raise Fault("unauthorized", 401)
            session["external_unlock"] = console.clock() + UNLOCK_TTL
            return {"unlocked": True, "expires_in": UNLOCK_TTL}
        if name == "lock":
            require(body == {}, "invalid_input", 400)
            session.pop("external_unlock", None)
            return {"unlocked": False}
        require(self._unlocked(session), "external_locked", 403)
        if name == "save":
            require(
                set(body)
                == {"kind", "expected_revision", "client_id", "value", "credential", "ca"},
                "invalid_input",
                400,
            )
            kind = body["kind"]
            require(kind in {"assets", "home"}, "invalid_input", 400)
            value = _value(kind, body["value"])
            credential = _secret_change(body["credential"])
            ca = _secret_change(body["ca"], ca=True)
            url = value["endpoint" if kind == "assets" else "base_url"]
            pins = await reviewed_pins(url, self.config["allowed_cidrs"])
            await self.platform.local_work.run(self._gate, session)
            require(self._unlocked(session), "external_locked", 403)
            result = await self.platform.local_work.run(
                self.catalog.save,
                kind,
                {**value, "pins": pins},
                credential,
                ca,
                body["expected_revision"],
                body["client_id"],
            )
            require(
                (await self.platform.local_work.run(self.catalog.snapshot))[0]
                == result["revision"],
                "revision_conflict",
                409,
            )
            unlock = session["external_unlock"]
            await self.platform.local_work.run(self.sync)
            session["external_unlock"] = unlock
            return {**result, "applied": True}
        if name == "test":
            require(
                set(body) == {"kind"} and body["kind"] in {"assets", "home"}, "invalid_input", 400
            )
            return await self.test(body["kind"], session)
        raise Fault("not_found", 404)

    async def test(self, kind, session):
        await self.platform.local_work.run(self.sync)
        revision, row = await self.platform.local_work.run(self._row, kind)
        require(row and row["value"]["enabled"], "external_not_configured", 503)
        require(row["credential"], "external_credential_missing", 503)
        try:
            if kind == "assets":
                result = await self.platform.assets.read(
                    self.console.assets._header(),
                    {
                        "connection_id": self.config["assets_connection_id"],
                        "operation": "libraries.list",
                        "body": {"page_size": 1},
                    },
                )
                if not result["ok"]:
                    raise Fault(result["code"], result["status"])
            else:
                home = self.console.home
                entity = next(iter(home.entities))
                async with home._client() as client:
                    await home._observe(client, entity)
            state, code = "connected", "read_observed"
        except Fault as exc:
            state = (
                "unauthorized"
                if exc.status in {401, 403} or exc.code == "device_unauthorized"
                else "unavailable"
            )
            code = exc.code
        await self.platform.local_work.run(self._gate, session)
        require(
            (await self.platform.local_work.run(self.catalog.snapshot))[0] == revision,
            "revision_conflict",
            409,
        )
        result = {
            "kind": kind,
            "state": state,
            "code": code,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "revision": revision,
        }
        await self.platform.local_work.run(self.catalog.record_test, kind, revision, result)
        return result
