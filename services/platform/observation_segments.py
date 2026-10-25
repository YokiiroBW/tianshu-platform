"""Fresh, bounded observation authority for one actor's consolidation segment."""

from contextlib import closing

from .contracts import canonical, digest, loads, require


def _authority(row, conversation, actor):
    require(row is not None, "scope_changed", 409)
    require(not row["pending"], "dependency_unavailable", 503)
    require(row["read_enabled"], "scope_changed", 409)
    policy = row[conversation.split(":", 1)[0] + "_policy"]
    configured = policy["actor_id"]
    require(configured is not None and actor in (None, configured), "forbidden", 403)
    # Pausing observation or denying replies does not revoke the historical
    # archive. Only current read authority and the explicit actor binding apply.
    return {
        "owner_id": row["id"],
        "instance_id": row["instance_id"],
        "self_id": row["account_id"],
        "actor_id": configured,
        "epoch": row["archive_epoch"],
        "revision": row["revision"],
        "host_revision": row["host_revision"],
    }


def check_segment(owner, request):
    """Check all sources together; discovery never supplies an actor of its own."""
    contracts = owner.p.contracts
    contracts.check("memory-extraction#segment_observation_check_request", request)
    refs = [item["source_ref"] for item in request["sources"]]
    require(len(set(refs)) == len(refs), "invalid_input", 400)
    candidates = [
        row
        for row in owner.rows()
        if row["instance_id"] == request["instance_id"] and row["account_id"] == request["self_id"]
    ]
    require(len(candidates) == 1, "scope_changed", 409)
    row = candidates[0]
    conversation = request["conversation_id"]
    before = _authority(row, conversation, request["actor_id"])
    channel = owner.segment_channel(row, conversation, before["actor_id"])
    delivery_channel = owner.segment_delivery_channel(row, conversation, before["actor_id"])
    placeholders = ",".join("?" for _ in refs)
    with closing(owner._db()) as db:
        sources = {
            source["source_ref"]: dict(source)
            for source in db.execute(
                "SELECT * FROM sources WHERE source_ref IN (" + placeholders + ")", refs
            ).fetchall()
        }
    proofs = []
    for expected in request["sources"]:
        source = sources.get(expected["source_ref"])
        require(
            source is not None
            and source["connection_id"] == before["owner_id"]
            and source["instance_id"] == request["instance_id"]
            and source["self_id"] == request["self_id"]
            and source["conversation"] == conversation
            and source["digest"] == expected["source_digest"]
            and source["epoch"] == expected["scope_version"]
            and source["archive_epoch"] == expected["archive_epoch"] == before["epoch"],
            "scope_changed",
            409,
        )
        proofs.append(
            {
                **expected,
                "physical_key": {
                    "platform_id": source["instance_id"],
                    "self_id": source["self_id"],
                    "conversation_id": source["conversation"],
                    "account_id": source["author"],
                    "event_id": source["event_id"],
                    # record() accepts only revision 1 in the existing archive.
                    "revision": 1,
                },
            }
        )
    # Source rows are immutable after record(). A single SQL statement reads
    # the complete requested batch. The catalog's monotonic control revision
    # closes its separate owner read; no state is retained for another request.
    current = owner.get(row["id"])
    after = _authority(current, conversation, before["actor_id"])
    require(before == after, "scope_changed", 409)
    require(
        channel == owner.segment_channel(current, conversation, after["actor_id"]),
        "scope_changed",
        409,
    )
    response = {
        "valid": True,
        "actor_id": before["actor_id"],
        "instance_id": before["instance_id"],
        "self_id": before["self_id"],
        "conversation_id": conversation,
        "channel": channel,
        "delivery_channel": delivery_channel,
        "authority": {
            "owner_id": before["owner_id"],
            "epoch": before["epoch"],
            "revision": before["revision"],
            "digest": digest(
                {
                    "authority": before,
                    "channel": channel,
                    "delivery_channel": delivery_channel,
                    "sources": proofs,
                }
            ),
        },
        "sources": proofs,
    }
    contracts.check("memory-extraction#segment_observation_check_response", response)
    return response


