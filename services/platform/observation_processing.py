"""Current automatic-work policy, deliberately separate from historical source reads."""

import re
from contextlib import closing

from . import group_policy
from .contracts import canonical, require


def native_frame(owner, frame):
    if "channel" not in frame:
        return frame, None
    channel = frame["channel"]
    candidates = []
    # Both generations of admitted delivery and the stable learning frame are
    # owner-issued channels. Compare the complete channel; never parse an ID.
    with closing(owner._db()) as db:
        replies = db.execute(
            "SELECT * FROM reply_connections WHERE 'binding:' || connection_id=?",
            (channel["binding_id"],),
        ).fetchall()
    for reply in replies:
        row = owner.get(reply["observation_id"])
        if row and channel == owner._observation_channel(
            "binding:" + reply["connection_id"], reply["conversation"]
        ):
            candidates.append(
                dict(
                    instance_id=row["instance_id"],
                    self_id=row["account_id"],
                    conversation_id=reply["conversation"],
                    actor_id=frame["actor_id"],
                )
            )
    for row in owner.rows():
        conversation = channel["channel_conversation_id"]
        if re.fullmatch(r"(group|private):[1-9][0-9]*", conversation) is None:
            continue
        actor = owner.actor(row, conversation)
        if actor and channel == owner.segment_channel(row, conversation, actor):
            candidates.append(
                dict(
                    instance_id=row["instance_id"],
                    self_id=row["account_id"],
                    conversation_id=conversation,
                    actor_id=frame["actor_id"],
                )
            )
    for slot in getattr(owner.p.bots, "slots", {}).values():
        if any(
            owner.p.sources.entries[key]["channel"] == channel for key in slot["input_entry_ids"]
        ):
            candidates.append(
                dict(
                    instance_id=slot["platform_id"],
                    self_id=slot["self_id"],
                    conversation_id=channel["channel_conversation_id"],
                    actor_id=frame["actor_id"],
                )
            )
    unique = {canonical(item): item for item in candidates}
    if len(unique) == 1:
        return next(iter(unique.values())), None
    if unique:
        return None, "ambiguous_frame"
    # No relation is a genuine external source only if no managed BOT could
    # own it. A known managed namespace with an unknown binding fails closed.
    bot_principal = (owner.p.bots.config or {}).get("principal")
    external = any(
        entry["channel"] == channel and entry["owner"] != bot_principal
        for entry in owner.p.sources.entries.values()
    )
    managed = bool(owner.catalog and owner.catalog.all()) and not external
    return None, "frame_unavailable" if managed else "not_managed"


def current(owner, frame):
    native, code = native_frame(owner, frame)
    result = dict(
        frame=frame,
        mode=None,
        actor_id=None,
        processing_enabled=code == "not_managed",
        policy_version=None,
        code=code,
    )
    if native is None:
        return result
    if re.fullmatch(r"(group|private):[1-9][0-9]*", native["conversation_id"]) is None:
        result["code"] = "frame_unavailable"
        return result
    rows = [
        row
        for row in owner.rows()
        if row["instance_id"] == native["instance_id"] and row["account_id"] == native["self_id"]
    ]
    if not rows:
        managed = (
            [
                row
                for row in owner.catalog.all()
                if row.get("instance_id") == native["instance_id"]
                and row.get("account_id") == native["self_id"]
            ]
            if owner.catalog
            else []
        )
        kind, conversation_id = native["conversation_id"].split(":", 1)
        candidates = [
            row
            for row in managed
            if row.get("kind") != "observation"
            and row.get("conversation") == {"kind": kind, "id": conversation_id}
        ]
        if len(candidates) == 1:
            row = candidates[0]
            actor = row["actor_id"]
            allowed = (
                row["enabled"]
                and not row["pending"]
                and row["state"] in {"ready", "degraded"}
                and native["actor_id"] in (None, actor)
                and owner._reply_role_active(actor)
            )
            result.update(
                mode="reply",
                actor_id=actor,
                policy_version=row["revision"],
                processing_enabled=bool(allowed),
                code=None
                if allowed
                else "policy_pending"
                if row["pending"]
                else "account_disabled",
            )
            return result
        result.update(
            processing_enabled=not managed, code="frame_unavailable" if managed else "not_managed"
        )
        return result
    if len(rows) != 1:
        result["code"] = "ambiguous_frame"
        return result
    row = rows[0]
    conversation = native["conversation_id"]
    actor = owner.actor(row, conversation)
    if conversation.startswith("group:"):
        mode = group_policy.effective(row["group_policy"], conversation)["mode"]
        version = group_policy.revision(row, conversation)
    else:
        verdict = owner.event_decision(
            row, dict(conversation_id=conversation, mentioned=False, text="")
        )
        mode = (
            "disabled"
            if not verdict["observe"]
            else "reply"
            if verdict["reply_permitted"]
            else "observe"
        )
        version = row["host_revision"]
    result.update(mode=mode, actor_id=actor, policy_version=version)
    if mode == "disabled":
        return result
    if row["pending"]:
        result["code"] = "policy_pending"
    elif not row["enabled"] or not row["read_enabled"] or row["state"] not in {"ready", "degraded"}:
        result["code"] = "account_disabled"
    elif actor is None:
        result["code"] = "actor_unbound"
    elif native["actor_id"] is not None and native["actor_id"] != actor:
        result["code"] = "actor_changed"
    elif not owner._reply_role_active(actor):
        result["code"] = "actor_unavailable"
    else:
        result["processing_enabled"] = True
    return result


def processing(owner, header, body):
    with owner.p.store.connect(write=True) as db:
        _, principal = owner.p.auth.authenticate(header, db, "observation.verify")
        require(
            principal["kind"] == "service" and principal["service"] in {"companion", "memory"},
            "forbidden",
            403,
        )
    require(
        isinstance(body, dict)
        and set(body) == {"schema_version", "request_id", "frames"}
        and type(body["schema_version"]) is int
        and body["schema_version"] == 1,
        "invalid_input",
        400,
    )
    owner.p.contracts.check("common#id", body["request_id"])
    frames = body["frames"]
    require(
        isinstance(frames, list)
        and 1 <= len(frames) <= 100
        and len({canonical(item) for item in frames}) == len(frames),
        "invalid_input",
        400,
    )
    for item in frames:
        require(
            isinstance(item, dict)
            and (
                set(item) == {"instance_id", "self_id", "conversation_id", "actor_id"}
                or set(item) == {"channel", "actor_id"}
            ),
            "invalid_input",
            400,
        )
        if item["actor_id"] is not None:
            owner.p.contracts.check("common#id", item["actor_id"])
        if "channel" in item:
            owner.p.contracts.check("common#channel_key", item["channel"])
            require(
                item["channel"]["namespace"] == "qq" and item["channel"]["thread_id"] is None,
                "invalid_input",
                400,
            )
            conversation = item["channel"]["channel_conversation_id"]
        else:
            owner.p.contracts.check("common#id", item["instance_id"])
            require(
                isinstance(item["self_id"], str) and re.fullmatch(r"[1-9][0-9]*", item["self_id"]),
                "invalid_input",
                400,
            )
            conversation = item["conversation_id"]
        if "channel" not in item:
            require(
                isinstance(conversation, str)
                and re.fullmatch(r"(group|private):[1-9][0-9]*", conversation),
                "invalid_input",
                400,
            )
    return dict(
        schema_version=1,
        request_id=body["request_id"],
        frames=[current(owner, frame) for frame in frames],
    )
