"""Read-only graph projections of existing owners, never another execution ledger.

The host may attach an async event_reader accepting keyword-only correlation_id,
start_ns, end_ns and limit. It returns {events: [frozen safe event], complete: bool}.
It owns the fixed-source guard query, credentials, bounded work and shutdown. This
product knows neither a Loki address nor a token. Missing readers stay unconfigured.
"""

import asyncio
import math
import re
import time
from datetime import datetime

from . import group_policy
from .bot_observation import decision
from .contracts import Fault, digest, require, utc
from .diagnostics import FIELDS, LEVELS, OUTCOMES, SERVICE_NAMES, UUID_PATTERN
from .role_runtime import is_placeholder_actor

PREFIX = "/api/web/flow-graph/"
CORRELATION = re.compile(r"[a-f0-9]{32}\Z")
WORD = re.compile(r"[a-z][a-z0-9_.]{0,63}\Z")
ERROR_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
STAGES = (
    ("message", "消息入口"),
    ("companion", "陪伴处理"),
    ("memory", "记忆读取"),
    ("knowledge", "资料读取"),
    ("model", "模型调用"),
    ("reply", "回复投递"),
    ("learning", "异步记忆"),
    ("schedule", "日程与生活"),
    ("receipt", "回执"),
)


def event_stage(event):
    """Only explicit registered business words identify a phase, never an HTTP guess."""
    name = event["event"]
    if event["service"] == "gateway":
        if name in {"upstream.call_started", "upstream.first_output", "upstream.call_finished"}:
            return "model"
        if name == "receipt.persist_failed":
            return "receipt"
        return None
    if event["service"] != "companion":
        return None
    if name in {"turn.memory.started", "turn.memory.finished"}:
        return "memory"
    if name in {"turn.knowledge.started", "turn.knowledge.finished"}:
        return "knowledge"
    if name in {"turn.delivery.started", "turn.delivery.finished"}:
        return "reply"
    if name in {"memory.phase.started", "memory.phase.finished", "outbox.flush"}:
        return "learning"
    if name in {"proactive.reconcile.started", "proactive.reconcile.finished"}:
        return "receipt"
    if name in {"proactive.delivery.started", "proactive.delivery.finished"}:
        return "schedule"
    if name in {
        "turn.prepared",
        "turn.queued",
        "turn.generation.started",
        "turn.generation.finished",
        "turn.cancelled",
    }:
        return "companion"
    return None


def safe_events(value, correlation_id, start_ns, end_ns, limit):
    """Revalidate the frozen scalar boundary; unknown fields never reach a browser."""
    require(
        isinstance(value, dict)
        and isinstance(value.get("events"), list)
        and type(value.get("complete")) is bool,
        "invalid_upstream",
        502,
    )
    events, seen, instants = [], {}, {}
    for row in value["events"]:
        require(isinstance(row, dict) and set(row) == set(FIELDS), "invalid_upstream", 502)
        require(
            row["schema_version"] == "1.0.0"
            and row["service"] in SERVICE_NAMES
            and row["level"] in LEVELS
            and row["outcome"] in OUTCOMES
            and isinstance(row["event"], str)
            and WORD.fullmatch(row["event"])
            and isinstance(row["instance_id"], str)
            and UUID_PATTERN.fullmatch(row["instance_id"])
            and isinstance(row["event_id"], str)
            and UUID_PATTERN.fullmatch(row["event_id"])
            and type(row["sequence"]) is int
            and row["sequence"] > 0
            and (
                row["correlation_id"] is None
                or isinstance(row["correlation_id"], str)
                and CORRELATION.fullmatch(row["correlation_id"])
            )
            and (
                row["error_code"] is None
                or isinstance(row["error_code"], str)
                and ERROR_WORD.fullmatch(row["error_code"])
            ),
            "invalid_upstream",
            502,
        )
        duration = row["duration_ms"]
        require(
            duration is None
            or type(duration) in (int, float)
            and math.isfinite(duration)
            and duration >= 0,
            "invalid_upstream",
            502,
        )
        try:
            require(isinstance(row["timestamp"], str), "invalid_upstream", 502)
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            require(stamp.utcoffset() is not None, "invalid_upstream", 502)
            ns = int(stamp.timestamp() * 1_000_000_000)
        except (ValueError, TypeError, OverflowError):
            raise Fault("invalid_upstream", 502) from None
        require(start_ns <= ns <= end_ns, "invalid_upstream", 502)
        require(
            correlation_id is None or row["correlation_id"] == correlation_id,
            "invalid_upstream",
            502,
        )
        key = (row["service"], row["instance_id"], row["sequence"])
        if key in seen:
            require(seen[key] == row, "invalid_upstream", 502)
            continue
        seen[key] = row
        instants[key] = ns
        events.append({key: row[key] for key in FIELDS})
    events.sort(
        key=lambda r: (
            instants[(r["service"], r["instance_id"], r["sequence"])],
            r["service"],
            r["instance_id"],
            r["sequence"],
        )
    )
    return events[:limit], value["complete"] and len(events) <= limit


