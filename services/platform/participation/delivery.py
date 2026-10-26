"""Atomic associations and eligibility for the original Bot delivery ledger."""

from contextlib import closing

from ..contracts import Fault, canonical, epoch, loads, require, utc
from . import current
from .protocol import candidate_ref, decision_ref, registration_ref


class Delivery:
    def __init__(self, application):
        self.app, self.p = application, application.p
        self.cursors = {}

    def confirmed(self, prepared, scope, anchor):
        response = prepared["response"]
        if response is None:
            return False
        outcomes = [
            item
            for item in response["outcomes"]
            if item["actor_id"] == scope["actor_id"] and item["state"] in {"accepted", "duplicate"}
        ]
        require(len(outcomes) == 1, "scope_changed", 409)
        admission = outcomes[0]["admission"]
        require(
            admission["scope"] == scope
            and admission["binding_version"] == anchor["binding_version"]
            and admission["accepted_origin"]["assertion_ref"] == prepared["actor_ref"]
            and admission["source"]["message_key"] == prepared["ingest"]["input"]["message_key"],
            "scope_changed",
            409,
        )
        return True

    def bind(self, authority, queue, ref, scope, reply_id):
        """Run after INSERT replies and before the same queue transaction commits."""
        if not self.app.enabled:
            return
        prepared = self.p.sources.participation_origin(authority, ref)
        if prepared is None:
            return
        registration = prepared["registration"]
        candidate_id = registration["candidate"]["candidate_id"]
        record = self.app.repository.get(candidate_id, db=queue)
        self.app.check(record)
        saved = self.app.repository.registration(candidate_id, db=queue)
        require(
            saved is not None and saved["document"] == registration and saved["host_registered"],
            "dependency_unavailable",
            503,
        )
        require(epoch(registration["expires_at"]) > self.p.origins.clock(), "scope_changed", 409)
        claim = record["claim_result"]
        require(
            claim is not None
            and claim["current_claim_state"] == "active"
            and self.p.origins.clock() - epoch(claim["checked_at"]) <= 5,
            "scope_changed",
            409,
        )
        candidate = record["document"]
        anchor = current.anchor(
            self.p, authority, prepared["entry_id"], prepared["entry"], candidate["native"]
        )
        require(
            anchor["stage"] == "resolved"
            and scope["actor_id"] == candidate["actor_id"]
            and scope["person_id"] == anchor["person_id"]
            and scope["conversation_id"] == anchor["conversation_id"],
            "forbidden",
            403,
        )
        original = registration["author_anchor"]
        require(
            anchor["entry_id"] == original["entry_id"]
            and (original["stage"] != "resolved" or anchor == original),
            "scope_changed",
            409,
        )
        reply = queue.execute("SELECT * FROM replies WHERE reply_id=?", (reply_id,)).fetchone()
        require(
            reply is not None
            and reply["connection_id"] == registration["admission"]["connection_id"]
            and loads(reply["scope"]) == scope,
            "forbidden",
            403,
        )
        confirmed = self.confirmed(prepared, scope, anchor)
        document = {
            "schema_version": 1,
            "kind": "participation_delivery_binding",
            "request_id": "binding:" + reply["attempt_id"],
            "candidate": candidate_ref(candidate),
            "decision": decision_ref(record["decision"]),
            "claim": registration["claim"],
            "source": candidate["source"],
            "author_anchor": anchor,
            "configuration": candidate["configuration"],
            "delivery": {
                key: reply[key] for key in ("connection_id", "turn_id", "reply_id", "attempt_id")
            },
            "bound_at": utc(self.p.origins.clock()),
            "expires_at": registration["expires_at"],
            "registration": registration_ref(registration),
            "source_confirmation": "confirmed" if confirmed else "pending",
        }
        self.app.protocol.response("delivery_binding", document)
        old = queue.execute(
            "SELECT * FROM participation_bindings WHERE reply_id=?", (reply_id,)
        ).fetchone()
        if old:
            previous = loads(old["document"])
            require(
                old["attempt_id"] == reply["attempt_id"]
                and old["registration_id"] == registration["registration_id"]
                and all(
                    previous[key] == document[key]
                    for key in document
                    if key not in {"bound_at", "source_confirmation"}
                ),
                "idempotency_conflict",
                409,
            )
            return
        queue.execute(
            "INSERT INTO participation_bindings VALUES(?,?,?,?)",
            (reply_id, reply["attempt_id"], registration["registration_id"], canonical(document)),
        )

    async def prepare_creation(self, header, ref):
        if not self.app.enabled:
            return
        self.app.guard(header)
        with self.p.store.connect() as db:
            prepared = self.p.sources.participation_origin(db, ref)
        if prepared is None:
            return
        record = await self.p.local_work.run(
            self.app.repository.get, prepared["registration"]["candidate"]["candidate_id"]
        )
        await self.app.refresh_claim(record, active=True)

    def _check(self, reply_id, attempt_id):
        with self.p.store.connect() as authority, closing(self.p.bots._db()) as queue:
            row = queue.execute(
                "SELECT * FROM participation_bindings WHERE reply_id=?", (reply_id,)
            ).fetchone()
            if row is None:
                return None
            require(row["attempt_id"] == attempt_id, "idempotency_conflict", 409)
            binding = loads(row["document"])
            record = self.app.repository.get(binding["candidate"]["candidate_id"], db=queue)
            live = self.app.check(record)
            registration = self.app.repository.registration(record["id"], db=queue)
            require(
                registration is not None
                and registration_ref(registration["document"]) == binding["registration"],
                "dependency_unavailable",
                503,
            )
            prepared = self.p.sources.participation_origin(authority, registration["actor_ref"])
            require(
                prepared is not None and prepared["registration"] == registration["document"],
                "dependency_unavailable",
                503,
            )
            scope = self.p.origins.scope(authority, prepared["entry"])
            anchor = current.anchor(
                self.p,
                authority,
                prepared["entry_id"],
                prepared["entry"],
                record["document"]["native"],
            )
            require(
                anchor == binding["author_anchor"]
                and epoch(binding["expires_at"]) > self.p.origins.clock(),
                "scope_changed",
                409,
            )
            confirmed = self.confirmed(prepared, scope, anchor)
        if confirmed and binding["source_confirmation"] == "pending":
            binding["source_confirmation"] = "confirmed"
            # Only this monotonic field changes; the original association and bound_at stay fixed.
            with closing(self.p.bots._db()) as queue, queue:
                queue.execute(
                    "UPDATE participation_bindings SET document=? WHERE reply_id=? AND attempt_id=?",
                    (canonical(binding), reply_id, attempt_id),
                )
        return record, live, binding

    async def prepare(self, header, body):
        """Authenticate before network reads; return this claim's checked associations."""
        if not self.app.enabled:
            return {}
        with closing(self.p.bots._db()) as db:
            self.p.bots._authenticate(db, header, body["connection_id"])
            connection = body["connection_id"]
            cursor = self.cursors.get(connection, (0, ""))
            # Rotate pending associations so expired/pending origins cannot pin the
            # scan forever. Existing claimed batches are always checked separately.
            claimed = db.execute(
                "SELECT r.reply_id,r.attempt_id FROM replies r JOIN participation_bindings b ON b.reply_id=r.reply_id "
                "WHERE r.connection_id=? AND r.state='claimed' ORDER BY r.observed_at,r.reply_id LIMIT 20",
                (connection,),
            ).fetchall()
            pending = db.execute(
                "SELECT r.reply_id,r.attempt_id,r.observed_at FROM replies r JOIN participation_bindings b ON b.reply_id=r.reply_id "
                "WHERE r.connection_id=? AND r.state='pending' AND (r.observed_at>? OR (r.observed_at=? AND r.reply_id>?)) "
                "ORDER BY r.observed_at,r.reply_id LIMIT 20",
                (connection, cursor[0], cursor[0], cursor[1]),
            ).fetchall()
            self.cursors[connection] = (
                (pending[-1]["observed_at"], pending[-1]["reply_id"])
                if len(pending) == 20
                else (0, "")
            )
            rows = [*claimed, *pending]
        checked = {}
        for row in rows:
            proof = {
                "attempt_id": row["attempt_id"],
                "checked_at": self.p.origins.clock(),
                "active": False,
            }
            try:
                evidence = await self.p.local_work.run(
                    self._check, row["reply_id"], row["attempt_id"]
                )
                record, live, binding = evidence
                claim = await self.app.refresh_claim(record)
                if (
                    claim["current_claim_state"] != "active"
                    or epoch(claim["claim"]["expires_at"]) <= self.p.origins.clock()
                ):
                    checked[row["reply_id"]] = proof
                    continue
                response = await self.app.rpc(live, "bind", binding)
                require(response.get("binding") == binding, "adapter_incompatible", 502)
                if binding["source_confirmation"] != "confirmed":
                    continue
                await self.p.local_work.run(self._check, row["reply_id"], row["attempt_id"])
                checked[row["reply_id"]] = {
                    "attempt_id": row["attempt_id"],
                    "candidate_id": record["id"],
                    "checked_at": self.p.origins.clock(),
                    "active": True,
                }
            except Fault as error:
                # A transport/unavailable result is not proof that an in-flight
                # batch became invalid. Unvisited and uncertain claims still block.
                if error.status in {403, 404, 408, 409}:
                    checked[row["reply_id"]] = proof
            except OSError:
                continue
        return checked

    def eligible(self, db, connection_id, checked):
        """Exclude participation before ordering/limits and before blocking a connection."""
        valid = []
        for reply_id, proof in (checked or {}).items():
            if not proof["active"] or self.p.origins.clock() - proof["checked_at"] > 5:
                continue
            try:
                record = self.app.repository.get(proof["candidate_id"], db=db)
                self.app.check(record)
            except Fault:
                continue
            row = db.execute(
                "SELECT b.document,r.connection_id FROM participation_bindings b JOIN replies r ON r.reply_id=b.reply_id "
                "WHERE b.reply_id=? AND b.attempt_id=?",
                (reply_id, proof["attempt_id"]),
            ).fetchone()
            if (
                row
                and row["connection_id"] == connection_id
                and loads(row["document"])["source_confirmation"] == "confirmed"
            ):
                valid.append(reply_id)
        return valid

    def inactive_claimed(self, db, connection_id, checked):
        invalid = []
        for reply_id, proof in (checked or {}).items():
            if proof["active"] or self.p.origins.clock() - proof["checked_at"] > 5:
                continue
            row = db.execute(
                "SELECT 1 FROM replies r JOIN participation_bindings b ON b.reply_id=r.reply_id "
                "WHERE r.reply_id=? AND r.attempt_id=? AND r.connection_id=? AND r.state='claimed'",
                (reply_id, proof["attempt_id"], connection_id),
            ).fetchone()
            if row:
                invalid.append(reply_id)
        return invalid

    async def before_send(self, reply_id, attempt_id):
        if not self.app.enabled:
            return
        evidence = await self.p.local_work.run(self._check, reply_id, attempt_id)
        if evidence is None:
            return
        record, row, binding = evidence
        require(binding["source_confirmation"] == "confirmed", "scope_changed", 409)
        await self.app.refresh_claim(record, active=True)
        answer = await self.app.rpc(row, "bind", binding)
        require(answer.get("binding") == binding, "adapter_incompatible", 502)
        await self.p.local_work.run(self._check, reply_id, attempt_id)
