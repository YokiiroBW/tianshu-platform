"""Platform-owned input and permission facts, with authenticated atomic Core backfill.

Application ports accept deployment-authenticated adapters, never model-produced proofs.
The published rules add relationship checks after authentication and database lookups.
"""

import copy
import uuid

from .auth import secret
from .contracts import Fault, canonical, digest, epoch, loads, require, utc
from .origins import source_key


class Sources:
    def __init__(self, store, auth, contracts, origins, settings, clock):
        self.store, self.auth, self.contracts = store, auth, contracts
        self.origins, self.clock = origins, clock
        self.entries = copy.deepcopy(settings.get("input_entries", {}))
        self.core = copy.deepcopy(settings.get("core"))
        if self.core is not None:
            from .transport import core_settings

            core_settings(self.core)
        for key, entry in self.entries.items():
            contracts.check("common#id", key)
            require(
                set(entry)
                == {
                    "owner",
                    "account",
                    "channel",
                    "audience",
                    "ttl_seconds",
                    "actor_entries",
                    "default_actor_ids",
                    "routing_version",
                },
                "invalid_input",
                400,
            )
            require(
                key not in auth.entries and entry["owner"] in auth.principals, "invalid_input", 400
            )
            owner = auth.principals[entry["owner"]]
            require(owner["service"] in {"platform", "nonebot"}, "invalid_input", 400)
            contracts.check("common#account", entry["account"])
            contracts.check("common#channel_key", entry["channel"])
            require(
                entry["account"]["namespace"] == entry["channel"]["namespace"], "invalid_input", 400
            )
            if owner["kind"] == "operator":
                require(owner["account"] == entry["account"], "invalid_input", 400)
            require(entry["audience"] in {"group", "self_private"}, "invalid_input", 400)
            require(
                type(entry["ttl_seconds"]) is int and 1 <= entry["ttl_seconds"] <= 3600,
                "invalid_input",
                400,
            )
            require(
                type(entry["routing_version"]) is int and entry["routing_version"] > 0,
                "invalid_input",
                400,
            )
            for name in ("actor_entries", "default_actor_ids"):
                values = entry[name]
                require(
                    isinstance(values, list)
                    and len(values) <= 32
                    and len(set(values)) == len(values),
                    "invalid_input",
                    400,
                )
                for value in values:
                    contracts.check("common#id", value)
            actors = []
            for actor_entry in entry["actor_entries"]:
                actor = auth.entries.get(actor_entry)
                require(actor is not None, "invalid_input", 400)
                require(
                    all(actor[k] == entry[k] for k in ("owner", "account", "channel", "audience")),
                    "invalid_input",
                    400,
                )
                actors.append(actor["actor_id"])
            require(len(set(actors)) == len(actors), "invalid_input", 400)
            # Defaults express intent and may include currently unauthorized actors.

    def _entry(self, db, entry_id, expected_digest=None):
        entry = self.entries.get(entry_id)
        require(entry is not None)
        require(expected_digest is None or expected_digest == digest(entry))
        require(not db.execute("SELECT 1 FROM revoked_entries WHERE id=?", (entry_id,)).fetchone())
        require(
            not db.execute(
                "SELECT 1 FROM revoked_principals WHERE id=?", (entry["owner"],)
            ).fetchone()
        )
        require(secret(self.auth.principals[entry["owner"]]["token_env"]) is not None)
        return entry

    def register_input(self, header, entry_id, data):
        """Trusted application adapter has already verified the external submission.

        Its bearer principal and immutable account/channel registration are checked here.
        This is not a public HTTP endpoint or an external QQ/login implementation.
        """
        self.contracts.check("sources#physical_input", data)
        with self.store.connect(write=True) as db:
            identity, caller = self.auth.authenticate(header, db, "source.register")
            entry = self._entry(db, entry_id)
            require(entry["owner"] == identity)
            require(
                data["author"] == entry["account"]
                and data["message_key"]["channel"] == entry["channel"]
            )
            key, revision, content = (
                source_key(data["message_key"]),
                data["message_key"]["revision"],
                digest(data),
            )
            old = db.execute(
                "SELECT * FROM input_observations WHERE physical_key=?", (key,)
            ).fetchone()
            if old:
                require(old["author"] == canonical(data["author"]))
                require(revision >= old["revision"], "version_conflict", 409)
                if revision == old["revision"]:
                    require(content == old["input_digest"], "idempotency_conflict", 409)
                else:
                    require(old["kind"] != "retract", "scope_changed", 409)
                    require(data["kind"] != "message", "version_conflict", 409)
            else:
                require(data["kind"] == "message", "dependency_unavailable", 503)
            if old is None or revision > old["revision"]:
                db.execute(
                    "INSERT OR REPLACE INTO input_observations VALUES(?,?,?,?,?)",
                    (key, canonical(data["author"]), revision, content, data["kind"]),
                )
            ref = "source-input:" + uuid.uuid4().hex
            expires = self.clock() + entry["ttl_seconds"]
            db.execute(
                "INSERT INTO source_inputs VALUES(?,?,?,?,?,?,?,0)",
                (
                    ref,
                    entry_id,
                    digest(entry),
                    caller["service"],
                    content,
                    canonical({k: data[k] for k in ("author", "message_key", "kind")}),
                    expires,
                ),
            )
            return {
                "assertion_ref": ref,
                "expires_at": utc(expires),
                "proof": "source_input",
                "external_identity": "adapter_owned",
            }

    def _input(self, db, ingest):
        row = db.execute(
            "SELECT * FROM source_inputs WHERE ref=?",
            (ingest["command"]["origin"]["assertion_ref"],),
        ).fetchone()
        require(row is not None and not row["revoked"] and row["expires_at"] > self.clock())
        require(epoch(ingest["command"]["deadline_at"]) > self.clock(), "timeout", 408)
        entry = self._entry(db, row["entry_id"], row["entry_digest"])
        data = ingest["input"]
        require(
            row["input_digest"] == digest(data)
            and row["input_metadata"]
            == canonical({k: data[k] for k in ("author", "message_key", "kind")})
            and data["author"] == entry["account"]
            and data["message_key"]["channel"] == entry["channel"]
        )
        current = db.execute(
            "SELECT * FROM input_observations WHERE physical_key=?",
            (source_key(data["message_key"]),),
        ).fetchone()
        require(
            current is not None and current["input_digest"] == row["input_digest"],
            "scope_changed",
            409,
        )
        return row, entry

    def _route(self, db, ingest, source, entry):
        command_key = canonical(
            [source["ingress"], "core.ingest_actors", ingest["command"]["idempotency_key"]]
        )
        semantic = digest(
            {"input": ingest["input"], "target_actor_ids": ingest["target_actor_ids"]}
        )
        row = db.execute(
            "SELECT * FROM fanout_routes WHERE command_key=?", (command_key,)
        ).fetchone()
        if row:
            require(row["semantic_digest"] == semantic, "idempotency_conflict", 409)
        else:
            effective = (
                []
                if ingest["input"]["kind"] == "retract"
                else sorted(ingest["target_actor_ids"] or entry["default_actor_ids"])
            )
            db.execute(
                "INSERT INTO fanout_routes VALUES(?,?,?,?,?)",
                (
                    command_key,
                    semantic,
                    canonical(effective),
                    canonical(sorted(entry["default_actor_ids"])),
                    entry["routing_version"],
                ),
            )
            row = db.execute(
                "SELECT * FROM fanout_routes WHERE command_key=?", (command_key,)
            ).fetchone()
        return row

    def _authority(self, db, request):
        ingest = request["ingest"]
        source, entry = self._input(db, ingest)
        route = self._route(db, ingest, source, entry)
        contexts = []
        effective = loads(route["effective"])
        for entry_id in entry["actor_entries"]:
            candidate = self.auth.entries[entry_id]
            if candidate["actor_id"] not in effective:
                continue
            try:
                actor_entry = self.auth.entry(db, entry_id)
                self.auth.route(actor_entry, source["ingress"], "companion", "dialogue")
                self.auth.route(actor_entry, "companion", "memory", "dialogue")
            except Fault as exc:
                if exc.status == 403:
                    continue
                raise
            historic = db.execute(
                "SELECT o.ref FROM actor_origins a JOIN origins o ON a.ref=o.ref WHERE a.input_digest=? AND a.entry_document=? AND a.ingress=? AND a.input_entry_id=? AND o.revoked=0 AND o.expires_at>? ORDER BY a.issued_at DESC LIMIT 1",
                (
                    source["input_digest"],
                    canonical(actor_entry),
                    source["ingress"],
                    source["entry_id"],
                    self.clock(),
                ),
            ).fetchone()
            if historic:
                ref = historic[0]
            else:
                ref = "origin:" + uuid.uuid4().hex
                expires = min(
                    source["expires_at"],
                    self.clock() + actor_entry["ttl_seconds"],
                    epoch(actor_entry["expires_at"])
                    if "expires_at" in actor_entry
                    else float("inf"),
                )
                db.execute(
                    "INSERT INTO origins(ref,entry_id,entry_digest,expires_at) VALUES(?,?,?,?)",
                    (ref, entry_id, digest(actor_entry), expires),
                )
                db.execute(
                    "INSERT INTO actor_origins VALUES(?,?,?,?,?,?)",
                    (
                        ref,
                        source["input_digest"],
                        canonical(actor_entry),
                        epoch(utc(self.clock())),
                        source["ingress"],
                        source["entry_id"],
                    ),
                )
            _, _, context = self.origins.context(
                db, ref, source["ingress"], "companion", "dialogue"
            )
            contexts.append(context)
        result = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "operation": "input",
            "request_digest": digest(request),
            "ingest_digest": digest(ingest),
            "verified_account": entry["account"],
            "verified_channel": entry["channel"],
            "input_digest": source["input_digest"],
            "origin_ref": source["ref"],
            "expires_at": utc(source["expires_at"]),
            "default_actor_ids": loads(route["defaults"]),
            "routing_version": route["routing_version"],
            "actor_contexts": contexts,
            "audience": entry["audience"],
        }
        self.contracts.check("sources#input_authority", result)
        return result, route

    def read(self, header, request):
        operation = request.get("operation")
        require(operation in {"input", "current"}, "invalid_input", 400)
        self.contracts.check(
            f"sources#{'input' if operation == 'input' else 'current'}_access_request", request
        )
        with self.store.connect(write=True) as db:
            _, principal = self.auth.authenticate(header, db, "source." + operation)
            require(
                principal["kind"] == "service"
                and principal["service"] == ("companion" if operation == "input" else "memory")
            )
            if operation == "input":
                return self._authority(db, request)[0]
            return self._current(db, request)

    def prepare_mapping(self, header, ingest):
        self.contracts.check("sources#fanout_request", ingest)
        with self.store.connect(write=True) as db:
            preparer, principal = self.auth.authenticate(header, db, "mapping.prepare")
            source, entry = self._input(db, ingest)
            require(entry["owner"] == preparer and source["ingress"] == principal["service"])
            access = {
                "schema_version": 1,
                "request_id": "access:" + uuid.uuid4().hex,
                "operation": "input",
                "ingest": ingest,
            }
            authority, route = self._authority(db, access)
            ticket = "fanout:" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO fanout_exchanges VALUES(?,?,?,?,?,?,NULL)",
                (
                    ticket,
                    route["command_key"],
                    preparer,
                    canonical(ingest),
                    canonical(access),
                    canonical(authority),
                ),
            )
            return ticket

    def confirm_mapping(self, header, ticket, response):
        """Trusted Core application response port, not a new mapping HTTP RPC."""
        with self.store.connect(write=True) as db:
            _, principal = self.auth.authenticate(header, db, "mapping.confirm")
            require(principal["kind"] == "service" and principal["service"] == "companion")
            self._confirm(db, ticket, response)

    def _confirm(self, db, ticket, response):
        self.contracts.check("sources#fanout_response", response)
        exchange = db.execute("SELECT * FROM fanout_exchanges WHERE ticket=?", (ticket,)).fetchone()
        require(exchange is not None, "not_found", 404)
        require(
            not db.execute(
                "SELECT 1 FROM revoked_principals WHERE id=?", (exchange["prepared_by"],)
            ).fetchone()
        )
        ingest, access = loads(exchange["request"]), loads(exchange["access_request"])
        authority = loads(exchange["authority"])
        source, entry = self._input(db, ingest)
        route = self._route(db, ingest, source, entry)
        accepted = [o for o in response["outcomes"] if o["state"] != "forbidden"]
        identity = (
            None
            if not accepted
            else {
                "person_id": response["person_id"],
                "binding_version": accepted[0]["admission"]["binding_version"],
            }
        )
        frozen = {
            "semantic_digest": route["semantic_digest"],
            "effective_actor_ids": loads(route["effective"]),
            "routing_version": route["routing_version"],
        }
        try:
            self.contracts.rules.fanout_mapping(
                ingest,
                response,
                identity,
                access,
                authority,
                utc(self.clock()),
                frozen,
                source["ingress"],
            )
        except self.contracts.rules.Violation:
            raise Fault("forbidden", 403) from None
        stable = canonical(response)
        if exchange["response"] is not None:
            require(exchange["response"] == stable, "idempotency_conflict", 409)
        histories = []
        for outcome in accepted:
            admission = outcome["admission"]
            origin = db.execute(
                "SELECT o.*, a.input_digest, a.entry_document, a.issued_at, a.ingress, a.input_entry_id FROM origins o JOIN actor_origins a ON o.ref=a.ref WHERE o.ref=?",
                (admission["accepted_origin"]["assertion_ref"],),
            ).fetchone()
            require(origin is not None)
            historic = loads(origin["entry_document"])
            require(
                origin["input_digest"] == digest(ingest["input"])
                and origin["ingress"] == source["ingress"]
                and origin["input_entry_id"] == source["entry_id"]
            )
            require(
                origin["issued_at"] <= epoch(admission["accepted_at"]) < origin["expires_at"]
                and epoch(admission["accepted_at"]) <= epoch(utc(self.clock()))
            )
            require(
                historic["actor_id"] == outcome["actor_id"]
                and historic["account"] == ingest["input"]["author"]
                and historic["channel"] == ingest["input"]["message_key"]["channel"]
            )
            require(origin["entry_id"] in entry["actor_entries"])
            current = self.auth.entry(db, origin["entry_id"])
            require(
                all(
                    current[k] == historic[k]
                    for k in ("owner", "account", "channel", "actor_id", "audience")
                )
            )
            self.auth.route(current, source["ingress"], "companion", "dialogue")
            self.auth.route(current, "companion", "memory", "dialogue")
            receipt_id = admission["source"]["receipt_id"]
            prior = db.execute(
                "SELECT admission FROM admission_history WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            require(prior is None or prior[0] == canonical(admission), "idempotency_conflict", 409)
            previous_actor = db.execute(
                "SELECT admission FROM admission_history WHERE selector_key=? AND revision=?",
                (canonical(admission["selector"]), admission["source"]["message_key"]["revision"]),
            ).fetchone()
            require(
                previous_actor is None or previous_actor[0] == canonical(admission),
                "idempotency_conflict",
                409,
            )
            histories.append(
                (
                    receipt_id,
                    canonical(admission),
                    origin["entry_id"],
                    origin["entry_document"],
                    canonical(ingest["input"]["author"]),
                    canonical(admission["selector"]),
                    admission["source"]["message_key"]["revision"],
                )
            )
        # Core is the authenticated owner of these atomic identity/admission facts.
        # No mapping is derived from a client identity dictionary or source observation.
        account = canonical(ingest["input"]["author"])
        if identity:
            for table in ("identities", "ingest_subjects"):
                old = db.execute(f"SELECT * FROM {table} WHERE account=?", (account,)).fetchone()
                require(
                    old is None or old["person_id"] == identity["person_id"],
                    "version_conflict",
                    409,
                )
                if table == "identities":
                    require(
                        old is None or old["version"] == identity["binding_version"],
                        "version_conflict",
                        409,
                    )
            db.execute(
                "INSERT OR IGNORE INTO identities VALUES(?,?,?)",
                (account, identity["person_id"], identity["binding_version"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO ingest_subjects VALUES(?,?)",
                (account, identity["person_id"]),
            )
        else:
            require(response["person_id"] is None)
        channel = canonical(ingest["input"]["message_key"]["channel"])
        old = db.execute(
            "SELECT conversation_id FROM channels WHERE channel=?", (channel,)
        ).fetchone()
        require(old is None or old[0] == response["conversation_id"], "version_conflict", 409)
        db.execute(
            "INSERT OR IGNORE INTO channels VALUES(?,?)", (channel, response["conversation_id"])
        )
        db.executemany("INSERT OR IGNORE INTO admission_history VALUES(?,?,?,?,?,?,?)", histories)
        db.execute("UPDATE fanout_exchanges SET response=? WHERE ticket=?", (stable, ticket))

    def _current(self, db, request):
        admissions = request["admissions"]
        require(
            len({canonical(a["selector"]) for a in admissions}) == len(admissions),
            "invalid_input",
            400,
        )
        grants = []
        for admission in admissions:
            saved = db.execute(
                "SELECT * FROM admission_history WHERE receipt_id=?",
                (admission["source"]["receipt_id"],),
            ).fetchone()
            require(saved is not None, "dependency_unavailable", 503)
            require(saved["admission"] == canonical(admission))
            historic = loads(saved["entry_document"])
            allowed = False
            try:
                current = self.auth.entry(db, saved["entry_id"])
                require(
                    all(
                        current[k] == historic[k]
                        for k in ("owner", "account", "channel", "actor_id", "audience")
                    )
                )
                self.auth.route(current, "companion", "memory", "dialogue")
                origin = db.execute(
                    "SELECT ingress,input_entry_id FROM actor_origins WHERE ref=?",
                    (admission["accepted_origin"]["assertion_ref"],),
                ).fetchone()
                require(origin is not None)
                input_entry = self._entry(db, origin["input_entry_id"])
                require(saved["entry_id"] in input_entry["actor_entries"])
                require(
                    all(
                        input_entry[k] == historic[k]
                        for k in ("owner", "account", "channel", "audience")
                    )
                )
                self.auth.route(current, origin[0], "companion", "dialogue")
                require(self.origins.scope(db, current) == admission["scope"])
                binding = db.execute(
                    "SELECT version FROM identities WHERE account=?", (saved["account"],)
                ).fetchone()
                require(binding is not None and binding[0] == admission["binding_version"])
                allowed = True
            except Fault as exc:
                if exc.status != 403:
                    raise
            grants.append(
                {
                    "selector": admission["selector"],
                    "admission_digest": digest(admission),
                    "entry_id": saved["entry_id"],
                    "entry_digest": digest(historic),
                    "account": loads(saved["account"]),
                    "scope": admission["scope"],
                    "binding_version": admission["binding_version"],
                    "state": "allowed" if allowed else "denied",
                }
            )
        context = None
        if request["viewer"] is not None:
            viewer = request["viewer"]
            _, _, context = self.origins.context(
                db, viewer["origin"]["assertion_ref"], "companion", "memory", "dialogue"
            )
            require(
                context["allowed_scope"] == viewer["scope"]
                and all(viewer["scope"][k] is not None for k in ("person_id", "conversation_id"))
            )
        row = db.execute(
            "SELECT generation,sequence FROM authority_head WHERE singleton=1"
        ).fetchone()
        result = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "operation": "current",
            "request_digest": digest(request),
            "head": dict(row),
            "grants": grants,
            "viewer_context": context,
        }
        self.contracts.check("sources#current_access_response", result)
        return result

    async def dispatch(self, header, ingest):
        """Read an actual authenticated HTTPS Core response, then atomically confirm it."""
        from .transport import core_post

        with self.store.connect(write=True) as db:
            self.auth.authenticate(header, db, "source.dispatch")
        ticket = self.prepare_mapping(header, ingest)
        response = await core_post(self.core, ingest)
        with self.store.connect(write=True) as db:
            principal, _ = self.auth.authenticate(header, db, "source.dispatch")
            exchange = db.execute(
                "SELECT prepared_by FROM fanout_exchanges WHERE ticket=?", (ticket,)
            ).fetchone()
            require(exchange[0] == principal)
            self._confirm(db, ticket, response)
        return response
