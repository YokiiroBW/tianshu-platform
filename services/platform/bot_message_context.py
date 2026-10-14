"""Normalize bounded OneBot references within an already-authorized connection."""

import re
from contextlib import closing

from .contracts import require


def validate(value):
    require(
        isinstance(value, dict) and set(value) == {"reply_ids", "mentioned_accounts"},
        "invalid_input",
        400,
    )
    for key, limit, pattern in (
        ("reply_ids", 16, r"[A-Za-z0-9_.:-]{1,128}"),
        ("mentioned_accounts", 64, r"[1-9][0-9]*"),
    ):
        items = value[key]
        require(isinstance(items, list) and len(items) <= limit, "invalid_input", 400)
        require(
            all(type(item) is str and re.fullmatch(pattern, item) for item in items),
            "invalid_input",
            400,
        )
        require(len(set(items)) == len(items), "invalid_input", 400)
    return value


def source_fields(connect, connection_id, channel, context):
    value = validate(context)
    references = []
    with closing(connect()) as db:
        for native_id in value["reply_ids"]:
            rows = db.execute(
                "SELECT message_id FROM events WHERE connection_id=? AND event_id=? AND state='accepted' LIMIT 2",
                (connection_id, native_id),
            ).fetchall()
            # Unknown refs stay unresolved. Never look in another connection or fetch quoted content.
            target = rows[0][0] if len(rows) == 1 else native_id
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", target):
                target = "native:" + target[:121]
            references.append(dict(channel=channel, message_id=target, revision=1))
    return dict(
        reply_refs=references,
        mentioned_accounts=[
            dict(namespace="qq", immutable_account_id=account)
            for account in value["mentioned_accounts"]
        ],
    )
