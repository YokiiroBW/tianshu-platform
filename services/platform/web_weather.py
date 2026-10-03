"""QWeather location and current conditions, using the existing encrypted catalog."""

import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp

from .contracts import Fault, loads, require

PREFIX = "/api/web/weather/"
CACHE_SECONDS = 300


def host_url(value):
    require(isinstance(value, str) and len(value) <= 253, "invalid_input", 400)
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise Fault("invalid_weather_host", 400) from None
    require(
        parsed.scheme == "https"
        and re.fullmatch(r"[a-z0-9][a-z0-9-]*\.qweatherapi\.com", parsed.netloc)
        and not parsed.path
        and not parsed.query
        and not parsed.fragment,
        "invalid_weather_host",
        400,
    )
    return value


def location_view(row):
    require(isinstance(row, dict), "weather_invalid_response", 502)
    keys = {
        "id": "id",
        "name": "name",
        "adm1": "adm1",
        "adm2": "adm2",
        "country": "country",
        "tz": "tz",
        "utc_offset": "utcOffset",
    }
    result = {}
    for target, source in keys.items():
        value = row.get(source)
        require(
            isinstance(value, str) and len(value) <= 128 and (value == "" or value.isprintable()),
            "weather_invalid_response",
            502,
        )
        result[target] = value
    require(
        re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result["id"])
        and re.fullmatch(r"[A-Za-z0-9_+/-]{1,128}", result["tz"])
        and re.fullmatch(r"[+-]\d{2}:\d{2}", result["utc_offset"]),
        "weather_invalid_response",
        502,
    )
    try:
        latitude, longitude = float(row["lat"]), float(row["lon"])
        require(-90 <= latitude <= 90 and -180 <= longitude <= 180, "weather_invalid_response", 502)
    except (KeyError, TypeError, ValueError):
        raise Fault("weather_invalid_response", 502) from None
    result.update(latitude=latitude, longitude=longitude)
    return result


