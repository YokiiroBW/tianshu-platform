"""Managed reverse WebSocket endpoints backed by the existing encrypted catalog."""

import asyncio
import hmac
import re
import secrets
import time
import uuid

from .contracts import Fault, digest, require


class OneBotServers:
    def __init__(self, adapters):
        self.adapters = adapters
        self.catalog = adapters.catalog
        self.lock = asyncio.Lock()
        self.reservations = {}
        self.status_provider = None

    def rows(self):
        return self.catalog.servers() if self.catalog else []

    def row(self, server_id):
        row = self.catalog.server(server_id) if self.catalog else None
        return row if row and row.get("kind") == "onebot_server" else None

    async def create(self, body):
        require(set(body) == {"name", "token", "client_id"}, "invalid_input", 400)
        name, token, client_id = body["name"], body["token"], body["client_id"]
        require(
            isinstance(name, str) and 1 <= len(name) <= 64 and name.isprintable(),
            "invalid_input",
            400,
        )
        require(
            isinstance(token, str)
            and (not token or 16 <= len(token) <= 256)
            and token.isascii()
            and all(33 <= ord(char) <= 126 for char in token),
            "invalid_input",
            400,
        )
        require(isinstance(client_id, str) and 1 <= len(client_id) <= 128, "invalid_input", 400)
        fingerprint = digest(["onebot-server-create", body])
        async with self.lock:
            receipt = self.catalog.receipt(client_id, fingerprint)
            if receipt:
                return self.row(receipt["id"])
            require(len(self.rows()) < 64, "queue_full", 429)
            row = {
                "kind": "onebot_server",
                "id": "onebot:" + uuid.uuid4().hex,
                "name": name,
                "token": token or secrets.token_urlsafe(32),
                "account_id": None,
            }
            prior = self.catalog.begin_server(row, client_id, fingerprint)
            return self.row(prior["id"]) if prior else row

    @staticmethod
    def _matches(row, token, account_id):
        return bool(
            row
            and isinstance(token, str)
            and token.isascii()
            and isinstance(account_id, str)
            and re.fullmatch(r"[1-9][0-9]*", account_id)
            and hmac.compare_digest(row["token"], token)
            and (row["account_id"] is None or row["account_id"] == account_id)
        )

    async def authorize(self, server_id, token, account_id):
        return self._matches(self.row(server_id), token, account_id)

    async def reserve(self, server_id, token, account_id):
        async with self.lock:
            row = self.row(server_id)
            if not self._matches(row, token, account_id):
                return None
            existing = self.reservations.get(server_id)
            if existing and existing[1] > time.monotonic():
                return None
            reservation = secrets.token_hex(16)
            self.reservations[server_id] = (reservation, time.monotonic() + 30)
            return reservation

    async def claim(self, server_id, token, account_id, reservation):
        # Persist only after the SDK accepts the socket. A reservation prevents
        # concurrent handshakes from binding different identities to one server.
        async with self.lock:
            existing = self.reservations.get(server_id)
            if not existing or existing[0] != reservation or existing[1] <= time.monotonic():
                return False
            try:
                row = self.row(server_id)
                if not self._matches(row, token, account_id):
                    return False
                if row["account_id"] is None:
                    row["account_id"] = account_id
                    self.catalog.put_server(row)
                return True
            finally:
                self.reservations.pop(server_id, None)

    async def release(self, server_id, reservation):
        async with self.lock:
            existing = self.reservations.get(server_id)
            if existing and existing[0] == reservation:
                self.reservations.pop(server_id, None)

    async def snapshot(self):
        if self.status_provider is None:
            return {}
        try:
            return await self.status_provider()
        except (RuntimeError, OSError, TimeoutError):
            return {}

    async def view(self):
        owned = await self.adapters._owned_onboarding()
        if owned is None or self.status_provider is None:
            return {"available": False, "servers": []}
        instance_id, details = owned
        connected = await self.snapshot()
        observations = self.adapters.p.bot_observation.rows()
        servers = []
        for row in self.rows():
            observation = next(
                (
                    item
                    for item in observations
                    if item["instance_id"] == instance_id
                    and item["account_id"] == row["account_id"]
                ),
                None,
            )
            servers.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "token": row["token"],
                    "account_id": row["account_id"],
                    "connected": connected.get(row["id"]) == row["account_id"]
                    and row["account_id"] is not None,
                    "observation_id": observation["id"] if observation else None,
                    "observation_state": observation["state"] if observation else None,
                    "ws_path": "/onebot/v11/ws/" + row["id"].split(":", 1)[1],
                }
            )
        return {"available": True, "ws_port": details["ws_port"], "servers": servers}

    async def synchronize(self):
        """Enroll authenticated live accounts through the existing observation owner."""
        if self.status_provider is None or not self.rows():
            return
        connected = await self.snapshot()
        if not connected:
            return
        owned = await self.adapters._owned_onboarding()
        if owned is None:
            return
        instance_id, _ = owned
        observation = self.adapters.p.bot_observation
        for row in self.rows():
            account = row["account_id"]
            if not account or connected.get(row["id"]) != account:
                continue
            if any(
                existing["instance_id"] == instance_id and existing["account_id"] == account
                for existing in observation.rows()
            ):
                continue
            session, probe = {}, None
            try:
                probe = await self.adapters.probe_owned(session)
                if not any(item["id"] == account for item in probe["accounts"]):
                    continue
                async with observation.lock:
                    if any(
                        existing["instance_id"] == instance_id and existing["account_id"] == account
                        for existing in observation.rows()
                    ):
                        continue
                    await observation.create(
                        {
                            "draft_id": probe["draft_id"],
                            "account_id": account,
                            "name": row["name"],
                            "client_id": "onebot-observation:" + uuid.uuid4().hex,
                        },
                        session,
                    )
            except (Fault, OSError, TimeoutError):
                # The existing observation pump reconciles persisted pending
                # intent; otherwise the next iteration retries discovery.
                continue
            finally:
                if probe:
                    self.adapters.drafts.pop(probe["draft_id"], None)
