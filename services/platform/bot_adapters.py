"""Browser-managed bot adapters, using the existing Bots authority and ledgers."""

import asyncio
import json
import re
import secrets
import ssl
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone

import aiohttp

from .auth import secret
from .bot_adapter_catalog import BotAdapterCatalog
from .contracts import Fault, digest, loads, require, utc
from .role_runtime import is_placeholder_actor
from .transport import core_settings
from .web_external import _url
from .web_external_net import PinnedResolver, assert_pins, reviewed_pins

PREFIX = "/api/web/bot-adapters/"
RPC = "/tianshu/adapter/v1"
PROTOCOL = "tianshu.bot-adapter/v1"
TIMEOUT = 5


def _id(value):
    require(
        isinstance(value, str) and 1 <= len(value) <= 128 and value.isprintable(),
        "invalid_input",
        400,
    )
    return value


def _qq_id(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) is not None,
        "invalid_input",
        400,
    )
    return value


def _client_id(value):
    try:
        require(str(uuid.UUID(value)) == value, "invalid_input", 400)
    except (TypeError, ValueError):
        raise Fault("invalid_input", 400) from None
    return value


class BotAdapters:
    def __init__(self, platform):
        self.p = platform
        self.config = platform.settings.get("bot_adapter_self_service")
        self.catalog = BotAdapterCatalog(self.config["directory"]) if self.config else None
        self.drafts = {}
        self.lock = asyncio.Lock()
        self.row_locks = {}
        self.admission_lock = threading.RLock()
        if self.catalog:
            for row in self.catalog.all():
                if row.get("kind") == "observation":
                    continue
                self._attach(row)
                # A crash may have followed intent persistence but preceded local Bots.disable.
                # Seal every dynamic row, including disabled and unknown rows, on every restart.
                with closing(self.p.bots._db()) as db:
                    db.execute("UPDATE connections SET enabled=0 WHERE id=?", (row["id"],))
                    db.commit()
                # Persisted readiness alone cannot prove either remote participant still agrees.
                if row["enabled"]:
                    row["state"] = "unknown"
                    row["enabled"] = False
                    row["pending"] = {"desired": True, "request_id": row["last_request_id"]}
                    self.catalog.put(row)

    def _row_lock(self, row_id):
        _id(row_id)
        require(self.catalog.get(row_id) is not None, "not_found", 404)
        return self.row_locks.setdefault(row_id, asyncio.Lock())

    def _live(self, row):
        current = self.catalog.get(row["id"])
        managed = self.p.role_runtime.get(row["actor_id"])
        return (
            current is not None
            and current["revision"] == row["revision"]
            and current["enabled"]
            and not current["pending"]
            and current["state"] in {"ready", "degraded"}
            and (managed is None or self.p.role_runtime.active(row["actor_id"]))
        )

    def _attach(self, row):
        bots, auth, sources = self.p.bots, self.p.auth, self.p.sources
        slot_id = "adapter:" + row["id"]
        binding_id = "binding:" + row["id"]
        self.p.contracts.check("common#id", slot_id)
        self.p.contracts.check("common#id", binding_id)
        self.p.contracts.check("common#id", row["actor_id"])
        require(
            slot_id not in bots.slots
            and binding_id
            not in {value["channel"]["binding_id"] for value in auth.entries.values()},
            "scope_changed",
            409,
        )
        channel = {
            "namespace": "qq",
            "binding_id": binding_id,
            "channel_conversation_id": f"{row['conversation']['kind']}:{row['conversation']['id']}",
            "thread_id": None,
        }
        self.p.contracts.check("common#channel_key", channel)
        audience = "group" if row["conversation"]["kind"] == "group" else "self_private"
        input_ids = []
        for index, author in enumerate(row["allowed_authors"]):
            entry_id = f"{row['id']}:author:{index}"
            actor_entry_id = f"{entry_id}:actor"
            self.p.contracts.check("common#id", entry_id)
            self.p.contracts.check("common#id", actor_entry_id)
            require(
                entry_id not in sources.entries and actor_entry_id not in auth.entries,
                "scope_changed",
                409,
            )
            account = {"namespace": "qq", "immutable_account_id": author}
            self.p.contracts.check("common#account", account)
            auth.entries[actor_entry_id] = {
                "kind": "trusted_application",
                "owner": bots.config["principal"],
                "account": account,
                "channel": channel,
                "actor_id": row["actor_id"],
                "audience": audience,
                "ttl_seconds": 60,
                "routes": [
                    {"caller": "platform", "receiver": "companion", "purpose": "dialogue"},
                    {"caller": "companion", "receiver": "memory", "purpose": "dialogue"},
                    {"caller": "companion", "receiver": "platform", "purpose": "dialogue"},
                ],
            }
            sources.entries[entry_id] = {
                "owner": bots.config["principal"],
                "account": account,
                "channel": channel,
                "audience": audience,
                "ttl_seconds": 60,
                "actor_entries": [actor_entry_id],
                "default_actor_ids": [row["actor_id"]],
                "routing_version": 1,
            }
            input_ids.append(entry_id)
        bots.slots[slot_id] = {
            "adapter": row["adapter"],
            "platform_id": row["instance_id"],
            "self_id": row["account_id"],
            "input_entry_ids": input_ids,
            "label": row["name"],
        }

    def _gate(self, console, session):
        require(self.catalog is not None, "management_disabled", 503)
        code = self.p.bots._web_code(console, session)
        require(code == "ready", code, 403)
        require(console.session_valid(session), "session_expired", 401)

    def _project(self, row):
        return {
            key: row[key]
            for key in (
                "id",
                "name",
                "adapter",
                "address",
                "account_id",
                "conversation",
                "allowed_authors",
                "actor_id",
                "enabled",
                "revision",
                "state",
                "last_error",
                "last_checked_at",
            )
        }

    @staticmethod
    def _draft_owner(session):
        return session.setdefault("bot_adapter_draft_nonce", secrets.token_hex(16))

    async def route(self, console, path, body, session):
        require(isinstance(body, dict), "invalid_input", 400)
        if path == PREFIX + "view":
            require(body == {}, "invalid_input", 400)
            code = self.p.bots._web_code(console, session)
            available = self.catalog is not None and code != "operator_not_authorized"
            if available:
                for row in self.catalog.all():
                    if row.get("kind") == "observation":
                        continue
                    if row["state"] == "unknown":
                        async with self._row_lock(row["id"]):
                            current = self.catalog.get(row["id"])
                            if current["state"] == "unknown":
                                await self._reconcile(current)
            return {
                "available": available,
                "unlocked": available and code == "ready",
                "actors": [
                    item
                    for item in self.config["actors"]
                    if not is_placeholder_actor(item["id"], self.p.role_runtime.get(item["id"]))
                ]
                + self.p.role_runtime.active_actors()
                if available
                else [],
                "connections": [
                    self._project(row)
                    for row in self.catalog.all()
                    if row.get("kind") != "observation"
                ]
                if available
                else [],
            }
        self._gate(console, session)
        if path == PREFIX + "probe":
            require(
                set(body) == {"adapter", "address", "access_key", "allow_private_http", "ca_pem"},
                "invalid_input",
                400,
            )
            return await self.probe(body, session)
        if path == PREFIX + "create":
            require(
                set(body)
                == {
                    "draft_id",
                    "name",
                    "account_id",
                    "conversation",
                    "allowed_authors",
                    "actor_id",
                    "client_id",
                },
                "invalid_input",
                400,
            )
            async with self.lock:
                return {"connection": self._project(await self.create(body, session))}
        if path == PREFIX + "reconcile":
            require(set(body) == {"id", "expected_revision", "client_id"}, "invalid_input", 400)
            async with self._row_lock(body["id"]):
                return {"connection": self._project(await self.reconcile(body))}
        operation = path.removeprefix(PREFIX)
        require(operation in {"enable", "disable"}, "not_found", 404)
        require(set(body) == {"id", "expected_revision", "client_id"}, "invalid_input", 400)
        async with self._row_lock(body["id"]):
            return {"connection": self._project(await self.change(operation, body))}

    async def _call(self, address, key, pins, ca_pem, path, payload, *, core=False, ca_file=None):
        # Match the host RPC's bounded inline-media request allowance.
        media_paths = {RPC + "/messages/send", RPC + "/observation/messages/send"}
        limit = 45 * 1024 * 1024 if not core and path in media_paths else 65536
        require(len(json.dumps(payload).encode()) <= limit, "invalid_input", 400)
        tls = ssl.create_default_context(cafile=ca_file, cadata=ca_pem)
        connector = (
            aiohttp.TCPConnector(resolver=PinnedResolver(pins), ssl=tls)
            if pins
            else aiohttp.TCPConnector(ssl=tls)
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT), trust_env=False, connector=connector
            ) as client:
                async with client.post(
                    address.rstrip("/") + path,
                    json=payload,
                    headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
                    allow_redirects=False,
                ) as response:
                    require(
                        response.status not in {301, 302, 303, 307, 308}, "adapter_redirect", 502
                    )
                    if not core and response.status == 404:
                        raise Fault("adapter_not_installed", 404)
                    if not core and response.status == 401:
                        raise Fault("adapter_unauthorized", 401)
                    require(
                        response.content_type == "application/json", "adapter_incompatible", 502
                    )
                    data = await response.content.read(65537)
                    require(len(data) <= 65536, "adapter_incompatible", 502)
                    result = loads(data)
                    require(isinstance(result, dict), "adapter_incompatible", 502)
                    if response.status != 200:
                        code = result.get("code")
                        if response.status == 401:
                            raise Fault(
                                "adapter_unauthorized" if not core else "dependency_unavailable",
                                401 if not core else 503,
                            )
                        if response.status == 404 and not core:
                            raise Fault("adapter_not_installed", 404)
                        if response.status == 400 and not core:
                            raise Fault("adapter_incompatible", 502)
                        if core and response.status in {400, 403}:
                            raise Fault("bot_role_not_approved", 403)
                        if response.status == 409 and code in {
                            "version_conflict",
                            "idempotency_conflict",
                        }:
                            raise Fault(code, 409)
                        raise Fault(
                            "adapter_unavailable" if not core else "dependency_unavailable", 503
                        )
                    return result
        except Fault:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ssl.SSLError, ValueError):
            raise Fault(
                "adapter_unreachable" if not core else "dependency_unavailable", 503
            ) from None

    async def _plugin(self, row, path, payload):
        current = await reviewed_pins(row["address"], self.config["allowed_cidrs"])
        require(current == row["pins"], "external_target_changed", 503)
        assert_pins(row["address"], row["pins"], self.config["allowed_cidrs"])
        if path == "/messages/send":
            # This is the final synchronous admission point before starting the SDK RPC.
            require(self._live(row), "connection_disabled", 409)
        return await self._call(
            row["address"], row["access_key"], row["pins"], row["ca_pem"], RPC + path, payload
        )

    async def _core(self, path, payload):
        settings = self.p.settings["core"]
        core_settings(settings)
        token = secret(settings["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        # The same authenticated platform caller already used for source dispatch.
        return await self._call(
            settings["base_url"],
            token,
            None,
            None,
            "/internal/v1/bot-bindings/" + path,
            payload,
            core=True,
            ca_file=settings.get("ca_file"),
        )

    async def probe(self, body, session):
        now = time.time()
        self.drafts = {key: item for key, item in self.drafts.items() if item[1] > now}
        require(len(self.drafts) < 128, "queue_full", 429)
        require(body["adapter"] in {"astrbot", "nonebot"}, "invalid_input", 400)
        require(type(body["allow_private_http"]) is bool, "invalid_input", 400)
        address = _url(body["address"], "home", body["allow_private_http"])
        require(address.startswith("https://") or body["allow_private_http"], "invalid_input", 400)
        key = body["access_key"]
        require(
            isinstance(key, str)
            and 24 <= len(key) <= 4096
            and key.isascii()
            and all(33 <= ord(c) <= 126 for c in key),
            "invalid_input",
            400,
        )
        ca = body["ca_pem"]
        require(ca is None or (isinstance(ca, str) and 1 <= len(ca) <= 16384), "invalid_input", 400)
        if ca is not None:
            try:
                ssl.create_default_context(cadata=ca)
            except (ValueError, ssl.SSLError):
                raise Fault("invalid_input", 400) from None
        pins = await reviewed_pins(address, self.config["allowed_cidrs"])
        result = await self._call(address, key, pins, ca, RPC + "/capabilities", {})
        require(
            result.get("protocol") == PROTOCOL
            and result.get("adapter") == body["adapter"]
            and isinstance(result.get("instance_id"), str)
            and 1 <= len(result["instance_id"]) <= 128
            and "text" in result.get("capabilities", [])
            and type(result.get("max_outbound_utf8_bytes")) is int
            and result["max_outbound_utf8_bytes"] >= 32768,
            "adapter_incompatible",
            502,
        )
        accounts = result.get("accounts")
        require(isinstance(accounts, list) and len(accounts) <= 64, "adapter_incompatible", 502)
        for account in accounts:
            require(
                isinstance(account, dict)
                and set(account) == {"id", "platform", "label"}
                and account["platform"] == "qq"
                and isinstance(account["label"], str),
                "adapter_incompatible",
                502,
            )
            require(
                isinstance(account["id"], str)
                and re.fullmatch(r"[1-9][0-9]*", account["id"]) is not None,
                "adapter_incompatible",
                502,
            )
        draft_id = "draft:" + uuid.uuid4().hex
        expires = time.time() + 600
        draft = {
            "adapter": body["adapter"],
            "address": address,
            "access_key": key,
            "allow_private_http": body["allow_private_http"],
            "ca_pem": ca,
            "pins": pins,
            "instance_id": result["instance_id"],
            "accounts": accounts,
        }
        self.drafts[draft_id] = (
            self._draft_owner(session),
            expires,
            self.catalog._seal(draft, "draft:" + draft_id),
        )
        return {
            "draft_id": draft_id,
            "expires_at": utc(expires),
            "protocol": PROTOCOL,
            "instance_id": result["instance_id"],
            "accounts": accounts,
        }

    def _binding_request(self, row, desired):
        return {
            "request_id": row["last_request_id"],
            "connection_id": row["id"],
            "revision": row["revision"],
            "binding_id": "binding:" + row["id"],
            "actor_id": row["actor_id"],
            "conversation": row["conversation"],
            "enabled": desired,
        }

    def _plugin_request(self, row, desired):
        return {
            "request_id": row["last_request_id"],
            "connection_id": row["id"],
            "revision": row["revision"],
            "account_id": row["account_id"],
            "conversation": row["conversation"],
            "allowed_authors": row["allowed_authors"],
            "enabled": desired,
        }

    async def _apply(self, row):
        desired = row["pending"]["desired"]
        try:
            core = await self._core("apply", self._binding_request(row, desired))
            require(
                core
                == {"connection_id": row["id"], "revision": row["revision"], "enabled": desired},
                "dependency_unavailable",
                503,
            )
            plugin = await self._plugin(row, "/bindings/apply", self._plugin_request(row, desired))
            require(
                plugin
                == {"connection_id": row["id"], "revision": row["revision"], "enabled": desired},
                "adapter_incompatible",
                502,
            )
            return self._finish(row, desired)
        except Fault as exc:
            row["state"] = "unknown"
            row["last_error"] = exc.code
            self.catalog.put(row)
            return row

    def _finish(self, row, desired):
        with closing(self.p.bots._db()) as db:
            exists = db.execute("SELECT 1 FROM connections WHERE id=?", (row["id"],)).fetchone()
        if not exists:
            # A rev1 disabled create may have stopped between private catalog and Bots insert.
            # Never rebuild a later connection: losing its delivery ledger could duplicate SDK
            # sends after Core replays an old reply.
            require(row["revision"] == 1 and not desired, "dependency_unavailable", 503)
            self.p.bots.create(
                "adapter:" + row["id"],
                [row["actor_id"]],
                connection_id=row["id"],
                token=row["bot_token"],
            )
        if desired:
            try:
                self.p.bots.change(row["id"], "enable")
            except Fault as exc:
                row["state"] = "unknown"
                row["last_error"] = exc.code
                self.catalog.put(row)
                return row
        row["enabled"] = desired
        row["state"] = "ready" if desired else "disabled"
        row["pending"] = None
        row["last_error"] = None
        row["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        self.catalog.put(row)
        return row

    async def _reconcile(self, row):
        pending = row.get("pending")
        if not pending:
            return row
        desired = pending["desired"]
        try:
            core = await self._core("status", {"connection_id": row["id"]})
            plugin = await self._plugin(row, "/bindings/status", {"connection_id": row["id"]})
            expected = {"connection_id": row["id"], "revision": row["revision"], "enabled": desired}
            if core == {"found": True, "binding": expected} and plugin == {
                "found": True,
                "binding": expected,
            }:
                return self._finish(row, desired)
        except Fault as exc:
            row["last_error"] = exc.code
        self.catalog.put(row)
        return row

    async def create(self, body, session):
        client_id = _client_id(body["client_id"])
        fingerprint = digest(body)
        receipt = self.catalog.receipt(client_id, fingerprint)
        if receipt:
            return self.catalog.get(receipt["id"])
        require(len(self.catalog.all()) < 64, "queue_full", 429)
        draft = self.drafts.get(body["draft_id"])
        require(
            draft and draft[0] == self._draft_owner(session) and draft[1] > time.time(),
            "draft_expired",
            409,
        )
        value = self.catalog._open(draft[2], "draft:" + body["draft_id"])
        name, account_id, actor_id = body["name"], body["account_id"], body["actor_id"]
        require(
            isinstance(name, str) and 1 <= len(name) <= 64 and name.isprintable(),
            "invalid_input",
            400,
        )
        _qq_id(account_id)
        require(any(a["id"] == account_id for a in value["accounts"]), "invalid_input", 400)
        require(
            actor_id
            in {a["id"] for a in self.config["actors"] + self.p.role_runtime.active_actors()},
            "forbidden",
            403,
        )
        conversation = body["conversation"]
        require(
            isinstance(conversation, dict)
            and set(conversation) == {"kind", "id"}
            and conversation["kind"] in {"group", "private"},
            "invalid_input",
            400,
        )
        _qq_id(conversation["id"])
        authors = body["allowed_authors"]
        require(
            isinstance(authors, list)
            and 1 <= len(authors) <= 32
            and len(set(authors)) == len(authors),
            "invalid_input",
            400,
        )
        for author in authors:
            _qq_id(author)
        require(
            conversation["kind"] != "private" or authors == [conversation["id"]],
            "invalid_input",
            400,
        )
        row_id = "bot:" + uuid.uuid4().hex
        row = {
            key: value[key]
            for key in (
                "adapter",
                "address",
                "access_key",
                "allow_private_http",
                "ca_pem",
                "pins",
                "instance_id",
            )
        }
        row.update(
            id=row_id,
            name=name,
            account_id=account_id,
            conversation=conversation,
            allowed_authors=authors,
            actor_id=actor_id,
            enabled=False,
            revision=1,
            state="unknown",
            last_error=None,
            last_checked_at=None,
            last_request_id=client_id,
            pending={"desired": False, "request_id": client_id},
            bot_token=secrets.token_urlsafe(48),
        )
        prior = self.catalog.begin(row, client_id, fingerprint)
        if prior:
            return self.catalog.get(prior["id"])
        async with self._row_lock(row_id):
            self._attach(row)
            self.p.bots.create(
                "adapter:" + row_id, [actor_id], connection_id=row_id, token=row["bot_token"]
            )
            row = await self._apply(row)
        self.drafts.pop(body["draft_id"], None)
        return row

    async def change(self, operation, body):
        _client_id(body["client_id"])
        fingerprint = digest([operation, body])
        receipt = self.catalog.receipt(body["client_id"], fingerprint)
        if receipt:
            return self.catalog.get(receipt["id"])
        row = self.catalog.get(body["id"])
        require(row is not None, "not_found", 404)
        require(type(body["expected_revision"]) is int, "invalid_input", 400)
        require(body["expected_revision"] == row["revision"], "version_conflict", 409)
        desired = operation == "enable"
        require(not row["pending"], "result_unknown", 409)
        require(row["enabled"] != desired, "version_conflict", 409)
        row["revision"] += 1
        row["state"] = "unknown"
        row["enabled"] = False
        row["last_request_id"] = body["client_id"]
        row["pending"] = {"desired": desired, "request_id": body["client_id"]}
        with self.admission_lock:
            prior = self.catalog.begin(row, body["client_id"], fingerprint)
            if prior:
                return self.catalog.get(prior["id"])
            if not desired:
                self.p.bots.change(row["id"], "disable")
        row = await self._apply(row)
        return row

    async def reconcile(self, body):
        _client_id(body["client_id"])
        fingerprint = digest(["reconcile", body])
        receipt = self.catalog.receipt(body["client_id"], fingerprint)
        if receipt:
            return self.catalog.get(receipt["id"])
        row = self.catalog.get(body["id"])
        require(row is not None, "not_found", 404)
        require(type(body["expected_revision"]) is int, "invalid_input", 400)
        require(body["expected_revision"] == row["revision"], "version_conflict", 409)
        require(row["state"] == "unknown" and row["pending"], "version_conflict", 409)
        prior = self.catalog.begin(row, body["client_id"], fingerprint)
        if prior:
            return self.catalog.get(prior["id"])
        row = await self._reconcile(row)
        if row["pending"]:
            # The operator requested recovery of exactly the persisted write. Reuse its
            # original request_id/revision/desired; peers guarantee semantic idempotency.
            row = await self._apply(row)
        return row

    async def pump_once(self):
        if not self.catalog:
            return
        for row in self.catalog.all():
            if row.get("kind") == "observation":
                continue
            if row["state"] == "unknown":
                async with self._row_lock(row["id"]):
                    current = self.catalog.get(row["id"])
                    if current["state"] == "unknown":
                        await self._reconcile(current)
                continue
            if not self._live(row):
                continue
            try:
                events = await self._plugin(
                    row, "/events/poll", {"connection_id": row["id"], "limit": 20}
                )
                require(
                    isinstance(events.get("events"), list) and len(events["events"]) <= 20,
                    "adapter_incompatible",
                    502,
                )
                acknowledged = []
                for item in events["events"]:
                    if not self._live(row):
                        break
                    event = item["event"]
                    outcome = await self.p.bots.event("Bearer " + row["bot_token"], event)
                    if outcome["state"] == "accepted":
                        acknowledged.append(item["id"])
                    elif outcome["state"] == "unknown":
                        status = await self.p.local_work.run(
                            self.p.bots.event_status,
                            "Bearer " + row["bot_token"],
                            {
                                "connection_id": row["id"],
                                "event_id": event["event_id"],
                                "account_id": event["account_id"],
                            },
                        )
                        if status["state"] == "accepted":
                            acknowledged.append(item["id"])
                if acknowledged:
                    await self._plugin(
                        row,
                        "/events/ack",
                        {"connection_id": row["id"], "event_ids": acknowledged},
                    )
                if not self._live(row):
                    continue
                claimed = await self.p.local_work.run(
                    self.p.bots.claim,
                    "Bearer " + row["bot_token"],
                    {"connection_id": row["id"], "instance_id": "platform-pump", "limit": 20},
                )
                for delivery in claimed["deliveries"]:
                    if not self._live(row):
                        break
                    receipt = None
                    try:
                        receipt = await self._plugin(
                            row,
                            "/messages/send",
                            {"connection_id": row["id"], "delivery": delivery},
                        )
                    except Fault as exc:
                        if exc.code == "connection_disabled":
                            break
                        try:
                            status = await self._plugin(
                                row,
                                "/messages/status",
                                {
                                    "connection_id": row["id"],
                                    "reply_id": delivery["reply_id"],
                                    "attempt_id": delivery["attempt_id"],
                                },
                            )
                            receipt = status.get("receipt") if status.get("found") else None
                        except Fault:
                            pass
                    if not receipt:
                        receipt = {
                            "reply_id": delivery["reply_id"],
                            "attempt_id": delivery["attempt_id"],
                            "state": "unknown",
                            "channel_message_ids": [],
                        }
                    require(
                        receipt.get("reply_id") == delivery["reply_id"]
                        and receipt.get("attempt_id") == delivery["attempt_id"]
                        and receipt.get("state") in {"sent", "failed", "unknown"},
                        "adapter_incompatible",
                        502,
                    )
                    await self.p.local_work.run(
                        self.p.bots.ack,
                        "Bearer " + row["bot_token"],
                        {
                            "connection_id": row["id"],
                            "reply_id": delivery["reply_id"],
                            "attempt_id": delivery["attempt_id"],
                            "state": receipt["state"],
                            "channel_message_ids": receipt.get("channel_message_ids", []),
                        },
                    )
                row["last_checked_at"] = datetime.now(timezone.utc).isoformat()
                row["last_error"] = None
                row["state"] = "ready"
            except (Fault, KeyError, TypeError) as exc:
                row["last_error"] = exc.code if isinstance(exc, Fault) else "adapter_incompatible"
                row["state"] = "degraded"
            self.catalog.observe(row)
