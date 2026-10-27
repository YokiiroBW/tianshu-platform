"""Re-read the observation, role and author owners at participation boundaries."""

from contextlib import closing

from ..bot_observation import decision
from ..contracts import canonical, digest, epoch, require, utc
from .. import group_policy


def configuration(platform, row, actor, host_generation, group_id):
    role = platform.role_runtime.get(actor)
    if role is not None:
        require(platform.role_runtime.active(actor), "scope_changed", 409)
        version = role["version"]
    else:
        registered = platform.bot_adapters.config.get("actors", [])
        require(any(item["id"] == actor for item in registered), "scope_changed", 409)
        version = 1
    generation = digest(
        [
            row["id"],
            group_policy.revision(row, group_id),
            row["archive_epoch"],
            group_policy.effective(row["group_policy"], group_id),
            role,
            platform.bot_adapters.config.get("actors", []),
        ]
    )
    return {
        "platform_generation": "observation:" + generation,
        "host_generation": host_generation,
        "role_version": version,
        "reply_policy_revision": group_policy.revision(row, group_id),
    }


def check(application, record):
    p = application.p
    candidate, event = record["document"], record["event"]
    application.protocol.check("candidate", candidate)
    now = epoch(utc(p.origins.clock()))
    require(
        epoch(candidate["created_at"]) <= now < epoch(candidate["expires_at"]), "scope_changed", 409
    )
    row = p.bot_observation.get(record["observation_id"])
    require(
        row is not None
        and row["enabled"]
        and row["read_enabled"]
        and not row["pending"]
        and row["state"] in {"ready", "degraded"},
        "scope_changed",
        409,
    )
    native = candidate["native"]
    require(
        row["instance_id"] == native["instance_id"]
        and row["account_id"] == native["self_id"]
        and {"nonebot": "nonebot2", "onebot": "nonebot2", "astrbot": "astrbot"}[row["adapter"]]
        == native["framework"]["name"]
        and p.bot_observation.actor(row, event["conversation_id"]) == candidate["actor_id"]
        and event["mentioned"] is False
        and event["content_state"] == "text"
        and event["conversation_id"] == "group:" + native["group_id"]
        and event["account_id"] == native["author_account_id"]
        and event["event_id"] == native["native_message_id"]
        and event["revision"] == native["message_revision"]
        and event["text"] == candidate["text"]
        and event["sent_at"] == candidate["sent_at"],
        "scope_changed",
        409,
    )
    verdict = decision(row["group_policy"], native["group_id"], False, "group", event["text"])
    require(
        verdict["proactive_permitted"]
        and not verdict["reply_triggered"]
        and p.bot_observation.same_capture_policy(row, event),
        "scope_changed",
        409,
    )
    require(
        configuration(
            p,
            row,
            candidate["actor_id"],
            candidate["configuration"]["host_generation"],
            native["group_id"],
        )
        == candidate["configuration"],
        "scope_changed",
        409,
    )
    proof = candidate["source"]
    with closing(p.bot_observation._db()) as db:
        source = db.execute(
            "SELECT * FROM sources WHERE source_ref=?", (proof["source_ref"],)
        ).fetchone()
    require(
        source is not None
        and source["connection_id"] == row["id"]
        and source["instance_id"] == native["instance_id"]
        and source["self_id"] == native["self_id"]
        and source["conversation"] == event["conversation_id"]
        and source["author"] == event["account_id"]
        and source["event_id"] == event["event_id"]
        and source["digest"] == proof["source_digest"] == digest(event)
        and source["epoch"] == proof["scope_version"] == event["scope_revision"]
        and source["archive_epoch"] == proof["archive_epoch"] == row["archive_epoch"],
        "scope_changed",
        409,
    )
    anchor = candidate["author_anchor"]
    require(
        anchor["account_id"] == native["author_account_id"]
        and anchor["group_id"] == native["group_id"],
        "forbidden",
        403,
    )
    return row


def anchor(platform, db, entry_id, entry, native):
    """This actor entry, account and current scope all come from original owners."""
    require(
        entry["account"] == {"namespace": "qq", "immutable_account_id": native["author_account_id"]}
        and entry["channel"]["channel_conversation_id"] == "group:" + native["group_id"],
        "forbidden",
        403,
    )
    scope = platform.origins.scope(db, entry)
    identity = db.execute(
        "SELECT person_id,version FROM identities WHERE account=?", (canonical(entry["account"]),)
    ).fetchone()
    resolved = (
        identity is not None
        and scope["person_id"] is not None
        and scope["conversation_id"] is not None
    )
    return {
        "stage": "resolved" if resolved else "registered",
        "account_id": native["author_account_id"],
        "group_id": native["group_id"],
        "entry_id": entry_id,
        "person_id": scope["person_id"] if resolved else None,
        "conversation_id": scope["conversation_id"] if resolved else None,
        "binding_version": identity["version"] if resolved else None,
    }
