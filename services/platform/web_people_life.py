"""Existing QQ control authority's content-free life/delivery projection.

Private life bodies continue through ordinary scope-bound readers. An administrator
can configure a registered destination and see delivery states, never borrow its origin.
"""

import uuid
from contextlib import closing

from .contracts import Fault, canonical, loads, require

PREFIX = "/api/web/people-life/"


class WebPeopleLife:
    def __init__(self, platform, console):
        self.p, self.console = platform, console

    def target(self, actor, qq_id, conversation):
        require(
            isinstance(qq_id, str) and qq_id.isdigit() and isinstance(conversation, str),
            "invalid_input",
            400,
        )
        account = {"namespace": "qq", "immutable_account_id": qq_id}
        candidates = []
        with self.p.store.connect() as db:
            for key in self.p.auth.entries:
                try:
                    entry = self.p.auth.entry(db, key)
                except Fault as error:
                    if error.status != 403:
                        raise
                    continue
                if (
                    entry["account"] != account
                    or entry["actor_id"] != actor
                    or entry["channel"]["channel_conversation_id"] != conversation
                ):
                    continue
                self.p.auth.route(entry, "companion", "platform", "dialogue")
                scope = self.p.origins.scope(db, entry)
                if scope["person_id"] and scope["conversation_id"]:
                    candidates.append((key, entry, scope))
        require(len(candidates) == 1, "not_found", 404)
        return candidates[0]

    def delivery(self, scope, channel):
        if not hasattr(self.p.bots, "delivery"):
            return {"available": False, "items": []}
        items = []
        with closing(self.p.bots._db()) as db:
            for row in db.execute(
                "SELECT * FROM expressions WHERE scope=? AND channel=? ORDER BY created_at DESC LIMIT 50",
                (canonical(scope), canonical(channel)),
            ):
                receipt = self.p.bots.delivery._receipt(db, row, "people-delivery-view")
                items.append(
                    {
                        "expression_id": row["id"],
                        "kind": loads(row["origin"])["kind"],
                        "state": receipt["state"],
                        "final": receipt["final"],
                        "created_at": row["created_at"],
                        "sent_segments": sum(
                            item["state"] == "sent" for item in receipt["segments"]
                        ),
                        "total_segments": len(receipt["segments"]),
                    }
                )
        return {"available": True, "items": items}

    async def route(self, name, body, session):
        require(
            name in {"view", "subscription", "state"} and isinstance(body, dict), "not_found", 404
        )
        action = "qq.admin.view" if name == "view" else "qq.admin.manage"
        await self.p.local_work.run(self.p.qq_admin.check_access, self.console, session, action)
        selection = {"actor_id", "qq_id", "conversation"}
        require(
            set(body)
            == selection
            | (set() if name == "view" else {"value", "expected_version", "client_id"}),
            "invalid_input",
            400,
        )
        _, entry, scope = await self.p.local_work.run(
            self.target, body["actor_id"], body["qq_id"], body["conversation"]
        )
        payload = {
            "schema_version": 2,
            "request_id": "people-control:" + uuid.uuid4().hex,
            "actor_id": body["actor_id"],
            "person_id": scope["person_id"],
            "channel": entry["channel"],
        }
        self.p.contracts.check("life-runtime#control_read_request", payload)
        control = await self.console.life_management._call(
            "proactive/control", payload, management=True
        )
        if name == "view":
            result = {
                "control": control,
                "delivery": await self.p.local_work.run(self.delivery, scope, entry["channel"]),
                "private_life": {"available": False, "code": "scope_required"},
            }
        else:
            require(isinstance(body["value"], dict), "invalid_input", 400)
            value = dict(body["value"])
            if name == "subscription":
                value.update(
                    person_id=scope["person_id"],
                    audience=scope["audience"],
                    conversation_id=scope["conversation_id"],
                    channel=entry["channel"],
                    consent_basis="explicit_admin_registration",
                    consent_ref="platform-admin:"
                    + self.console.config["principal"]
                    + ":"
                    + body["client_id"],
                )
                operation = "proactive.subscription"
            else:
                require(
                    any(row["id"] == value.get("id") for row in control["subscriptions"]),
                    "not_found",
                    404,
                )
                operation = "proactive.subscription.state"
            request = {
                "schema_version": 2,
                "request_id": body["client_id"],
                "actor_id": body["actor_id"],
                "operation": operation,
                "expected_version": body["expected_version"],
                "value": value,
            }
            self.p.contracts.check("life-runtime#manage_request", request)
            result = await self.console.life_management._call("manage", request, management=True)
        await self.p.local_work.run(self.p.qq_admin.check_access, self.console, session, action)
        _, current, current_scope = await self.p.local_work.run(
            self.target, body["actor_id"], body["qq_id"], body["conversation"]
        )
        require(current == entry and current_scope == scope, "scope_changed", 409)
        return result