class WebFlowGraph:
    def __init__(self, console, *, event_reader=None):
        self.console = console
        self.p = console.platform
        self.event_reader = event_reader

    def _gate(self, session):
        require(self.console.session_valid(session), "session_expired", 401)
        principal = self.p.auth.principals[self.console.config["principal"]]
        # The same operator action used by the existing bot views; login alone
        # does not enumerate another account's topology or operational events.
        require("bot.manage" in principal["actions"], "operator_not_authorized", 403)

    async def route(self, path, body, session):
        from .web_flow_graph_routes import route

        await self.p.local_work.run(self._gate, session)
        result = await route(self, path, body, session)
        await self.p.local_work.run(self._gate, session)
        return result

    async def relationships(self, session):
        adapters = await self.p.bot_adapters.route(
            self.console, "/api/web/bot-adapters/view", {}, session
        )
        legacy = await self.p.bots.web_route(self.console, "/api/web/bots/view", {}, session)
        observations = await self.p.bot_observation.route(
            self.console, "/api/web/bot-observation/view", {}, session
        )
        principal = self.p.auth.principals[self.console.config["principal"]]
        rows = (
            await self.p.local_work.run(self.p.role_runtime.directory)
            if "role.manage" in principal["actions"]
            else []
        )
        roles = {}
        for role in rows:
            actor = role["actor_id"]
            if is_placeholder_actor(actor, role):
                continue
            roles[actor] = {
                "id": actor,
                "label": role["name"],
                "state": role["state"],
                "enabled": role["enabled"],
                "profile_id": role.get("profile_id"),
                "profile_version": role.get("profile_version"),
                "capabilities": list(role.get("capabilities", [])),
                "source": "runtime",
            }
        for actor in adapters["actors"]:
            roles.setdefault(
                actor["id"],
                {
                    "id": actor["id"],
                    "label": actor["label"],
                    "state": "registered",
                    "enabled": None,
                    "profile_id": None,
                    "profile_version": None,
                    "capabilities": [],
                    "source": "registered",
                },
            )
        accounts, windows, defaults = {}, {}, []

        def account(adapter, instance, identity, label, state, source):
            key = "account:" + digest([adapter, instance, identity])[:24]
            existing = accounts.get(key)
            if existing is None:
                accounts[key] = {
                    "id": key,
                    "label": label,
                    "adapter": adapter,
                    "account_id": identity,
                    "instance_id": instance,
                    "states": [state],
                    "sources": [source],
                }
            else:
                existing["states"] = sorted(set([*existing["states"], state]))
                existing["sources"] = sorted(set([*existing["sources"], source]))
            return key

        def register_actor(actor):
            if actor:
                roles.setdefault(
                    actor,
                    {
                        "id": actor,
                        "label": actor,
                        "state": "unverified",
                        "enabled": None,
                        "profile_id": None,
                        "profile_version": None,
                        "capabilities": [],
                        "source": "binding",
                    },
                )

        def window(
            key,
            conversation,
            actor_ids,
            *,
            binding,
            mode,
            state,
            source,
            edit=None,
            observe=None,
            reply_permitted=None,
        ):
            kind, _, target = conversation.partition(":")
            if kind not in {"group", "private"} or not target:
                return
            identifier = "window:" + digest([key, conversation, source])[:24]
            actors = [actor for actor in actor_ids if actor]
            for actor in actors:
                register_actor(actor)
            windows[identifier] = {
                "id": identifier,
                "label": ("群聊 " if kind == "group" else "私聊 ") + target,
                "conversation_id": conversation,
                "kind": kind,
                "account_id": key,
                "actor_ids": actors,
                "binding": binding if actors else "unbound",
                "mode": mode,
                "observe": mode != "disabled" if observe is None else observe,
                "reply_permitted": mode == "reply" if reply_permitted is None else reply_permitted,
                "state": state,
                "source": source,
                "edit": edit,
            }

        for item in adapters["connections"]:
            registered = self.p.bot_adapters.catalog.get(item["id"])
            key = account(
                item["adapter"],
                registered["instance_id"],
                item["account_id"],
                item["name"],
                item["state"],
                "adapter",
            )
            conversation = item["conversation"]
            window(
                key,
                conversation["kind"] + ":" + conversation["id"],
                [item["actor_id"]],
                binding="explicit",
                mode="reply" if item["enabled"] else "disabled",
                state=item["state"],
                source="adapter",
            )
        connection_by_slot = {row["slot_id"]: row for row in legacy["connections"]}
        managed_ids = {row["id"] for row in adapters["connections"]}
        for slot in legacy["slots"]:
            connection = connection_by_slot.get(slot["slot_id"])
            if connection and connection["connection_id"] in managed_ids:
                continue
            state = connection["state"] if connection else "not_created"
            key = account(
                slot["adapter"],
                slot["platform_id"],
                slot["self_id"],
                slot["label"],
                state,
                "registered",
            )
            window(
                key,
                slot["conversation_id"],
                connection["actor_ids"] if connection else slot["actor_ids"],
                binding="registered",
                mode="reply" if connection and connection["enabled"] else "disabled",
                state=state,
                source="registered",
            )
        discovered_complete = True
        discovery_budget = 500
        for row in observations["connections"]:
            key = account(
                row["adapter"],
                row["instance_id"],
                row["account_id"],
                row["name"],
                row["state"],
                "observation",
            )
            group = group_policy.normalize(row["group_policy"])
            for kind, policy in (("group", group["default"]), ("private", row["private_policy"])):
                register_actor(policy["actor_id"])
                defaults.append(
                    {
                        "account_id": key,
                        "kind": kind,
                        "actor_id": policy["actor_id"],
                        "mode": policy["mode"],
                        "observe": policy.get("observe", policy["mode"] != "disabled"),
                    }
                )
            configured = {"group:" + target for target in group["groups"]}
            configured.update("private:" + target for target in row["private_policy"]["list"])
            discovered = set()
            if observations["unlocked"] and row["read_enabled"]:
                cursor = None
                while discovery_budget > 0:
                    result = await self.p.bot_observation.route(
                        self.console,
                        "/api/web/bot-observation/discovered",
                        {"id": row["id"], "limit": min(100, discovery_budget), "cursor": cursor},
                        session,
                    )
                    discovered.update(item["conversation"] for item in result["items"])
                    discovery_budget -= len(result["items"])
                    cursor = result["next_cursor"]
                    if not cursor:
                        break
                if cursor or discovery_budget <= 0:
                    discovered_complete = False
            else:
                discovered_complete = False
            for conversation in sorted(configured | discovered):
                is_group = conversation.startswith("group:")
                policy = (
                    group_policy.effective(group, conversation)
                    if is_group
                    else row["private_policy"]
                )
                binding = "explicit" if is_group and conversation in configured else "inherited"
                verdict = decision(
                    row["group_policy"] if is_group else row["private_policy"],
                    conversation.split(":", 1)[1],
                    False,
                    "group" if is_group else "private",
                )
                window(
                    key,
                    conversation,
                    [policy["actor_id"]],
                    binding=binding,
                    mode=policy["mode"],
                    state=row["state"],
                    source="observation",
                    observe=verdict["observe"],
                    reply_permitted=verdict["reply_permitted"],
                    edit={
                        "kind": "group",
                        "connection_id": row["id"],
                        "revision": row["revision"],
                        "unlocked": observations["unlocked"],
                    }
                    if is_group
                    else None,
                )
        assignable = {actor["id"] for actor in adapters["actors"]}
        return {
            "generated_at": utc(time.time()),
            "accounts": list(accounts.values()),
            "roles": [{**role, "assignable": role["id"] in assignable} for role in roles.values()],
            "conversations": list(windows.values()),
            "defaults": defaults,
            "discovered_complete": discovered_complete,
            "sources": [
                {"id": key, "available": view["available"]}
                for key, view in (
                    ("adapters", adapters),
                    ("registered", legacy),
                    ("observation", observations),
                )
            ],
        }

    async def query(self, correlation_id, start_ns, end_ns, limit):
        if self.event_reader is None:
            return {"availability": "not_configured", "events": [], "complete": False}
        try:
            result = await self.event_reader(
                correlation_id=correlation_id, start_ns=start_ns, end_ns=end_ns, limit=limit
            )
            events, complete = safe_events(result, correlation_id, start_ns, end_ns, limit)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reader errors may carry URLs, credentials or backend response text.
            raise Fault("flow_query_unavailable", 503) from None
        return {"availability": "ready", "events": events, "complete": complete}

    async def traces(self, minutes):
        end_ns = time.time_ns()
        start_ns = end_ns - minutes * 60 * 1_000_000_000
        result = await self.query(None, start_ns, end_ns, 1500)
        grouped = {}
        for event in result["events"]:
            correlation = event["correlation_id"]
            if correlation is not None:
                grouped.setdefault(correlation, []).append(event)
        traces = []
        for correlation, events in grouped.items():
            traces.append(
                {
                    "correlation_id": correlation,
                    "first_at": events[0]["timestamp"],
                    "last_at": events[-1]["timestamp"],
                    "event_count": len(events),
                    "services": sorted({row["service"] for row in events}),
                    "business_observed": any(event_stage(row) is not None for row in events),
                    "has_failure": any(row["outcome"] in {"failed", "rejected"} for row in events),
                    "has_unknown": any(row["outcome"] in {"unknown", "degraded"} for row in events),
                }
            )
        traces.sort(
            key=lambda row: (row["business_observed"], row["last_at"], row["correlation_id"]),
            reverse=True,
        )
        return {
            "availability": result["availability"],
            "window": {"start_ns": str(start_ns), "end_ns": str(end_ns)},
            "traces": traces[:30],
            "complete": result["complete"] and len(traces) <= 30,
            "event_count": len(result["events"]),
        }

    async def trace(self, correlation_id, start_ns, end_ns):
        result = await self.query(correlation_id, start_ns, end_ns, 1000)
        return {
            **result,
            "correlation_id": correlation_id,
            "window": {"start_ns": str(start_ns), "end_ns": str(end_ns)},
            "stages": [
                {
                    "id": identifier,
                    "label": label,
                    "event_ids": [
                        row["event_id"]
                        for row in result["events"]
                        if event_stage(row) == identifier
                    ],
                }
                for identifier, label in STAGES
            ],
        }