class WebWeather:
    def __init__(self, platform, console):
        self.platform, self.console = platform, console
        self.cache = {}
        self.lock = asyncio.Lock()

    @property
    def catalog(self):
        return self.console.external.catalog

    def can_manage(self):
        principal = self.platform.auth.principals.get(self.console.config["principal"], {})
        return principal.get("kind") == "operator" and "external.manage" in principal.get(
            "actions", []
        )

    def snapshot(self):
        if self.catalog is None:
            return 0, None
        revision, rows = self.catalog.snapshot()
        return revision, rows.get("weather")

    def state(self, actor, snapshot=None):
        revision, row = self.snapshot() if snapshot is None else snapshot
        value = row["value"] if row else {}
        location = value.get("locations", {}).get(actor)
        configured = bool(value.get("host") and row and row["credential"])
        return {
            "revision": revision,
            "can_manage": self.can_manage() and self.catalog is not None,
            "configured": configured,
            "credential_configured": bool(row and row["credential"]),
            "host": value.get("host", ""),
            "location": location,
            "server_time": time.time(),
            "code": "weather_store_unavailable"
            if self.catalog is None
            else "weather_not_configured"
            if not configured
            else "weather_location_missing"
            if location is None
            else "ready",
        }

    async def authorized(self, actor, session):
        require(isinstance(actor, str) and 1 <= len(actor) <= 256, "invalid_input", 400)
        await self.console.life.route("/api/web/life/today", {"actor_id": actor}, session)

    async def request(self, row, path, query):
        require(row and row["credential"], "weather_not_configured", 503)
        url = host_url(row["value"]["host"]) + path
        try:
            async with asyncio.timeout(8):
                async with aiohttp.ClientSession(trust_env=False) as client:
                    async with client.get(
                        url,
                        params={**query, "lang": "zh"},
                        headers={"X-QW-Api-Key": row["credential"]},
                        allow_redirects=False,
                    ) as response:
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(8192):
                            raw.extend(chunk)
                            require(len(raw) <= 131072, "weather_invalid_response", 502)
                        if response.status in {401, 403}:
                            raise Fault("weather_unauthorized", 503)
                        require(response.status == 200, "weather_unavailable", 503)
                        try:
                            answer = loads(bytes(raw))
                        except Fault:
                            raise Fault("weather_invalid_response", 502) from None
                        require(isinstance(answer, dict), "weather_invalid_response", 502)
                        if answer.get("code") == "404":
                            raise Fault("weather_location_not_found", 404)
                        if answer.get("code") in {"401", "402", "403"}:
                            raise Fault("weather_unauthorized", 503)
                        if path.startswith("/geo/"):
                            require(answer.get("code") == "200", "weather_unavailable", 503)
                        return answer
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise Fault("weather_unavailable", 503) from None

    async def lookup(self, row, query):
        answer = await self.request(row, "/geo/v2/city/lookup", {"location": query, "number": "10"})
        items = answer.get("location")
        require(isinstance(items, list) and len(items) <= 20, "weather_invalid_response", 502)
        return [location_view(item) for item in items]

    async def route(self, path, body, session):
        name = path.removeprefix(PREFIX)
        shapes = {
            "state": {"actor_id"},
            "current": {"actor_id"},
            "locations": {"actor_id", "query"},
            "location": {"actor_id", "location_id", "expected_revision", "client_id"},
            "configure": {"actor_id", "host", "credential", "expected_revision", "client_id"},
        }
        require(name in shapes, "not_found", 404)
        require(isinstance(body, dict) and set(body) == shapes[name], "invalid_input", 400)
        actor = body["actor_id"]
        await self.authorized(actor, session)
        if name == "state":
            return await self.platform.local_work.run(self.state, actor)
        revision, row = await self.platform.local_work.run(self.snapshot)
        if name in {"configure", "location", "locations"}:
            require(self.can_manage(), "external_manage_required", 403)
            require(self.catalog is not None, "weather_store_unavailable", 503)
        if name == "locations":
            query = body["query"]
            require(isinstance(query, str) and 1 <= len(query.strip()) <= 100, "invalid_input", 400)
            try:
                items = await self.lookup(row, query.strip())
            except Fault as exc:
                if exc.code != "weather_location_not_found":
                    raise
                items = []
            await self.authorized(actor, session)
            return {"items": items}
        if name in {"configure", "location"}:
            require(
                type(body["expected_revision"]) is int and body["expected_revision"] == revision,
                "revision_conflict",
                409,
            )
            value = dict(row["value"]) if row else {"locations": {}}
            credential = {"action": "keep"}
            if name == "configure":
                value["host"] = host_url(body["host"])
                credential = body["credential"]
                require(
                    isinstance(credential, dict)
                    and credential.get("action") in {"keep", "replace", "clear"},
                    "invalid_input",
                    400,
                )
                require(
                    set(credential)
                    == ({"action", "value"} if credential["action"] == "replace" else {"action"}),
                    "invalid_input",
                    400,
                )
                if credential["action"] == "replace":
                    secret = credential["value"]
                    require(
                        isinstance(secret, str)
                        and 8 <= len(secret) <= 4096
                        and all(33 <= ord(char) <= 126 for char in secret),
                        "invalid_input",
                        400,
                    )
            else:
                location_id = body["location_id"]
                require(
                    isinstance(location_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", location_id),
                    "invalid_input",
                    400,
                )
                items = await self.lookup(row, location_id)
                selected = next((item for item in items if item["id"] == location_id), None)
                require(selected is not None, "weather_location_not_found", 404)
                value["locations"] = {**value.get("locations", {}), actor: selected}
            await self.authorized(actor, session)
            require(self.can_manage(), "external_manage_required", 403)
            await self.platform.local_work.run(
                self.catalog.save,
                "weather",
                value,
                credential,
                {"action": "clear"},
                body["expected_revision"],
                body["client_id"],
            )
            self.cache.clear()
            return await self.platform.local_work.run(self.state, actor)
        state = await self.platform.local_work.run(self.state, actor, (revision, row))
        if state["code"] != "ready":
            return {**state, "weather": None, "fetched_at": None, "stale": False}
        async with self.lock:
            result = await self.current(row, state)
        await self.authorized(actor, session)
        latest, _ = await self.platform.local_work.run(self.snapshot)
        require(latest == revision, "revision_conflict", 409)
        return result

    async def current(self, row, state):
        key = (row["value"]["connection_revision"], state["location"]["id"])
        now = time.monotonic()
        cached = self.cache.get(key)
        if cached and now < cached["retry_after"]:
            return {**state, **cached["result"]}
        try:
            location = state["location"]
            path = f"/weather/v1/current/{location['latitude']:.2f}/{location['longitude']:.2f}"
            answer = await self.request(row, path, {})
            weather = {"observed_at": None}
            for target, field in {"temp": "temperature", "feels_like": "feelsLike"}.items():
                item = answer.get(field)
                require(
                    isinstance(item, dict)
                    and item.get("unit") == "°C"
                    and type(item.get("value")) in {int, float}
                    and -150 <= item["value"] <= 100,
                    "weather_invalid_response",
                    502,
                )
                weather[target] = f"{item['value']:g}"
            condition, wind = answer.get("condition"), answer.get("wind")
            require(
                isinstance(condition, dict) and isinstance(wind, dict),
                "weather_invalid_response",
                502,
            )
            for target, field in {"text": "text", "icon": "code"}.items():
                item = condition.get(field)
                require(
                    isinstance(item, str) and 1 <= len(item) <= 100 and item.isprintable(),
                    "weather_invalid_response",
                    502,
                )
                weather[target] = item
            require(
                type(wind.get("scale")) in {int, float} and 0 <= wind["scale"] <= 17,
                "weather_invalid_response",
                502,
            )
            weather["wind_scale"] = f"{wind['scale']:g}"
            metadata = answer.get("metadata")
            require(isinstance(metadata, dict), "weather_invalid_response", 502)
            attributions = metadata.get("attributions")
            require(
                isinstance(attributions, list)
                and len(attributions) <= 20
                and all(
                    isinstance(item, str) and 1 <= len(item) <= 2048 and item.isprintable()
                    for item in attributions
                ),
                "weather_invalid_response",
                502,
            )
            weather["attributions"] = attributions
            result = {
                "weather": weather,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "stale": False,
                "code": "ready",
            }
            retry = CACHE_SECONDS
        except Fault as exc:
            previous = cached["result"] if cached else {}
            result = {
                "weather": previous.get("weather"),
                "fetched_at": previous.get("fetched_at"),
                "stale": previous.get("weather") is not None,
                "code": exc.code,
            }
            retry = 60
        if len(self.cache) >= 128 and key not in self.cache:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = {"result": result, "retry_after": now + retry}
        return {**state, **result}
