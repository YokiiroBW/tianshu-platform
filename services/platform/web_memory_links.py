"""Two real accounts' consent for one exact Memory association.

The logged-in source account starts the challenge. The target must answer its
one-time sentence through the existing authenticated private input adapter.
The resulting proof binds accounts/scopes and the complete operation digest;
Memory alone owns the association and its revocation.
"""

import hashlib
import hmac
import ssl
import uuid

import aiohttp

from .auth import secret
from .contracts import Fault, canonical, digest, loads, require, utc
from .memory_proofs import operation_digest
from .origins import source_key

PREFIX = "/api/web/memory-links/"


class WebMemoryLinks:
    def __init__(self, platform, console):
        self.p, self.console = platform, console
        platform.sources.association_consent = self.observe

    def _target(self, db, actor, account):
        candidates = []
        for key in self.p.auth.entries:
            try:
                entry = self.p.auth.entry(db, key)
            except Fault:
                continue
            if (
                entry["account"] == account
                and entry["actor_id"] == actor
                and entry["audience"] == "self_private"
            ):
                self.p.auth.route(entry, "companion", "memory", "dialogue")
                scope = self.p.origins.scope(db, entry)
                if scope["person_id"] and scope["conversation_id"]:
                    candidates.append((key, entry, scope))
        require(len(candidates) == 1, "not_found", 404)
        return candidates[0]

    def _nonce(self, identifier):
        principal = self.p.auth.principals[self.console.config["principal"]]
        token = secret(principal["token_env"])
        require(token is not None, "unauthorized", 401)
        return hmac.new(token.encode(), identifier.encode(), hashlib.sha256).hexdigest()[:32]

    def begin(self, actor, target, client_id):
        p = self.p
        require(isinstance(client_id, str), "invalid_input", 400)
        try:
            require(str(uuid.UUID(client_id)) == client_id, "invalid_input", 400)
        except ValueError:
            raise Fault("invalid_input", 400) from None
        _, entry_id, scope = self.console.life_management._selection(actor)
        require(
            scope["person_id"] and scope["conversation_id"] and scope["audience"] == "self_private",
            "memory_identity_not_ready",
            503,
        )
        with p.store.connect(write=True) as db:
            entry = p.auth.entry(db, entry_id)
            target_id, other, target_scope = self._target(db, actor, target)
            require(
                entry["account"] != other["account"] and scope != target_scope, "invalid_input", 400
            )
            identifier = "account-challenge:" + digest([entry["account"], actor, client_id])[:32]
            nonce = self._nonce(identifier)
            old = db.execute(
                "SELECT * FROM memory_link_challenges WHERE request_id=?", (client_id,)
            ).fetchone()
            if old:
                require(
                    old["id"] == identifier
                    and old["source_entry"] == entry_id
                    and old["source_scope"] == canonical(scope)
                    and old["target_account"] == canonical(target)
                    and old["nonce_digest"] == digest(nonce),
                    "idempotency_conflict",
                    409,
                )
                expires = old["expires_at"]
            else:
                expires = p.origins.clock() + 600
                db.execute(
                    "INSERT INTO memory_link_challenges VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,NULL,'pending')",
                    (
                        identifier,
                        digest(nonce),
                        entry_id,
                        canonical(entry["account"]),
                        canonical(scope),
                        canonical(target),
                        target_id,
                        expires,
                        client_id,
                        "account-link:" + uuid.uuid4().hex,
                    ),
                )
            return {
                "challenge_id": identifier,
                "state": old["state"] if old else "pending",
                "expires_at": utc(expires),
                "consent_sentence": "天枢关联账号 " + identifier + " " + nonce,
            }

    def observe(self, db, source_entry_id, source_entry, physical):
        """Called only after Sources authenticated the immutable author and revision."""
        if physical["kind"] != "message" or source_entry["audience"] != "self_private":
            return
        text = "\n".join(
            part["text"] for part in physical["parts"] if part["kind"] == "text"
        ).strip()
        chunks = text.split(" ")
        if len(chunks) != 3 or chunks[0] != "天枢关联账号":
            return
        row = db.execute("SELECT * FROM memory_link_challenges WHERE id=?", (chunks[1],)).fetchone()
        if row is None or row["state"] != "pending" or row["expires_at"] <= self.p.origins.clock():
            return
        if row["target_account"] != canonical(physical["author"]) or not hmac.compare_digest(
            row["nonce_digest"], digest(chunks[2])
        ):
            return
        if row["target_entry"] not in source_entry["actor_entries"]:
            return
        entry = self.p.auth.entry(db, row["target_entry"])
        if (
            entry["account"] != physical["author"]
            or entry["channel"] != physical["message_key"]["channel"]
        ):
            return
        db.execute(
            "UPDATE memory_link_challenges SET consent_key=?,consent_revision=?,consent_digest=?,state='consented' WHERE id=?",
            (
                source_key(physical["message_key"]),
                physical["message_key"]["revision"],
                digest(physical),
                row["id"],
            ),
        )

    def owned(self, actor, identifier):
        _, entry_id, scope = self.console.life_management._selection(actor)
        with self.p.store.connect() as db:
            row = db.execute(
                "SELECT * FROM memory_link_challenges WHERE id=?", (identifier,)
            ).fetchone()
            require(
                row is not None
                and row["source_entry"] == entry_id
                and row["source_scope"] == canonical(scope),
                "not_found",
                404,
            )
            return dict(row)

    def proposal(self, actor, identifier, origin):
        row = self.owned(actor, identifier)
        p = self.p
        if row["association"]:
            return None, loads(row["association"])
        require(
            row["state"] == "consented" and row["expires_at"] > p.origins.clock(),
            "consent_required",
            409,
        )
        with p.store.connect(write=True) as db:
            source = p.auth.entry(db, row["source_entry"])
            target = p.auth.entry(db, row["target_entry"])
            require(
                canonical(target["account"]) == row["target_account"]
                and target["audience"] == "self_private",
                "scope_changed",
                409,
            )
            target_scope = p.origins.scope(db, target)
            require(
                target_scope["person_id"] and target_scope["conversation_id"],
                "memory_identity_not_ready",
                503,
            )
            current = db.execute(
                "SELECT * FROM input_observations WHERE physical_key=?", (row["consent_key"],)
            ).fetchone()
            require(
                current is not None
                and current["input_digest"] == row["consent_digest"]
                and current["revision"] == row["consent_revision"]
                and current["kind"] != "retract",
                "scope_changed",
                409,
            )
            scopes = [loads(row["source_scope"]), target_scope]
            request = {
                "command": {
                    "schema_version": 1,
                    "request_id": row["operation_id"],
                    "idempotency_key": row["operation_id"],
                    "origin": {"assertion_ref": origin["assertion_ref"]},
                    "deadline_at": utc(min(row["expires_at"], p.origins.clock() + 30)),
                },
                "action": "link",
                "source_account": source["account"],
                "target_account": target["account"],
                "association_id": None,
                "expected_version": 0,
                "scopes": scopes,
                "proof_ref": None,
            }
            semantic = operation_digest(request)
            key = "association:" + row["id"]
            proof = db.execute(
                "SELECT * FROM memory_operation_proofs WHERE command_key=?", (key,)
            ).fetchone()
            if proof:
                require(
                    proof["semantic"] == semantic
                    and not proof["revoked"]
                    and proof["expires_at"] > p.origins.clock(),
                    "scope_changed",
                    409,
                )
                ref = proof["ref"]
            else:
                ref = "memory-proof:" + uuid.uuid4().hex
                db.execute(
                    "INSERT INTO memory_operation_proofs VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
                    (
                        ref,
                        "association",
                        semantic,
                        source["owner"],
                        canonical([source["account"], target["account"]]),
                        canonical(scopes),
                        canonical([row["source_entry"], row["target_entry"]]),
                        row["consent_key"],
                        row["consent_revision"],
                        row["consent_digest"],
                        row["expires_at"],
                        key,
                    ),
                )
            request["proof_ref"] = ref
            p.contracts.check("memory-context#association_request", request)
            return request, None

    async def _call(self, payload):
        config = (self.p.settings.get("role_runtime") or {}).get("memory")
        require(config is not None, "dependency_unavailable", 503)
        token = secret(config["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        try:
            async with (
                aiohttp.ClientSession(
                    trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
                ) as client,
                client.post(
                    config["base_url"].rstrip("/") + "/internal/v1/memory/context/association",
                    json=payload,
                    headers={"Authorization": "Bearer " + token},
                    ssl=ssl.create_default_context(cafile=config.get("ca_file")),
                    allow_redirects=False,
                ) as response,
            ):
                raw = await response.content.read(65537)
                require(len(raw) <= 65536, "invalid_upstream", 502)
                result = loads(raw)
                if response.status != 200:
                    require(
                        result.get("code")
                        in {
                            "forbidden",
                            "not_found",
                            "version_conflict",
                            "scope_changed",
                            "idempotency_conflict",
                            "dependency_unavailable",
                            "timeout",
                        },
                        "invalid_upstream",
                        502,
                    )
                    raise Fault(result["code"], response.status)
                self.p.contracts.check("memory-context#association_response", result)
                require(
                    result["request_id"] == payload["command"]["request_id"],
                    "invalid_upstream",
                    502,
                )
                return result
        except (aiohttp.ClientError, TimeoutError, OSError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None

    def saved(self, identifier, result):
        with self.p.store.connect(write=True) as db:
            db.execute(
                "UPDATE memory_link_challenges SET association=?,state=? WHERE id=?",
                (canonical(result), result["state"], identifier),
            )

    async def route(self, name, body, session):
        require(
            isinstance(body, dict) and isinstance(body.get("actor_id"), str), "invalid_input", 400
        )
        actor = body["actor_id"]
        async with self.console.life_management.scoped_actor(
            actor, session, require_identity=True
        ) as (origin, _scope):
            if name == "begin":
                require(
                    set(body) == {"actor_id", "target_account", "client_id"}, "invalid_input", 400
                )
                self.p.contracts.check("common#account", body["target_account"])
                return await self.p.local_work.run(
                    self.begin, actor, body["target_account"], body["client_id"]
                )
            require(name in {"status", "complete", "revoke"}, "not_found", 404)
            expected = {"actor_id", "challenge_id"} | (
                {"expected_version", "client_id"} if name == "revoke" else set()
            )
            require(set(body) == expected, "invalid_input", 400)
            row = await self.p.local_work.run(self.owned, actor, body["challenge_id"])
            if name == "status":
                result = {
                    "challenge_id": row["id"],
                    "state": "expired"
                    if row["expires_at"] <= self.p.origins.clock() and not row["association"]
                    else row["state"],
                    "expires_at": utc(row["expires_at"]),
                    "association": loads(row["association"]) if row["association"] else None,
                }
                if result["state"] == "pending":
                    nonce = await self.p.local_work.run(self._nonce, row["id"])
                    require(digest(nonce) == row["nonce_digest"], "scope_changed", 409)
                    result["consent_sentence"] = "天枢关联账号 " + row["id"] + " " + nonce
                return result
            if name == "complete":
                payload, saved = await self.p.local_work.run(
                    self.proposal, actor, row["id"], origin
                )
                if saved:
                    return saved
            else:
                require(row["association"] is not None, "not_found", 404)
                saved = loads(row["association"])
                source = self.p.auth.entries[row["source_entry"]]
                payload = {
                    "command": {
                        "schema_version": 1,
                        "request_id": body["client_id"],
                        "idempotency_key": body["client_id"],
                        "origin": {"assertion_ref": origin["assertion_ref"]},
                        "deadline_at": utc(self.p.origins.clock() + 30),
                    },
                    "action": "revoke",
                    "source_account": source["account"],
                    "target_account": None,
                    "association_id": saved["association_id"],
                    "expected_version": body["expected_version"],
                    "scopes": [],
                    "proof_ref": None,
                }
                self.p.contracts.check("memory-context#association_request", payload)
            result = await self._call(payload)
            require(
                await self.p.local_work.run(self.console.session_valid, session),
                "session_expired",
                401,
            )
            await self.p.local_work.run(self.saved, row["id"], result)
            return result
