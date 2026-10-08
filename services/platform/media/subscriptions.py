"""Subscription configuration and complete membership scans over the existing rule engine."""

import asyncio
import uuid
import time
from dataclasses import asdict

from ..contracts import Fault, canonical, digest, require
from .config import quality, source, text
from .metadata import normalize_bilibili
from .rules import RuleValidationError, parse_policy


def job_payload(
    resolved,
    cids,
    account_id,
    target_id,
    requested_quality,
    subscription_id=None,
    rule_evidence=None,
):
    snapshot = resolved["snapshot"]
    selected = [part["cid"] for part in snapshot["parts"] if part["cid"] in cids]
    require(
        selected and len(selected) <= 128 and len(selected) == len(set(cids)), "invalid_input", 400
    )
    creator = snapshot["creators"][0]["name"] if snapshot["creators"] else ""
    return {
        "subscription_id": subscription_id,
        "media_key": f"bilibili:video:{snapshot['bvid']}:cid:{selected[0]}",
        "bvid": snapshot["bvid"],
        "cid": selected[0],
        "selected_cids": selected,
        "layout": "multipart" if subscription_id or len(snapshot["parts"]) > 1 else "single",
        "title": snapshot.get("title") or "",
        "creator": creator,
        "account_id": account_id,
        "target_id": target_id,
        "quality": requested_quality,
        "snapshot": snapshot,
        "cover_url": resolved["video"]["cover_url"],
        "overrides": {},
        "rule_evidence": rule_evidence,
    }


