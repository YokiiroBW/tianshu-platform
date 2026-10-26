"""Join semantic intent to the original Bot event and authenticated source preparation."""

import uuid

from ..contracts import Fault, canonical, digest, epoch, require, utc
from . import current
from .protocol import candidate_ref, decision_ref


def event_key(body):
    return canonical([body["connection_id"], body["event_id"], body["account_id"]])


class Admission:
    def __init__(self, application):
        self.app, self.p = application, application.p

    def reserve(self, db, body, semantic, candidate_id, old, actors):
        """Called in _admit_event's queue transaction, before its new event is visible."""
        key = event_key(body)
        marker = db.execute(
            "SELECT candidate_id FROM participation_events WHERE event_key=?", (key,)
        ).fetchone()
        if candidate_id is None:
            require(marker is None, "idempotency_conflict", 409)
            return actors
        record = self.app.repository.get(candidate_id, db=db)
        self.app.check(record)
        candidate, event, claim = record["document"], record["event"], record["claim_result"]
        require(
            record["decision"] is not None
            and record["decision"]["outcome"] == "engage"
            and claim is not None
            and claim["current_claim_state"] == "active"
            and epoch(claim["claim"]["expires_at"]) > self.p.origins.clock(),
            "scope_changed",
            409,
        )
        require(
            candidate["actor_id"] in actors
            and body == self.p.bot_observation.reply_body(body["connection_id"], event)
            and semantic == digest(body),
            "forbidden",
            403,
        )
        if marker:
            require(marker["candidate_id"] == candidate_id, "idempotency_conflict", 409)
        else:
            require(old is None, "idempotency_conflict", 409)
            db.execute("INSERT INTO participation_events VALUES(?,?)", (key, candidate_id))
        return [candidate["actor_id"]]

    async def dispatch(self, candidate_id, body, admission, data):
        app, p = self.app, self.p
        record = await p.local_work.run(app.repository.get, candidate_id)
        await app.refresh_claim(record, active=True)
        record = await p.local_work.run(app.repository.get, candidate_id)
        candidate, decision, claim = (
            record["document"],
            record["decision"],
            record["claim_result"]["claim"],
        )
        header, key = p.bots._platform_header(), event_key(body)
        started = False
        try:
            prepared = await p.local_work.run(
                p.sources.participation_prepared, header, key, candidate_ref(candidate)
            )
            if prepared is None:
                source = await p.local_work.run(
                    p.sources.register_input, header, admission["entry_id"], data
                )
                deadline = min(
                    p.origins.clock() + 30,
                    epoch(claim["expires_at"]),
                    epoch(decision["expires_at"]),
                )
                ingest = {
                    "schema_version": 1,
                    "command": {
                        "schema_version": 1,
                        "request_id": "request:" + uuid.uuid4().hex,
                        "idempotency_key": "bot:"
                        + digest([body["connection_id"], body["event_id"], body["account_id"]]),
                        "origin": {"assertion_ref": source["assertion_ref"]},
                        "deadline_at": utc(deadline),
                    },
                    "input": data,
                    "target_actor_ids": [candidate["actor_id"]],
                }
                await p.local_work.run(
                    lambda: p.sources.prepare_mapping(
                        header, ingest, participation=(key, candidate_ref(candidate))
                    )
                )
                prepared = await p.local_work.run(
                    p.sources.participation_prepared, header, key, candidate_ref(candidate)
                )
            require(
                prepared["ingest"]["input"] == data
                and prepared["ingest"]["target_actor_ids"] == [candidate["actor_id"]],
                "idempotency_conflict",
                409,
            )
            document = prepared["registration"]
            if document is None:
                with p.store.connect() as db:
                    anchor = current.anchor(
                        p, db, prepared["entry_id"], prepared["entry"], candidate["native"]
                    )
                document = {
                    "schema_version": 1,
                    "kind": "participation_admission_registration",
                    "registration_id": "registration:" + digest([key, candidate_ref(candidate)]),
                    "candidate": candidate_ref(candidate),
                    "decision": decision_ref(decision),
                    "claim": claim,
                    "native": candidate["native"],
                    "source": candidate["source"],
                    "author_anchor": anchor,
                    "configuration": candidate["configuration"],
                    "admission": {
                        "connection_id": body["connection_id"],
                        "event_id": body["event_id"],
                        "account_id": body["account_id"],
                        "event_semantic_digest": digest(body),
                        "message_id": admission["message_id"],
                        "revision": 1,
                        "input_entry_id": admission["entry_id"],
                        "input_digest": digest(data),
                        "source_input_assertion_ref": prepared["ingest"]["command"]["origin"][
                            "assertion_ref"
                        ],
                        "actor_assertion_ref": prepared["actor_ref"],
                        "fanout_ticket": prepared["ticket"],
                        "core_idempotency_key": prepared["ingest"]["command"]["idempotency_key"],
                    },
                    "registered_at": utc(p.origins.clock()),
                    "expires_at": utc(
                        min(epoch(claim["expires_at"]), prepared["source"]["expires_at"])
                    ),
                }
                app.protocol.response("admission_registration", document)
                await p.local_work.run(p.sources.register_participation, header, key, document)
            app.protocol.check("admission_registration", document)
            registration = await p.local_work.run(
                app.repository.register, candidate_id, document, prepared["ingest"]
            )
            record = await p.local_work.run(app.repository.get, candidate_id)
            row = await p.local_work.run(app.check, record)
            response = await app.rpc(row, "register", document)
            require(response.get("registration") == document, "adapter_incompatible", 502)
            await p.local_work.run(app.repository.host_registered, candidate_id)
            await app.refresh_claim(record, active=True)
            # Both local owners and the host have the same immutable registration now.
            saved = await p.local_work.run(app.repository.registration, candidate_id)
            require(
                saved["document"] == registration["document"] and saved["host_registered"],
                "dependency_unavailable",
                503,
            )
            started = True
            return await p.sources.dispatch_participation(header, key, document)
        except Fault as error:
            if not started:
                error.source_not_started = True
            raise
