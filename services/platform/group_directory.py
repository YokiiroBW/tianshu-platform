"""Body-free group discovery and per-group edits owned by BotObservation."""

from contextlib import closing
import asyncio
import copy
import math
import sqlite3
import time

from . import group_policy
from .contracts import Fault, digest, require
from .role_runtime import is_placeholder_actor


class GroupDirectory:
    def __init__(self, owner):
        self.owner = owner
        self.next_poll = {}
        self.cursors = {}
        self.tasks = {}

    def schedule(self, row):
        key = row["id"]
        previous = self.tasks.get(key)
        if previous is not None and not previous.done():
            return
        if previous is not None and not previous.cancelled():
            previous.result()
        if time.monotonic() >= self.next_poll.get(key, 0):
            self.tasks[key] = asyncio.create_task(self.refresh(row))

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def refresh(self, row):
        """Discovery remains available when content observation is disabled."""
        key = row["id"]
        now = time.monotonic()
        if now < self.next_poll.get(key, 0):
            return
        self.next_poll[key] = now + 15
        try:
            result = await self.owner._plugin(
                row,
                "groups",
                {"account_id": row["account_id"], "cursor": self.cursors.get(key), "limit": 100},
            )
            require(
                isinstance(result, dict)
                and set(result)
                == {"schema_version", "instance_id", "account_id", "groups", "next_cursor"}
                and type(result["schema_version"]) is int
                and result["schema_version"] == 1
                and result["instance_id"] == row["instance_id"]
                and result["account_id"] == row["account_id"]
                and isinstance(result["groups"], list)
                and len(result["groups"]) <= 100,
                "adapter_incompatible",
                502,
            )
            cursor = result["next_cursor"]
            require(
                cursor is None
                or isinstance(cursor, str)
                and group_policy.GROUP_ID.fullmatch(cursor),
                "adapter_incompatible",
                502,
            )
            groups = [self._metadata(item) for item in result["groups"]]
            require(
                len({item["group_id"] for item in groups}) == len(groups),
                "adapter_incompatible",
                502,
            )
            async with self.owner.lock:
                await self.owner._local(self._merge, row, groups)
            self.cursors[key] = cursor
        except (Fault, OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # An older/offline SDK cannot stop the existing content pump or erase
            # already discovered groups. Its next metadata page is retried later.
            return

    @staticmethod
    def _metadata(item):
        fields = {
            "group_id",
            "name",
            "member_count",
            "max_member_count",
            "first_seen_at",
            "last_seen_at",
            "updated_at",
        }
        require(isinstance(item, dict) and set(item) == fields, "adapter_incompatible", 502)
        require(
            isinstance(item["group_id"], str) and group_policy.GROUP_ID.fullmatch(item["group_id"]),
            "adapter_incompatible",
            502,
        )
        require(
            item["name"] is None or isinstance(item["name"], str) and 0 < len(item["name"]) <= 128,
            "adapter_incompatible",
            502,
        )
        for field in ("member_count", "max_member_count"):
            require(
                item[field] is None or type(item[field]) is int and item[field] >= 0,
                "adapter_incompatible",
                502,
            )
        for field in ("first_seen_at", "last_seen_at", "updated_at"):
            value = item[field]
            require(
                field == "updated_at"
                and value is None
                or type(value) in (int, float)
                and math.isfinite(value)
                and value >= 0,
                "adapter_incompatible",
                502,
            )
        require(item["first_seen_at"] <= item["last_seen_at"], "adapter_incompatible", 502)
        return copy.deepcopy(item)

    def _merge(self, expected, items):
        current = self.owner.get(expected["id"])
        if current is None or any(
            current[key] != expected[key] for key in ("instance_id", "account_id")
        ):
            return
        known = copy.deepcopy(current.get("known_groups", {}))
        for item in items:
            prior = known.get(item["group_id"])
            if prior:
                item["first_seen_at"] = min(prior["first_seen_at"], item["first_seen_at"])
                item["last_seen_at"] = max(prior["last_seen_at"], item["last_seen_at"])
                if (prior.get("updated_at") or 0) > (item.get("updated_at") or 0):
                    for field in ("name", "member_count", "max_member_count", "updated_at"):
                        item[field] = prior[field]
                else:
                    for field in ("name", "member_count", "max_member_count"):
                        if item[field] is None:
                            item[field] = prior[field]
            known[item["group_id"]] = item
        if known != current.get("known_groups", {}):
            current["known_groups"] = known
            self.owner.catalog.put(current)

    def entries(self):
        entries = []
        for row in self.owner.rows():
            known = copy.deepcopy(row.get("known_groups", {}))
            archived = {}
            with closing(self.owner._db()) as db:
                for target in db.execute(
                    "SELECT conversation,MIN(last_at) AS first_at,MAX(last_at) AS last_at "
                    "FROM discovered WHERE connection_id=? AND archive_epoch=? "
                    "AND conversation LIKE 'group:%' GROUP BY conversation",
                    (row["id"], row["archive_epoch"]),
                ):
                    group_id = target["conversation"].split(":", 1)[1]
                    archived[group_id] = True
                    known.setdefault(
                        group_id,
                        dict(
                            group_id=group_id,
                            name=None,
                            member_count=None,
                            max_member_count=None,
                            first_seen_at=target["first_at"],
                            last_seen_at=target["last_at"],
                            updated_at=None,
                        ),
                    )
            for group_id in group_policy.normalize(row["group_policy"])["groups"]:
                known.setdefault(
                    group_id,
                    dict(
                        group_id=group_id,
                        name=None,
                        member_count=None,
                        max_member_count=None,
                        first_seen_at=None,
                        last_seen_at=None,
                        updated_at=None,
                    ),
                )
            for group_id, metadata in known.items():
                rule = group_policy.effective(row["group_policy"], group_id)
                actor = rule["actor_id"]
                entries.append(
                    {
                        **metadata,
                        "id": "group-directory:" + digest([row["id"], group_id])[:32],
                        "owner_id": row["id"],
                        "owner_name": row["name"],
                        "account_id": row["account_id"],
                        "revision": row["revision"],
                        "rule": copy.deepcopy(rule),
                        "pending": row["pending"] is not None,
                        "read_enabled": row["read_enabled"],
                        "has_archive": bool(archived.get(group_id)),
                        "state": row["state"],
                        "last_error": row.get("last_error"),
                        "ordinary_claim_available": row.get("ordinary_claim_available", False),
                        "memory_group_id": "observed-group:"
                        + digest([row["id"], "group:" + group_id, actor])[:32]
                        if actor
                        else None,
                    }
                )
        return sorted(entries, key=lambda item: (item["owner_id"], item["group_id"]))

    def view(self, console, session, body):
        require(set(body) == {"limit", "cursor"}, "invalid_input", 400)
        limit, cursor = body["limit"], body["cursor"]
        require(type(limit) is int and 1 <= limit <= 100, "invalid_input", 400)
        require(
            cursor is None
            or isinstance(cursor, dict)
            and set(cursor) == {"owner_id", "group_id"}
            and all(isinstance(value, str) for value in cursor.values()),
            "invalid_input",
            400,
        )
        code = self.owner.p.bots._web_code(console, session)
        available = self.owner.catalog is not None and code != "operator_not_authorized"
        rows = self.entries() if available else []
        if cursor:
            rows = [
                item
                for item in rows
                if (item["owner_id"], item["group_id"]) > (cursor["owner_id"], cursor["group_id"])
            ]
        page = rows[:limit]
        actors = (
            self.owner.adapters.config["actors"] + self.owner.p.role_runtime.active_actors()
            if available
            else []
        )
        actors = {
            item["id"]: item
            for item in actors
            if not is_placeholder_actor(item["id"], self.owner.p.role_runtime.get(item["id"]))
        }
        return dict(
            available=available,
            unlocked=available and code == "ready",
            groups=page,
            actors=list(actors.values()),
            next_cursor={key: page[-1][key] for key in ("owner_id", "group_id")}
            if len(rows) > limit
            else None,
        )

    async def change(self, body):
        require(
            set(body) == {"owner_id", "group_id", "expected_revision", "rule", "client_id"},
            "invalid_input",
            400,
        )
        require(
            isinstance(body["owner_id"], str)
            and isinstance(body["group_id"], str)
            and group_policy.GROUP_ID.fullmatch(body["group_id"]),
            "invalid_input",
            400,
        )
        require(
            isinstance(body["client_id"], str) and 0 < len(body["client_id"]) <= 128,
            "invalid_input",
            400,
        )
        fingerprint = digest(["observation-group-policy", body])
        receipt = self.owner.catalog.receipt(body["client_id"], fingerprint)
        if receipt:
            return self.owner.get(receipt["id"])
        row = self.owner.get(body["owner_id"])
        require(row is not None, "not_found", 404)
        require(
            type(body["expected_revision"]) is int and row["revision"] == body["expected_revision"],
            "version_conflict",
            409,
        )
        require(
            any(
                item["owner_id"] == row["id"] and item["group_id"] == body["group_id"]
                for item in self.entries()
            ),
            "not_found",
            404,
        )
        group = group_policy.copy_policy(row["group_policy"])
        group["groups"][body["group_id"]] = body["rule"]
        return await self.owner.policy(
            dict(
                id=row["id"],
                expected_revision=row["revision"],
                group_policy=group,
                private_policy=copy.deepcopy(row["private_policy"]),
                client_id=body["client_id"],
            ),
            intent_fingerprint=fingerprint,
        )
