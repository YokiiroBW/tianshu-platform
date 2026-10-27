"""Durable schedules with bounded catch-up and explicit overlap semantics."""

import asyncio
from datetime import datetime
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..contracts import Fault, digest, require
from .schema import check, DOCUMENTS


def next_run(trigger, now):
    if trigger["kind"] == "interval":
        return now + trigger["seconds"]
    try:
        zone = ZoneInfo(trigger["timezone"])
    except ZoneInfoNotFoundError:
        raise Fault("config_invalid", 400) from None
    fields = trigger["cron"].split()
    require(len(fields) == 5, "invalid_input", 400)
    ranges = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    sets = []
    for field, (minimum, maximum) in zip(fields, ranges):
        values = set()
        for part in field.split(","):
            pieces = part.split("/")
            require(len(pieces) <= 2, "invalid_input", 400)
            step = int(pieces[1]) if len(pieces) == 2 else 1
            require(step > 0, "invalid_input", 400)
            limits = pieces[0].split("-")
            if pieces[0] == "*":
                start, end = minimum, maximum
            elif len(limits) == 2:
                start, end = map(int, limits)
            else:
                start = end = int(limits[0])
            require(minimum <= start <= end <= maximum, "invalid_input", 400)
            values.update(range(start, end + 1, step))
        sets.append(values)
    # One year is bounded and supports leap-day schedules without replaying old ticks.
    candidate = int(now // 60) * 60 + 60
    for _ in range(527040):
        date = datetime.fromtimestamp(candidate, zone)
        weekday = (date.weekday() + 1) % 7
        day_match = date.day in sets[2]
        week_match = weekday in sets[4] or weekday == 0 and 7 in sets[4]
        day_ok = (
            day_match and week_match
            if fields[2] == "*" or fields[4] == "*"
            else day_match or week_match
        )
        if date.minute in sets[0] and date.hour in sets[1] and date.month in sets[3] and day_ok:
            return candidate
        candidate += 60
    raise Fault("invalid_input", 400)


class Scheduler:
    def __init__(self, host):
        self.host = host

    async def manage(self, instance, operation, value):
        await self.host.ensure_active()
        repo = self.host.repository
        identifier = digest([instance, value["schedule_id"]])
        current = await self.host.local(repo.get, "schedules", identifier)
        require(
            value.get("expected_revision") == (current["revision"] if current else None),
            "revision_conflict",
            409,
        )
        if operation == "schedule.remove":
            require(current, "not_found", 404)
            document = {**current, "enabled": False, "revision": current["revision"] + 1}
        else:
            schema = next(
                item for item in DOCUMENTS if item["$id"].endswith("contributions.schema.json")
            )
            check(
                schema["properties"]["schedules"]["items"]["properties"]["default"],
                value["trigger"],
            )
            owner = await self.host.instance(instance)
            package = await self.host.package(owner["package_id"], owner["version"])
            action = next(
                (
                    item
                    for item in package["description"]["actions"]
                    if item["id"] == value["action"]
                ),
                None,
            )
            require(action, "not_found", 404)
            check(action["input_schema"], value["input"])
            document = {
                "id": identifier,
                "instance_id": instance,
                "schedule_id": value["schedule_id"],
                "action": value["action"],
                "input": value["input"],
                "trigger": value["trigger"],
                "revision": (current["revision"] if current else 0) + 1,
                "enabled": True,
                "next_run": next_run(value["trigger"], time.time()),
                "pending": False,
                "last_invocation": current.get("last_invocation") if current else None,
            }
        await self.host.local(
            repo.put, "schedules", document, expected=current["revision"] if current else None
        )
        return {"schedule_revision": document["revision"], "removed": not document["enabled"]}

    async def seed(self, instance, description):
        for item in description.get("schedules", []):
            current = await self.host.local(
                self.host.repository.get, "schedules", digest([instance, item["id"]])
            )
            if current is None:
                await self.manage(
                    instance,
                    "schedule.put",
                    {
                        "schedule_id": item["id"],
                        "action": item["action"],
                        "input": item["input"],
                        "trigger": item["default"],
                        "expected_revision": None,
                    },
                )

    async def tick(self, now=None):
        now = time.time() if now is None else now
        repo = self.host.repository
        for schedule in await self.host.local(repo.list, "schedules"):
            try:
                await self._tick_schedule(schedule, now)
            except (Fault, OSError, ValueError, KeyError):
                # Keep this schedule's durable due/receipt for its next retry,
                # while unrelated schedules still get their turn this tick.
                continue

    async def _tick_schedule(self, schedule, now):
        repo = self.host.repository
        if not schedule["enabled"] or schedule["next_run"] > now and not schedule["pending"]:
            return
        instance = await self.host.instance(schedule["instance_id"])
        if instance["desired_state"] != "enabled":
            return
        call = (
            await self.host.local(repo.call, schedule["last_invocation"])
            if schedule["last_invocation"]
            else None
        )
        busy = call and call["execution"] in {"accepted", "running"}
        due = schedule["next_run"]
        trigger = schedule["trigger"]
        if schedule["next_run"] <= now:
            schedule["next_run"] = next_run(trigger, now)
        if busy:
            schedule["pending"] = trigger["overlap"] == "queue_one"
        else:
            # skip ignores ticks missed while stopped; coalesce issues one current intent.
            tolerance = trigger.get("seconds", 60)
            if schedule["pending"] or trigger["misfire"] == "coalesce" or now - due < tolerance:
                result = await self.host.executions.invoke(
                    instance["id"],
                    schedule["action"],
                    schedule["input"],
                    "schedule:" + digest([schedule["id"], due, schedule["revision"]]),
                    "automation:" + instance["id"],
                )
                schedule["last_invocation"] = result["id"]
            schedule["pending"] = False
        await self.host.local(repo.put, "schedules", schedule)

    async def run(self):
        while True:
            try:
                await self.tick()
            except (Fault, OSError, ValueError, KeyError):
                # A failing schedule does not stop other plugin invocations or the web service.
                pass
            await asyncio.sleep(1)