def resolve_segment(owner, request):
    """Resolve only persisted native-event/admission relationships, never text."""
    contracts = owner.p.contracts
    contracts.check("memory-extraction#segment_observation_resolve_request", request)
    requested = request["sources"]
    require(len({canonical(item) for item in requested}) == len(requested), "invalid_input", 400)
    actor = request["actor_id"]
    require(all(item["scope"]["actor_id"] == actor for item in requested), "forbidden", 403)
    message_ids = list(
        dict.fromkeys(item["source"]["message_key"]["message_id"] for item in requested)
    )
    events = {}
    if owner.p.bots.config is not None:
        with closing(owner.p.bots._db()) as db:
            for event in db.execute(
                "SELECT * FROM events WHERE message_id IN ("
                + ",".join("?" for _ in message_ids)
                + ")",
                message_ids,
            ).fetchall():
                events.setdefault(event["message_id"], []).append(dict(event))
    connection_ids = list(
        dict.fromkeys(e["connection_id"] for values in events.values() for e in values)
    )
    with closing(owner._db()) as db:
        replies = (
            {
                row["connection_id"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM reply_connections WHERE connection_id IN ("
                    + ",".join("?" for _ in connection_ids)
                    + ")",
                    connection_ids,
                ).fetchall()
            }
            if connection_ids
            else {}
        )
    candidates = []
    for item in requested:
        key = item["source"]["message_key"]
        matches = events.get(key["message_id"], [])
        require(len(matches) <= 1, "dependency_unavailable", 503)
        event = matches[0] if matches else None
        reply = replies.get(event["connection_id"]) if event else None
        # Legacy/manual adapters have no durable observation-frame relation.
        # Their original Core source remains authoritative and is not guessed.
        if reply is None or key["revision"] != 1:
            candidates.append(None)
            continue
        require(
            reply["actor_id"] == actor
            and key["channel"]
            == owner._observation_channel(
                "binding:" + reply["connection_id"], reply["conversation"]
            ),
            "forbidden",
            403,
        )
        row = owner.get(reply["observation_id"])
        require(row is not None, "dependency_unavailable", 503)
        candidates.append((event, reply, row))
    receipts = [
        item["source"]["receipt_id"] for item, candidate in zip(requested, candidates) if candidate
    ]
    with owner.p.store.connect(write=True) as db:
        admissions = (
            {
                row["receipt_id"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM admission_history WHERE receipt_id IN ("
                    + ",".join("?" for _ in receipts)
                    + ")",
                    receipts,
                ).fetchall()
            }
            if receipts
            else {}
        )
    refs = []
    for item, candidate in zip(requested, candidates):
        if candidate is None:
            refs.append(None)
            continue
        event, reply, row = candidate
        saved = admissions.get(item["source"]["receipt_id"])
        require(saved is not None, "dependency_unavailable", 503)
        admission, historic = loads(saved["admission"]), loads(saved["entry_document"])
        account = {"namespace": "qq", "immutable_account_id": event["account_id"]}
        require(
            admission["source"] == item["source"]
            and admission["scope"] == item["scope"]
            and historic["actor_id"] == actor
            and historic["account"] == account
            and historic["channel"] == item["source"]["message_key"]["channel"]
            and loads(saved["account"]) == account,
            "forbidden",
            403,
        )
        refs.append(
            owner._source_ref(
                row["instance_id"],
                row["account_id"],
                reply["conversation"],
                event["account_id"],
                event["event_id"],
            )
        )
    actual_refs = list(dict.fromkeys(ref for ref in refs if ref is not None))
    with closing(owner._db()) as db:
        sources = (
            {
                row["source_ref"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM sources WHERE source_ref IN ("
                    + ",".join("?" for _ in actual_refs)
                    + ")",
                    actual_refs,
                ).fetchall()
            }
            if actual_refs
            else {}
        )
    results = []
    for item, ref in zip(requested, refs):
        observation = None
        if ref is not None:
            source = sources.get(ref)
            require(source is not None, "dependency_unavailable", 503)
            observation = dict(
                instance_id=source["instance_id"],
                self_id=source["self_id"],
                conversation_id=source["conversation"],
                source_ref=ref,
                source_digest=source["digest"],
                scope_version=source["epoch"],
                archive_epoch=source["archive_epoch"],
            )
        results.append({**item, "observation": observation})
    response = dict(valid=True, actor_id=actor, sources=results)
    contracts.check("memory-extraction#segment_observation_resolve_response", response)
    return response
