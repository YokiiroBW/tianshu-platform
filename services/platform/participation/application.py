"""Capture and semantic intent; silence is the default until a real consumer engages."""

import asyncio
import sqlite3
from weakref import WeakValueDictionary
from contextlib import closing

from ..contracts import Fault, canonical, digest, epoch, require, utc
from . import current
from .protocol import MANIFEST, WINDOW_SECONDS, candidate_ref, decision_ref, protocol
from .repository import Repository


class Participation:
    def __init__(self, platform):
        self.p = platform
        self.protocol = protocol(platform.contracts)
        self.enabled = bool(platform.bots.config and platform.bot_observation.catalog)
        self.last_error = None
        self.locks = WeakValueDictionary()
        self.repository = Repository(platform.bots) if self.enabled else None
        if self.enabled:
            platform.sources.initialize_participation()
        from .admission import Admission
        from .delivery import Delivery

        self.admission = Admission(self)
        self.delivery = Delivery(self)

    def available(self):
        if not self.enabled:
            return False
        try:
            self.protocol.check("id", "participation:status")
            return True
        except (Fault, OSError, ValueError, KeyError):
            return False

    async def rpc(self, row, operation, value):
        require(len(canonical(value).encode()) <= 65536, "budget_exceeded", 413)
        return await self.p.bot_adapters._plugin(row, "/participation/" + operation, value)

    def check(self, record):
        require(self.available(), "dependency_unavailable", 503)
        return current.check(self, record)

    def guard(self, header):
        with self.p.store.connect() as db:
            identity, principal = self.p.auth.authenticate(header, db, "dialogue.send")
            require(
                principal["kind"] == "service" and principal["service"] == "companion",
                "forbidden",
                403,
            )
            return identity

    async def observe(self, row, event, source):
        """An optional projection must never fail the original archive/reply/ACK path."""
        if (
            not self.available()
            or event["mentioned"]
            or event["content_state"] != "text"
            or not event["conversation_id"].startswith("group:")
        ):
            return
        try:
            from ..bot_observation import decision

            target = event["conversation_id"].split(":", 1)[1]
            verdict = decision(row["group_policy"], target, False, "group", event["text"])
            actor = self.p.bot_observation.actor(row, event["conversation_id"])
            if (
                not actor
                or not verdict["proactive_permitted"]
                or verdict["reply_triggered"]
                or not self.p.bot_observation.same_capture_policy(row, event)
            ):
                return
            capabilities = await self.rpc(
                row, "capabilities", {"account_id": row["account_id"], "group_id": target}
            )
            require(
                capabilities.get("available") is True
                and capabilities.get("manifest_sha256") == MANIFEST,
                "dependency_unavailable",
                503,
            )
            if capabilities.get("claim_supported") is not True:
                self.last_error = "ordinary_claim_unavailable"
                return
            configuration = await self.p.local_work.run(
                current.configuration, self.p, row, actor, capabilities["host_generation"], target
            )
            now = self.p.origins.clock()
            candidate = {
                "schema_version": 1,
                "kind": "participation_candidate",
                "candidate_id": "participation:"
                + digest([source, actor, configuration, capabilities["host_epoch"]]),
                "actor_id": actor,
                "trigger": "ordinary_group_message",
                "native": {
                    "framework": capabilities["framework"],
                    "instance_id": event["platform_id"],
                    "namespace": "qq",
                    "self_id": event["self_id"],
                    "group_id": target,
                    "native_message_id": event["event_id"],
                    "author_account_id": event["account_id"],
                    "message_revision": event["revision"],
                },
                "source": source,
                "author_anchor": {
                    "stage": "observed",
                    "account_id": event["account_id"],
                    "group_id": target,
                    "entry_id": None,
                    "person_id": None,
                    "conversation_id": None,
                    "binding_version": None,
                },
                "configuration": configuration,
                "host_epoch": capabilities["host_epoch"],
                "sent_at": event["sent_at"],
                "created_at": utc(now),
                "expires_at": utc(now + WINDOW_SECONDS),
                "mentioned": False,
                "text": event["text"],
            }
            self.protocol.response("candidate", candidate)
            candidate = await self.p.local_work.run(
                self.repository.capture, row["id"], candidate, event, now, now + WINDOW_SECONDS
            )
            saved = await self.p.local_work.run(self.repository.get, candidate["candidate_id"])
            live = await self.p.local_work.run(self.check, saved)
            answer = await self.rpc(live, "candidate", candidate)
            require(answer.get("candidate") == candidate, "adapter_incompatible", 502)
            self.last_error = None
        except (Fault, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            self.last_error = error.code if isinstance(error, Fault) else "dependency_unavailable"

    async def consumer_status(self):
        try:
            value = await self.p.bot_adapters._core("participation-status", {})
            require(
                set(value) == {"consumer", "available", "reason", "last_error"}
                and value["consumer"] == "integrated"
                and type(value["available"]) is bool,
                "dependency_unavailable",
                503,
            )
            return value
        except (Fault, OSError, ValueError, KeyError, TypeError):
            return dict(
                consumer="unavailable",
                available=False,
                reason="consumer_unavailable",
                last_error=None,
            )

    async def route(self, header, operation, body):
        self.guard(header)
        if operation == "status":
            require(not body, "invalid_input", 400)
            return {
                "state": "available" if self.available() else "not_configured",
                "consumer": await self.consumer_status(),
                "last_error": self.last_error,
            }
        if operation == "candidates":
            require(
                set(body) == {"actor_id", "after", "limit"}
                and type(body["limit"]) is int
                and 1 <= body["limit"] <= 20
                and isinstance(body["after"], str),
                "invalid_input",
                400,
            )
            self.p.contracts.check("common#id", body["actor_id"])
            if not self.available():
                return {"state": "not_configured", "candidates": [], "next": None}
            with closing(self.p.bots._db()) as db:
                rows = db.execute(
                    "SELECT id FROM participation_candidates WHERE id>? AND expires_at>? ORDER BY id LIMIT ?",
                    (body["after"], self.p.origins.clock(), body["limit"]),
                ).fetchall()
            found = []
            for row in rows:
                record = await self.p.local_work.run(self.repository.get, row["id"])
                if record["document"]["actor_id"] != body["actor_id"]:
                    continue
                try:
                    await self.p.local_work.run(self.check, record)
                except Fault:
                    continue
                found.append(record["document"])
            return {
                "state": "available",
                "candidates": found,
                "next": rows[-1]["id"] if len(rows) == body["limit"] else None,
            }
        require(operation == "decision", "not_found", 404)
        require(self.available(), "dependency_unavailable", 503)
        self.protocol.response("semantic_decision", body)
        candidate_id = body["candidate"]["candidate_id"]
        lock = self.locks.setdefault(candidate_id, asyncio.Lock())
        async with lock:
            return await self.decide(body)

    async def decide(self, document):
        record = await self.p.local_work.run(
            self.repository.get, document["candidate"]["candidate_id"]
        )
        row = await self.p.local_work.run(self.check, record)
        candidate = record["document"]
        now = epoch(utc(self.p.origins.clock()))
        require(
            document["candidate"] == candidate_ref(candidate)
            and epoch(candidate["created_at"]) <= epoch(document["decided_at"]) <= now
            and now < epoch(document["expires_at"]) <= epoch(candidate["expires_at"]),
            "scope_changed",
            409,
        )
        await self.p.local_work.run(
            self.repository.save_decision, candidate["candidate_id"], document
        )
        if document["outcome"] != "engage":
            return {"state": document["outcome"], "decision": document}
        answer = await self.rpc(row, "candidate", candidate)
        require(answer.get("candidate") == candidate, "adapter_incompatible", 502)
        answer = await self.rpc(row, "decision", document)
        require(answer.get("decision") == document, "adapter_incompatible", 502)
        record = await self.p.local_work.run(self.repository.get, candidate["candidate_id"])
        if record["claim_request"] is None:
            result = await self.rpc(row, "ownership", {"native": candidate["native"]})
            require(result.get("found") is True, "dependency_unavailable", 503)
            self.protocol.check("host_ownership", result["ownership"])
            owner = result["ownership"]["ownership"]
            require(
                owner["native"] == candidate["native"]
                and owner["host_epoch"] == candidate["host_epoch"]
                and owner["host_generation"] == candidate["configuration"]["host_generation"],
                "scope_changed",
                409,
            )
            if owner["state"] != "free":
                return {
                    "state": "waiting" if owner["state"] == "processing" else "unavailable",
                    "decision": document,
                }
            request = {
                "schema_version": 1,
                "kind": "participation_claim_request",
                "request_id": "claim:" + digest([candidate_ref(candidate), decision_ref(document)]),
                "candidate": candidate_ref(candidate),
                "decision": decision_ref(document),
                "native": candidate["native"],
                "host_epoch": candidate["host_epoch"],
                "host_generation": candidate["configuration"]["host_generation"],
                "expected_owner_revision": owner["owner_revision"],
                "expires_at": document["expires_at"],
            }
            self.protocol.response("claim_request", request)
            await self.p.local_work.run(
                self.repository.claim_request, candidate["candidate_id"], request
            )
        record = await self.p.local_work.run(self.repository.get, candidate["candidate_id"])
        result = await self.refresh_claim(record)
        if result["result"] != "granted" or result["current_claim_state"] != "active":
            return {"state": "unavailable", "claim": result}
        row = await self.p.local_work.run(self.check, record)
        conn, token = await self.p.bot_observation._ensure_reply_scope(
            row,
            record["event"]["conversation_id"],
            candidate["native"]["author_account_id"],
            candidate["actor_id"],
        )
        await self.refresh_claim(
            await self.p.local_work.run(self.repository.get, candidate["candidate_id"]), active=True
        )
        body = self.p.bot_observation.reply_body(conn, record["event"])
        outcome = await self.p.bots.event(
            "Bearer " + token, body, participation=candidate["candidate_id"]
        )
        return {"state": outcome["state"], "event": outcome, "claim": result}

    async def refresh_claim(self, record, *, active=False):
        row = await self.p.local_work.run(self.check, record)
        request = record["claim_request"]
        require(request is not None, "dependency_unavailable", 503)
        result = await self.rpc(row, "claim", request)
        self.protocol.response("claim_result", result)
        require(
            result["request_id"] == request["request_id"]
            and result["request_digest"] == digest(request)
            and result["candidate"] == request["candidate"]
            and result["decision"] == request["decision"],
            "adapter_incompatible",
            502,
        )
        if result["claim"] is not None:
            require(
                result["claim"]["candidate"] == request["candidate"]
                and result["claim"]["decision"] == request["decision"]
                and result["claim"]["host_epoch"] == request["host_epoch"]
                and result["claim"]["host_generation"] == request["host_generation"]
                and epoch(result["claim"]["expires_at"]) <= epoch(request["expires_at"]),
                "adapter_incompatible",
                502,
            )
            if result["current_claim_state"] == "active":
                owner = result["ownership"]
                require(
                    owner["claim"] == result["claim"]
                    and owner["native"] == request["native"]
                    and owner["host_epoch"] == request["host_epoch"]
                    and owner["host_generation"] == request["host_generation"],
                    "adapter_incompatible",
                    502,
                )
        await self.p.local_work.run(self.repository.claim_result, record["id"], result)
        await self.p.local_work.run(self.check, record)
        if active:
            require(
                result["result"] == "granted"
                and result["current_claim_state"] == "active"
                and epoch(result["claim"]["expires_at"]) > self.p.origins.clock(),
                "scope_changed",
                409,
            )
        return result