class Subscriptions:
    def __init__(self, media):
        self.media = media
        self.locks = {}

    def save(self, body, db):
        require(
            set(body)
            == {
                "subscription_id",
                "expected_revision",
                "label",
                "source",
                "account_id",
                "target_id",
                "initial_sync",
                "quality",
                "rules",
                "interval_seconds",
                "client_id",
            },
            "invalid_input",
            400,
        )
        require(
            type(body["expected_revision"]) is int and body["expected_revision"] >= 0,
            "invalid_input",
            400,
        )
        identity = body["subscription_id"] or str(uuid.uuid4())
        require(isinstance(identity, str) and len(identity) <= 64, "invalid_input", 400)
        require(body["target_id"] in self.media.config["targets"], "target_not_configured", 409)
        if body["account_id"] is not None:
            account = self.media.repo._document(db, "account", body["account_id"])
            require(account and account["state"] == "ready", "auth_required", 409)
        try:
            parse_policy(body["rules"])
        except RuleValidationError:
            raise Fault("invalid_rules", 400) from None
        require(body["initial_sync"] in {"future_only", "history"}, "invalid_input", 400)
        require(
            type(body["interval_seconds"]) is int and 60 <= body["interval_seconds"] <= 604800,
            "invalid_input",
            400,
        )
        current = self.media.repo._document(db, "subscription", identity)
        source_value = source(body["source"])
        if current is not None:
            require(
                current["source"] == source_value
                and current["initial_sync"] == body["initial_sync"],
                "subscription_source_immutable",
                409,
            )
        result = {
            "subscription_id": identity,
            "label": text(body["label"], 128),
            "source": source_value,
            "account_id": body["account_id"],
            "target_id": body["target_id"],
            "initial_sync": body["initial_sync"],
            "quality": quality(body["quality"]),
            "rules": body["rules"],
            "interval_seconds": body["interval_seconds"],
            "state": "active",
            "baseline_ready": current["baseline_ready"] if current else False,
            "last_scan_at": current["last_scan_at"] if current else None,
            "next_scan_at": self.media.repo.clock(),
            "code": "ready" if current else "baseline_pending",
            "counts": current["counts"] if current else {"members": 0, "queued": 0},
            "quality_reassessment": bool(
                current
                and (current.get("quality_reassessment") or current["quality"] != body["quality"])
            ),
        }
        return self.media.repo.put(
            "subscription", identity, result, body["expected_revision"], db=db
        )

    def control(self, body, db):
        require(
            set(body) == {"subscription_id", "expected_revision", "action", "client_id"},
            "invalid_input",
            400,
        )
        require(body["action"] in {"pause", "resume"}, "invalid_input", 400)
        current = self.media.repo._document(db, "subscription", body["subscription_id"])
        require(current is not None, "not_found", 404)
        if body["action"] == "resume" and current["account_id"] is not None:
            account = self.media.repo._document(db, "account", current["account_id"])
            require(account and account["state"] == "ready", "auth_required", 409)
        return self.media.repo.put(
            "subscription",
            current["subscription_id"],
            {
                **current,
                "state": "paused" if body["action"] == "pause" else "active",
                "code": "user_paused" if body["action"] == "pause" else "ready",
                "next_scan_at": self.media.repo.clock(),
            },
            body["expected_revision"],
            db=db,
        )

    async def scan(self, identity, *, recompute=False):
        lock = self.locks.setdefault(identity, asyncio.Lock())
        require(not lock.locked(), "scan_in_progress", 409)
        async with lock:
            subscription = await self.media.local(self.media.repo.get, "subscription", identity)
            require(subscription is not None, "not_found", 404)
            require(subscription["state"] == "active", "subscription_paused", 409)
            owner = str(uuid.uuid4())
            generation = await self.media.local(self.media.repo.scan_claim, identity, owner)
            current_task = asyncio.current_task()

            async def pulse():
                while True:
                    await asyncio.sleep(5)
                    try:
                        await self.media.local(
                            self.media.repo.scan_heartbeat, identity, owner, generation
                        )
                    except Fault:
                        current_task.cancel()
                        return

            heartbeat = asyncio.create_task(pulse())
            try:
                deadline = time.monotonic() + self.media.config["scan_timeout_seconds"]
                cookie = await self.media.local(
                    self.media.accounts.cookie, subscription["account_id"]
                )
                account = (
                    await self.media.local(
                        self.media.repo.get, "account", subscription["account_id"]
                    )
                    if subscription["account_id"]
                    else None
                )
                members = await asyncio.wait_for(
                    self.media.connector.scan(
                        subscription["source"],
                        cookie,
                        page_limit=self.media.config["scan_page_limit"],
                        item_limit=self.media.config["scan_item_limit"],
                    ),
                    self.media.config["scan_timeout_seconds"],
                )
                old = await self.media.local(self.media.repo.members, identity)
                unseen = (
                    members
                    if recompute
                    or (
                        not subscription["baseline_ready"]
                        and subscription["initial_sync"] == "history"
                    )
                    else members - old
                    if subscription["baseline_ready"]
                    else set()
                )
                old_parts = await self.media.local(self.media.repo.member_parts, identity)
                jobs, part_members = [], {}
                for bvid in sorted(members):
                    require(time.monotonic() < deadline, "source_budget_exceeded", 503)
                    resolved = await self.media.connector.resolve(bvid, cookie, with_formats=False)
                    current_cids = [part["cid"] for part in resolved["snapshot"]["parts"]]
                    part_members[bvid] = current_cids
                    await self.media.local(self.media.repo.episode_numbers, bvid, current_cids)
                    selected = (
                        current_cids
                        if bvid in unseen
                        else [cid for cid in current_cids if cid not in old_parts.get(bvid, set())]
                        if subscription["baseline_ready"]
                        else []
                    )
                    if subscription.get("quality_reassessment"):
                        head = await self.media.local(
                            self.media.repo.get,
                            "target_head",
                            subscription["target_id"] + ":" + bvid,
                        )
                        owned = (
                            await self.media.local(self.media.repo.job, head["job_id"])
                            if head
                            else None
                        )
                        if owned and owned["quality"] != subscription["quality"]:
                            require(
                                set(owned["selected_cids"]) <= set(current_cids),
                                "source_cid_removed",
                                409,
                            )
                            selected = list(dict.fromkeys(selected + owned["selected_cids"]))
                    if not selected:
                        continue
                    metadata = normalize_bilibili(resolved["snapshot"])
                    decision = await self.media.evaluator.evaluate(
                        metadata,
                        subscription["rules"],
                        accessible=True,
                        quality_satisfied=False,
                        snapshot_revision=digest(canonical(resolved["snapshot"])),
                    )
                    if decision.requires_rule_attention:
                        raise Fault("rule_evaluation_failed", 409)
                    if decision.automatic_enqueue_allowed:
                        jobs.append(
                            job_payload(
                                resolved,
                                selected,
                                subscription["account_id"],
                                subscription["target_id"],
                                subscription["quality"],
                                identity,
                                asdict(decision),
                            )
                        )
                if account is not None:
                    current = await self.media.local(
                        self.media.repo.get, "account", account["account_id"]
                    )
                    require(
                        current["revision"] == account["revision"] and current["state"] == "ready",
                        "account_changed",
                        409,
                    )
                require(time.monotonic() < deadline, "source_budget_exceeded", 503)
                result, queued = await self.media.local(
                    self.media.repo.complete_scan,
                    subscription,
                    members,
                    jobs,
                    owner,
                    generation,
                    part_members,
                )
                self.media.wake.set()
                return {"subscription": result, "queued": queued}
            except (Fault, TimeoutError) as error:
                if isinstance(error, TimeoutError):
                    error = Fault("source_budget_exceeded", 503)
                current = await self.media.local(self.media.repo.get, "subscription", identity)
                if current is not None and current["revision"] == subscription["revision"]:
                    state = (
                        "auth_required"
                        if error.code in {"auth_required", "account_unavailable"}
                        else "rule_error"
                        if error.code in {"rule_evaluation_failed", "invalid_rules"}
                        else current["state"]
                    )
                    await self.media.local(
                        self.media.repo.put,
                        "subscription",
                        identity,
                        {
                            **current,
                            "state": state,
                            "code": error.code,
                            "next_scan_at": self.media.repo.clock()
                            + max(300, current["interval_seconds"]),
                        },
                        current["revision"],
                    )
                raise error
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self.media.local(self.media.repo.scan_release, identity, owner, generation)

    async def pump(self):
        while True:
            subscriptions = await self.media.local(self.media.repo.list, "subscription")
            for subscription in subscriptions:
                if (
                    subscription["state"] == "active"
                    and subscription["next_scan_at"] <= self.media.repo.clock()
                ):
                    try:
                        await self.scan(subscription["subscription_id"])
                    except Fault:
                        pass
            await asyncio.sleep(10)
