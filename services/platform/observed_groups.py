"""Browser delegations from the existing physical observation owner."""

import copy
from contextlib import closing

from . import group_policy
from .contracts import canonical, digest, require


def candidates(memory, actor):
    p = memory.platform
    owner = getattr(p, "bot_observation", None)
    if owner is None or owner.catalog is None:
        return []
    result = []
    for row in owner.rows():
        groups = set(group_policy.normalize(row["group_policy"])["groups"])
        with closing(owner._db()) as db:
            groups.update(
                r[0].split(":", 1)[1]
                for r in db.execute(
                    "SELECT DISTINCT conversation FROM discovered WHERE connection_id=? AND conversation LIKE 'group:%'",
                    (row["id"],),
                )
            )
        for target in sorted(groups):
            conversation = "group:" + target
            rule = group_policy.effective(row["group_policy"], target)
            if rule["actor_id"] != actor:
                continue
            channel = owner.segment_channel(row, conversation, actor)
            identity = "observed-group:" + digest([row["id"], conversation, actor])[:32]
            result.append(
                dict(
                    id=identity,
                    label=row["name"] + " · 群 " + target,
                    owner_id=row["id"],
                    physical_conversation=conversation,
                    actor_id=actor,
                    chat_channel=channel,
                    mode=rule["mode"],
                    pending=bool(row["pending"]),
                    readable=bool(row["read_enabled"]),
                    policy_version=group_policy.revision(row, conversation),
                )
            )
    return result


def guard(memory, group):
    p, actor = memory.platform, group["actor_id"]
    owner = p.bot_observation
    row = owner.get(group["owner_id"])
    require(row is not None and row["read_enabled"], "forbidden", 403)
    require(not row["pending"], "dependency_unavailable", 503)
    require(
        owner.actor(row, group["physical_conversation"]) == actor
        and owner._reply_role_active(actor),
        "scope_changed",
        409,
    )
    require(
        owner.segment_channel(row, group["physical_conversation"], actor) == group["chat_channel"],
        "scope_changed",
        409,
    )


def register(memory, group, conversation_id):
    """Only a verified Core frame response can supply conversation_id."""
    p = memory.platform
    guard(memory, group)
    channel = group["chat_channel"]
    with p.store.connect(write=True) as db:
        saved = db.execute(
            "SELECT conversation_id FROM channels WHERE channel=?", (canonical(channel),)
        ).fetchone()
        require(saved is None or saved[0] == conversation_id, "scope_changed", 409)
        if saved is None:
            db.execute("INSERT INTO channels VALUES(?,?)", (canonical(channel), conversation_id))
    template = p.auth.entries[memory.config["entry_id"]]
    entry_id = group["id"] + ":web"
    entry = copy.deepcopy(template)
    entry.update(
        actor_id=group["actor_id"],
        audience="group",
        routes=[dict(caller="platform", receiver="memory", purpose="dialogue")],
    )
    entry["channel"] = dict(
        namespace="web", binding_id=entry_id, channel_conversation_id=group["id"], thread_id=None
    )
    p.auth.entries[entry_id] = entry
    p.origins.read_channels[entry_id] = channel
    p.origins.read_guards[entry_id] = lambda: guard(memory, group)
    return {**group, "entry_id": entry_id, "conversation_id": conversation_id}


async def prepare(memory, actor):
    p = memory.platform
    groups = await p.local_work.run(candidates, memory, actor)
    prepared = []
    for group in groups:
        if group["pending"] or not group["readable"]:
            prepared.append(
                {
                    **group,
                    "conversation_id": None,
                    "unavailable": "policy_pending" if group["pending"] else "history_disabled",
                }
            )
            continue
        try:
            await p.local_work.run(guard, memory, group)
            request = dict(
                schema_version=1,
                actor_id=actor,
                channel=group["chat_channel"],
                delivery_channel=None,
                conversation_id=None,
            )
            p.contracts.check("memory-extraction#segment_frame_request", request)
            response = await p.bot_adapters._core("learning-frame", request)
            p.contracts.check("memory-extraction#segment_frame_response", response)
            require(
                response["valid"]
                and response["channel"] == request["channel"]
                and response["delivery_channel"] is None
                and response["frame"]["actor_id"] == actor
                and response["frame"]["audience"] == "group",
                "dependency_unavailable",
                503,
            )
            prepared.append(
                await p.local_work.run(
                    register, memory, group, response["frame"]["conversation_id"]
                )
            )
        except Exception as error:
            from .contracts import Fault

            if not isinstance(error, Fault):
                raise
            prepared.append({**group, "conversation_id": None, "unavailable": error.code})
    return prepared
