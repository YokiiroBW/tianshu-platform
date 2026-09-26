"""Browser text adapter to published Platform sources and Core web snapshot ports."""

import asyncio
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .auth import secret
from .contracts import Fault, canonical, digest, epoch, loads, require, utc
from .transport import CoreFault, core_web_call


class WebDialogue:
    def __init__(self, platform):
        self.p = platform
        self.path = platform.store.path + ".web-inputs.sqlite"
        platform.contracts.load_web(
            Path(platform.settings["contract_directory"]).parents[1] / "web-conversation/v1"
        )
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, semantic TEXT NOT NULL, entry_id TEXT NOT NULL, actor_id TEXT NOT NULL, account TEXT NOT NULL, message_id TEXT NOT NULL, state TEXT NOT NULL, result TEXT)"
            )
            db.commit()

    def available(self):
        c = self.p.settings.get("web", {})
        return bool(c.get("dialogue_enabled") and self.p.settings.get("core"))

    def model_configured(self):
        if (
            self.p.provider_catalog is not None
            and self.p.provider_catalog.view()["default"]["configured"]
        ):
            return True
        with self.p.store.connect() as db:
            rows = db.execute("SELECT document FROM configs WHERE revoked=0").fetchall()
        now = self.p.origins.clock()
        return any(
            epoch(d["published_at"]) <= now < epoch(d["usable_until"]) and d["bindings"]
            for d in (loads(r[0]) for r in rows)
        )

    def submissions(self, body, entry):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute(
                "SELECT message_id,state,result FROM submissions WHERE entry_id=? AND actor_id=? AND account=? ORDER BY rowid DESC LIMIT 20",
                (body["conversation"], body["actor"], canonical(entry["account"])),
            ).fetchall()
        return [
            {"message_id": r[0], "state": r[1], "result": loads(r[2]) if r[2] else None}
            for r in rows
        ]

    def selection(self, body):
        p = self.p
        c = p.settings["web"]
        require(body.get("conversation") in c["input_entries"])
        header = "Bearer " + (secret(p.auth.principals[c["principal"]]["token_env"]) or "")
        with p.store.connect(write=True) as db:
            identity, _ = p.auth.authenticate(header, db, "source.register", operator=True)
            entry = p.sources._entry(db, body["conversation"])
            require(entry["owner"] == identity)
            actor_entry = next(
                (
                    k
                    for k in entry["actor_entries"]
                    if p.auth.entries[k]["actor_id"] == body.get("actor")
                ),
                None,
            )
            require(actor_entry is not None)
            actor = p.auth.entry(db, actor_entry)
            p.auth.route(actor, "platform", "companion", "dialogue")
            p.auth.route(actor, "companion", "memory", "dialogue")
            scope = p.origins.scope(db, actor)
        return header, entry, actor_entry, scope

    async def snapshot(self, body):
        require(set(body) == {"conversation", "actor", "before"}, "invalid_input", 400)
        require(
            body["before"] is None or type(body["before"]) is int and body["before"] > 0,
            "invalid_input",
            400,
        )
        require(self.available(), "core_web_not_connected", 503)
        header, entry, actor_entry, scope = await self.p.local_work.run(self.selection, body)
        if scope["conversation_id"] is None or scope["person_id"] is None:
            return {
                "snapshot": None,
                "state": "not_started",
                "submissions": await self.p.local_work.run(self.submissions, body, entry),
            }
        origin = await self.p.local_work.run(self.p.origins.issue, header, actor_entry)
        query = {
            "schema_version": 1,
            "query": {
                "schema_version": 1,
                "request_id": "query:" + uuid.uuid4().hex,
                "origin": {"assertion_ref": origin["assertion_ref"]},
            },
            "deadline_at": utc(self.p.origins.clock() + 30),
            "conversation_id": scope["conversation_id"],
            "actor_id": body["actor"],
            "before_turn_sequence": body["before"],
            "limit": 20,
        }
        self.p.contracts.check("web-conversation#snapshot_request", query)
        result = await core_web_call(
            self.p.settings["core"],
            "web-snapshot",
            query,
            self.p.contracts,
            "web-conversation#snapshot_response",
        )
        require(
            result["request_id"] == query["query"]["request_id"]
            and result["actor_id"] == body["actor"]
            and result["conversation_id"] == scope["conversation_id"],
            "dependency_unavailable",
            503,
        )
        _, current_entry, _, current_scope = await self.p.local_work.run(self.selection, body)
        require(current_entry == entry and current_scope == scope, "scope_changed", 409)
        await self.p.local_work.run(self._check_origin, origin["assertion_ref"])
        turns = result["history"] + result["active_turns"]
        terminal = {"sent", "failed", "cancelled", "observed", "closed_unknown"}
        require(
            all(t["turn"]["phase"] in terminal for t in result["history"])
            and all(t["turn"]["phase"] not in terminal for t in result["active_turns"]),
            "dependency_unavailable",
            503,
        )
        sequences = [t["turn"]["turn_sequence"] for t in result["history"]]
        require(
            sequences == sorted(sequences, reverse=True)
            and all(body["before"] is None or s < body["before"] for s in sequences),
            "dependency_unavailable",
            503,
        )
        ids = [t["turn"]["turn_id"] for t in turns]
        require(len(ids) == len(set(ids)), "dependency_unavailable", 503)
        for turn in turns:
            segments = [r["segment_sequence"] for r in turn["replies"]]
            require(len(segments) == len(set(segments)), "dependency_unavailable", 503)
            require(
                all(r["segment_sequence"] <= r["segment_count"] for r in turn["replies"]),
                "dependency_unavailable",
                503,
            )
        for group in turns + result["collectors"]:
            messages = [(m["message_id"], m["revision"]) for m in group["messages"]]
            require(len(messages) == len(set(messages)), "dependency_unavailable", 503)
        # Already schema-whitelisted: no origin, source proofs or generated drafts.
        return {
            "snapshot": result,
            "state": "current",
            "submissions": await self.p.local_work.run(self.submissions, body, entry),
        }

    async def send(self, body):
        require(set(body) == {"conversation", "actor", "text", "client_id"}, "invalid_input", 400)
        require(
            isinstance(body["text"], str)
            and 1 <= len(body["text"].strip()) <= 8000
            and len(body["text"]) <= 8000,
            "invalid_input",
            400,
        )
        require(
            isinstance(body["client_id"], str) and len(body["client_id"]) == 36,
            "invalid_input",
            400,
        )
        try:
            uuid.UUID(body["client_id"])
        except ValueError:
            raise Fault("invalid_input", 400) from None
        require(self.available(), "core_web_not_connected", 503)
        require(await self.p.local_work.run(self.model_configured), "model_not_configured", 503)
        header, entry, _, _ = await self.p.local_work.run(self.selection, body)
        semantic = digest({"body": body, "account": entry["account"]})
        message_id, prior = await self.p.local_work.run(
            self._prepare_submission, body, entry, semantic
        )
        if prior is not None:
            return prior
        data = {
            "message_key": {"channel": entry["channel"], "message_id": message_id, "revision": 1},
            "author": entry["account"],
            "sent_at": utc(self.p.origins.clock()),
            "kind": "message",
            "parts": [{"kind": "text", "text": body["text"]}],
            "reply_refs": [],
            "mentioned_accounts": [],
        }
        started = False
        try:
            origin = await self.p.local_work.run(
                self.p.sources.register_input, header, body["conversation"], data
            )
            ingest = {
                "schema_version": 1,
                "command": {
                    "schema_version": 1,
                    "request_id": "request:" + uuid.uuid4().hex,
                    "idempotency_key": "web:" + body["client_id"],
                    "origin": {"assertion_ref": origin["assertion_ref"]},
                    "deadline_at": utc(self.p.origins.clock() + 30),
                },
                "input": data,
                "target_actor_ids": [body["actor"]],
            }
            started = True
            result = await self.p.sources.dispatch(header, ingest)
            # Admission is not a reply, and its source/origin/identity are internal.
            result = {
                "routing_state": result["routing_state"],
                "outcomes": [
                    {"actor_id": o["actor_id"], "state": o["state"]} for o in result["outcomes"]
                ],
            }
            state = (
                "accepted"
                if any(o["state"] in {"accepted", "duplicate"} for o in result["outcomes"])
                else "not_started"
            )
        except CoreFault as exc:
            state = "not_started" if exc.document["execution_state"] == "not_started" else "unknown"
            result = exc.document
        except Fault as exc:
            state = "unknown" if started else "not_started"
            result = {"code": exc.code}
        except (OSError, sqlite3.Error, asyncio.CancelledError):
            # Initial durable unknown survives process/caller interruption. Never replay.
            raise
        await self.p.local_work.run(self._finish_submission, body, state, result)
        return {"message_id": message_id, "state": state, "result": result}

    async def cancel(self, body):
        require(
            set(body) == {"conversation", "actor", "turn_id", "expected_version"},
            "invalid_input",
            400,
        )
        require(
            type(body["expected_version"]) is int and body["expected_version"] > 0,
            "invalid_input",
            400,
        )
        snapshot = await self.snapshot(
            {"conversation": body["conversation"], "actor": body["actor"], "before": None}
        )
        data = snapshot["snapshot"]
        require(data is not None)
        turn = next(
            (
                t["turn"]
                for t in data["active_turns"] + data["history"]
                if t["turn"]["turn_id"] == body["turn_id"]
            ),
            None,
        )
        require(turn is not None)
        header, _, actor_entry, _ = await self.p.local_work.run(self.selection, body)
        origin = await self.p.local_work.run(self.p.origins.issue, header, actor_entry)
        request = {
            "command": {
                "schema_version": 1,
                "request_id": "request:" + uuid.uuid4().hex,
                "idempotency_key": "cancel:" + uuid.uuid4().hex,
                "origin": {"assertion_ref": origin["assertion_ref"]},
                "deadline_at": utc(self.p.origins.clock() + 30),
            },
            "turn_id": body["turn_id"],
            "expected_version": body["expected_version"],
            "reason": "explicit_user_cancel",
            "conversation_id": data["conversation_id"],
        }
        self.p.contracts.check("conversation#cancel_request", request)
        return await core_web_call(
            self.p.settings["core"],
            "cancel",
            request,
            self.p.contracts,
            "conversation#cancel_response",
        )

    def _check_origin(self, reference):
        with self.p.store.connect(write=True) as db:
            self.p.origins.context(db, reference, "platform", "companion", "dialogue")

    def _prepare_submission(self, body, entry, semantic):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT semantic,message_id,state,result FROM submissions WHERE id=?",
                (body["client_id"],),
            ).fetchone()
            if prior:
                require(prior[0] == semantic, "idempotency_conflict", 409)
                return None, {
                    "message_id": prior[1],
                    "state": prior[2],
                    "result": loads(prior[3]) if prior[3] else None,
                }
            message_id = "message:" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO submissions VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    body["client_id"],
                    semantic,
                    body["conversation"],
                    body["actor"],
                    canonical(entry["account"]),
                    message_id,
                    "unknown",
                ),
            )
            db.commit()
        return message_id, None

    def _finish_submission(self, body, state, result):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute(
                "UPDATE submissions SET state=?,result=? WHERE id=?",
                (state, canonical(result), body["client_id"]),
            )
            db.commit()
