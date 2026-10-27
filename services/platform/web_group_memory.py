"""One registered group window over Chat raw interactions and Memory topic projections."""

import asyncio
import ssl
import uuid

import aiohttp

from .auth import secret
from .candidate_protocol import CandidateProtocol
from .contracts import Fault, canonical, loads, require, utc
from .transport import core_settings

ROUTES = frozenset(
    {"groups", "group-insights", "group-history", "group-evidence", "group-records", "group-topics"}
)
CHAT_MANIFEST = "4395f4207ef4bcaacb45522213aa622c5603d79006c29b48b5ff350525082ef4"
TOPIC_MANIFEST = "ce97efa564962779c112a453970f8e93f340ac4366f4df641dff58fde871709b"
ARCHIVE_MANIFEST = "4a99c3bda5e07c06974e9d21224230b7cdcbf3ba68490c67d0ffa4193a263260"
WINDOWS = {"24h": (86400, 3600), "7d": (7 * 86400, 86400), "30d": (30 * 86400, 86400)}


def validate(settings, auth):
    config = settings.get("web_group_memory")
    if config is None:
        return
    require(
        isinstance(config, dict) and set(config) <= {"enabled", "chat", "groups"},
        "invalid_input",
        400,
    )
    require(type(config.get("enabled", False)) is bool, "invalid_input", 400)
    require(
        settings.get("web_memory") is not None and settings.get("web") is not None,
        "invalid_input",
        400,
    )
    if config.get("chat") is not None:
        core_settings(config["chat"])
        require(config["chat"].get("timeout_seconds", 10) <= 10, "invalid_input", 400)
    groups = config.get("groups", [])
    require(isinstance(groups, list) and len(groups) <= 128, "invalid_input", 400)
    ids, entry_ids = set(), set()
    owner = settings["web"]["principal"]
    for group in groups:
        required = {"id", "label", "entry_id", "conversation_id", "chat_channel"}
        require(
            isinstance(group, dict) and required <= set(group) <= required | {"archive_mapping_id"},
            "invalid_input",
            400,
        )
        require(
            all(
                isinstance(group[k], str) and 0 < len(group[k]) <= 128
                for k in ("id", "label", "entry_id", "conversation_id")
            ),
            "invalid_input",
            400,
        )
        require(group["id"] not in ids and group["entry_id"] not in entry_ids, "invalid_input", 400)
        ids.add(group["id"])
        entry_ids.add(group["entry_id"])
        entry = auth.entries.get(group["entry_id"])
        require(
            entry is not None
            and entry["owner"] == owner
            and entry["account"] == auth.principals[owner]["account"]
            and entry["kind"] == "local_operator"
            and entry["audience"] == "group",
            "invalid_input",
            400,
        )
        require(
            entry["routes"]
            == [{"caller": "platform", "receiver": "memory", "purpose": "dialogue"}],
            "invalid_input",
            400,
        )
        require(entry["channel"]["namespace"] == "web", "invalid_input", 400)
        chat = group["chat_channel"]
        require(
            isinstance(chat, dict)
            and set(chat) == {"namespace", "binding_id", "channel_conversation_id", "thread_id"}
            and chat["namespace"] == "qq"
            and chat["thread_id"] is None,
            "invalid_input",
            400,
        )
        require(
            all(
                isinstance(chat[k], str)
                and 0 < len(chat[k]) <= 128
                and not any(c.isspace() for c in chat[k])
                for k in ("binding_id", "channel_conversation_id")
            ),
            "invalid_input",
            400,
        )
        require(
            group.get("archive_mapping_id") is None
            or (
                isinstance(group["archive_mapping_id"], str)
                and 0 < len(group["archive_mapping_id"]) <= 128
                and not any(c.isspace() for c in group["archive_mapping_id"])
            ),
            "invalid_input",
            400,
        )


class GroupMemory:
    def __init__(self, memory):
        self.memory = memory
        self.p = memory.platform
        self.config = self.p.settings.get("web_group_memory")
        self.observed = {}
        self.chat = CandidateProtocol(
            self.p.contracts.root, "chat-history/candidate-v1", CHAT_MANIFEST, response_limit=131072
        )
        self.topics = CandidateProtocol(
            self.p.contracts.root,
            "group-memory-projection/candidate-v1",
            TOPIC_MANIFEST,
            registry=self.p.contracts.registry,
            response_limit=262144,
        )
        self.archive = CandidateProtocol(
            self.p.contracts.root,
            "archive-memory-check/candidate-v1",
            ARCHIVE_MANIFEST,
            registry=self.p.contracts.registry,
            response_limit=262144,
        )

    def _group(self, group_id, actor, version):
        group = self.observed.get(group_id)
        if group is not None:
            from .observed_groups import guard

            require(group["actor_id"] == actor, "forbidden", 403)
            require(
                group.get("entry_id") and group["conversation_id"],
                group.get("unavailable", "memory_identity_not_ready"),
                503,
            )
            guard(self.memory, group)
        else:
            require(
                self.config is not None and self.config.get("enabled", False),
                "group_memory_not_configured",
                503,
            )
            group = next((g for g in self.config["groups"] if g["id"] == group_id), None)
        require(group is not None, "not_found", 404)
        self.memory.role_choice(actor, version)
        with self.p.store.connect() as db:
            entry = self.p.auth.entry(db, group["entry_id"])
            require(entry["actor_id"] == actor, "forbidden", 403)
            require(
                self.p.origins.scope(db, entry, entry_id=group["entry_id"])["conversation_id"]
                == group["conversation_id"],
                "scope_changed",
                409,
            )
            self.p.auth.route(entry, "platform", "memory", "dialogue")
        return group, entry

    def directory(self, actor, version):
        self.memory.role_choice(actor, version)
        items = []
        if self.config and self.config.get("enabled", False):
            for group in self.config["groups"]:
                try:
                    self._group(group["id"], actor, version)
                except Fault as error:
                    if error.status in {403, 404, 409}:
                        continue
                    raise
                items.append({key: group[key] for key in ("id", "label", "conversation_id")})
        existing = {item["id"] for item in items}
        for group in self.observed.values():
            if group["actor_id"] == actor and group["id"] not in existing:
                items.append(
                    {
                        key: group.get(key)
                        for key in (
                            "id",
                            "label",
                            "conversation_id",
                            "mode",
                            "pending",
                            "unavailable",
                            "policy_version",
                        )
                    }
                )
        return {
            "available": bool(items or self.config and self.config.get("enabled", False)),
            "groups": items,
            "chat_configured": bool(self.config and self.config.get("chat")),
            "configure_url": "#/settings/3",
        }

    async def _call(self, peer, path, request, *, limit, session_factory=None):
        _, timeout = core_settings(peer)
        token = secret(peer["token_env"])
        require(token is not None, "dependency_unavailable", 503)
        try:
            tls = (
                None if session_factory else ssl.create_default_context(cafile=peer.get("ca_file"))
            )
            async with (session_factory or aiohttp.ClientSession)(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
            ) as client:
                async with client.post(
                    peer["base_url"].rstrip("/") + path,
                    json=request,
                    headers={"Authorization": "Bearer " + token},
                    ssl=tls,
                    allow_redirects=False,
                ) as response:
                    require(response.content_type == "application/json", "invalid_upstream", 502)
                    raw = bytearray()
                    async for part in response.content.iter_chunked(8192):
                        raw.extend(part)
                        require(len(raw) <= limit, "capacity_exceeded", 413)
                    if response.status != 200:
                        code = {
                            401: "upstream_forbidden",
                            403: "upstream_forbidden",
                            404: "not_found",
                            413: "capacity_exceeded",
                            422: "invalid_input",
                            429: "upstream_busy",
                        }.get(response.status, "dependency_unavailable")
                        raise Fault(code, 403 if response.status in {401, 403} else 503)
                    return loads(bytes(raw))
        except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
            raise Fault("dependency_unavailable", 503) from None

    async def _chat(self, operation, request):
        peer = (self.config or {}).get("chat")
        require(peer is not None, "chat_not_configured", 503)
        self.chat.check(operation.replace("-", "_") + "_request", request)
        answer = await self._call(peer, "/internal/evidence/" + operation, request, limit=131072)
        self.chat.response(operation.replace("-", "_") + "_response", answer)
        require(answer["channel"] == request["channel"], "invalid_upstream", 502)
        return answer

    def _chat_registration(self, group, actor, version):
        require((self.config or {}).get("chat") is not None, "chat_not_configured", 503)
        if "owner_id" not in group:
            return group
        matches = []
        for candidate in (self.config or {}).get("groups", []):
            if candidate["conversation_id"] != group["conversation_id"]:
                continue
            try:
                verified, _ = self._group(candidate["id"], actor, version)
            except Fault:
                continue
            matches.append(verified)
        require(len(matches) == 1, "chat_group_not_registered", 503)
        return matches[0]

    async def _group_stats(self, group, actor, version, request):
        registered = await self.p.local_work.run(self._chat_registration, group, actor, version)
        return await self._chat("group-stats", {**request, "channel": registered["chat_channel"]})

    async def _topics(self, request):
        self.topics.check("request", request)
        peer = {
            key: value
            for key, value in self.memory.config.items()
            if key in {"base_url", "token_env", "timeout_seconds", "ca_file"}
        }
        answer = await self._call(
            peer,
            "/internal/v1/memory/browser/topics",
            request,
            limit=262144,
            session_factory=self.memory.session_factory,
        )
        self.topics.response("response", answer)
        require(
            all(answer[key] == request[key] for key in ("scope", "request_id", "subject")),
            "invalid_upstream",
            502,
        )
        return answer

    async def _archive_rows(self, group, base, records):
        """Join all real native aliases, with a known negative dominating any other alias."""
        mapping = group.get("archive_mapping_id")
        require(mapping is not None, "archive_filter_not_configured", 503)
        rows = {
            (row["locator"], row["stored_content_sha256"]): {"supported": False, "withdrawn": False}
            for row in records
        }
        items = [
            {
                "evidence_id": row["locator"],
                "content_hash": row["stored_content_sha256"],
                "sender_id": row["sender_id"],
                "alias": alias,
            }
            for row in records
            for alias in row["native_aliases"]
        ]
        peer = {
            key: value
            for key, value in self.memory.config.items()
            if key in {"base_url", "token_env", "timeout_seconds", "ca_file"}
        }
        try:
            async with asyncio.timeout(peer.get("timeout_seconds", 10)):
                for start in range(0, len(items), 100):
                    batch = items[start : start + 100]
                    request = {**base, "mapping_id": mapping, "items": batch}
                    self.archive.check("request", request)
                    answer = await self._call(
                        peer,
                        "/internal/v1/memory/browser/archive-check",
                        request,
                        limit=262144,
                        session_factory=self.memory.session_factory,
                    )
                    self.archive.response("response", answer)
                    require(
                        all(
                            answer[key] == request[key]
                            for key in ("scope", "request_id", "mapping_id")
                        )
                        and len(answer["items"]) == len(batch),
                        "invalid_upstream",
                        502,
                    )
                    for expected, actual in zip(batch, answer["items"], strict=True):
                        key = expected["evidence_id"], expected["content_hash"]
                        require(
                            (actual["evidence_id"], actual["content_hash"]) == key,
                            "invalid_upstream",
                            502,
                        )
                        state = rows[key]
                        state["withdrawn"] |= actual["forget_state"] == "withdrawn"
                        state["supported"] |= actual["forget_state"] == "no_known_forget"
        except TimeoutError:
            raise Fault("dependency_unavailable", 503) from None
        visible = [
            row
            for row in records
            if (state := rows[(row["locator"], row["stored_content_sha256"])])["supported"]
            and not state["withdrawn"]
        ]
        return visible, {
            "withdrawn": sum(state["withdrawn"] for state in rows.values()),
            "unsupported": sum(
                not state["supported"] and not state["withdrawn"] for state in rows.values()
            ),
        }

    async def _part(self, call):
        try:
            return {"state": "available", "data": await call}
        except Fault as error:
            return {"state": "unavailable", "code": error.code}

    async def route(self, name, body, session):
        require(name in ROUTES and isinstance(body, dict), "invalid_input", 400)
        await self.p.local_work.run(self.memory.prove_access, session)
        actor, version = body.get("role_id"), body.get("role_version")
        require(
            isinstance(actor, str) and type(version) is int and version >= 0, "invalid_input", 400
        )
        common = {"role_id", "role_version", "group_id"}
        if name == "groups":
            require(set(body) == {"role_id", "role_version"}, "invalid_input", 400)
            await self.p.local_work.run(self.memory.role_choice, actor, version)
            from .observed_groups import prepare

            groups = await prepare(self.memory, actor)
            self.observed = {
                key: item for key, item in self.observed.items() if item["actor_id"] != actor
            }
            self.observed.update({item["id"]: item for item in groups})
            return await self.p.local_work.run(self.directory, actor, version)
        group, entry = await self.p.local_work.run(
            self._group, body.get("group_id"), actor, version
        )
        request_id = "group-web:" + uuid.uuid4().hex
        async with self.memory.scoped_origin(
            actor, version, session, entry_id=group["entry_id"]
        ) as (reference, scope):
            require(
                scope["audience"] == "group"
                and scope["conversation_id"] == group["conversation_id"],
                "scope_changed",
                409,
            )
            base = {
                "schema_version": 1,
                "request_id": request_id,
                "origin": {"assertion_ref": reference},
                "scope": scope,
            }
            subject = {"kind": "group", "conversation_id": scope["conversation_id"]}
            channel = group["chat_channel"]
            if name == "group-insights":
                require(
                    set(body) == common | {"window"} and body["window"] in WINDOWS,
                    "invalid_input",
                    400,
                )
                seconds, bucket = WINDOWS[body["window"]]
                end = int(self.p.origins.clock())
                start = end - seconds + 1
                stats_request = {
                    "schema_version": 1,
                    "channel": channel,
                    "start_timestamp": start,
                    "end_timestamp": end,
                    "bucket_seconds": bucket,
                    "timezone_offset_minutes": 480,
                    "message_limit": 10000,
                }
                topic_request = {
                    **base,
                    "subject": subject,
                    "start_at": utc(start),
                    "end_at": utc(end),
                    "limit": 50,
                    "cursor": None,
                }
                stats, topics, records = await asyncio.gather(
                    self._part(self._group_stats(group, actor, version, stats_request)),
                    self._part(self._topics(topic_request)),
                    self._part(
                        self.memory._read(
                            "records", {**base, "subject": subject, "limit": 20, "cursor": None}
                        )
                    ),
                )
                result = {
                    "group": {key: group[key] for key in ("id", "label", "conversation_id")},
                    "window": {"start_timestamp": start, "end_timestamp": end},
                    "stats": stats,
                    "topics": topics,
                    "records": records,
                }
                # Scope/origin stay inside Platform. The selected group's public projection remains.
                for part in (topics, records):
                    if part["state"] == "available":
                        part["data"] = {
                            key: value
                            for key, value in part["data"].items()
                            if key not in {"scope", "request_id"}
                        }
            elif name in {"group-topics", "group-records"}:
                extra = (
                    {"cursor", "start_timestamp", "end_timestamp"}
                    if name == "group-topics"
                    else {"cursor"}
                )
                require(set(body) == common | extra, "invalid_input", 400)
                from .web_readers import _cursor

                request = {
                    **base,
                    "subject": subject,
                    "limit": 20,
                    "cursor": _cursor(body["cursor"]),
                }
                if name == "group-topics":
                    start, end = body["start_timestamp"], body["end_timestamp"]
                    require(
                        type(start) is int
                        and type(end) is int
                        and 0 <= start <= end <= int(self.p.origins.clock())
                        and end - start < 31 * 86400,
                        "invalid_input",
                        400,
                    )
                    request.update(start_at=utc(start), end_at=utc(end), limit=50)
                    answer = await self._topics(request)
                else:
                    answer = await self.memory._read("records", request)
                result = {
                    key: value
                    for key, value in answer.items()
                    if key not in {"scope", "request_id"}
                }
            elif name == "group-history":
                require(set(body) == common | {"filters", "cursor", "limit"}, "invalid_input", 400)
                group = await self.p.local_work.run(self._chat_registration, group, actor, version)
                channel = group["chat_channel"]
                result = await self._chat(
                    "search",
                    {
                        "schema_version": 1,
                        "channel": channel,
                        "filters": body["filters"],
                        "cursor": body["cursor"],
                        "limit": body["limit"],
                    },
                )
                if result["records"]:
                    checked = await self._chat(
                        "check",
                        {
                            "schema_version": 1,
                            "channel": channel,
                            "items": [
                                {
                                    "locator": row["locator"],
                                    "expected_stored_content_sha256": row["stored_content_sha256"],
                                }
                                for row in result["records"]
                            ],
                        },
                    )
                    require(
                        len(checked["results"]) == len(result["records"])
                        and all(
                            item["locator"] == row["locator"]
                            and item["status"] == "archive_current"
                            and item["current_stored_content_sha256"]
                            == row["stored_content_sha256"]
                            for item, row in zip(checked["results"], result["records"], strict=True)
                        ),
                        "scope_changed",
                        409,
                    )
                archive_returned = len(result["records"])
                result["records"], result["visibility"] = await self._archive_rows(
                    group, base, result["records"]
                )
                result["visibility"]["archive_returned"] = archive_returned
                result["coverage"]["returned"] = len(result["records"])
            else:
                require(
                    set(body) == common | {"locator", "stored_content_sha256"}, "invalid_input", 400
                )
                group = await self.p.local_work.run(self._chat_registration, group, actor, version)
                channel = group["chat_channel"]
                result = await self._chat(
                    "read-locator",
                    {
                        "schema_version": 1,
                        "channel": channel,
                        "locator": body["locator"],
                        "expected_stored_content_sha256": body["stored_content_sha256"],
                        "before": 2,
                        "after": 2,
                    },
                )
                if result["status"] == "archive_current":
                    context = result["context"]
                    visible, result["visibility"] = await self._archive_rows(
                        group, base, [*context["before"], result["target"], *context["after"]]
                    )
                    allowed = {row["locator"] for row in visible}
                    if result["target"]["locator"] not in allowed:
                        result.update(status="missing", target=None, context=None)
                    else:
                        result["context"] = {
                            **context,
                            "before": [
                                row for row in context["before"] if row["locator"] in allowed
                            ],
                            "after": [row for row in context["after"] if row["locator"] in allowed],
                        }
            await self.p.local_work.run(self._group, group["id"], actor, version)
            require(len(canonical(result).encode()) <= 1024 * 1024, "capacity_exceeded", 413)
        return result
