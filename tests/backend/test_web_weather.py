"""Synthetic QWeather contract, authorization, encrypted persistence and cache regression."""

import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.platform.contracts import Fault
from services.platform.external_catalog import ExternalCatalog
from services.platform.web_weather import PREFIX, WebWeather, host_url, location_view

CITY = {
    "id": "101010100",
    "name": "北京",
    "adm1": "北京市",
    "adm2": "北京",
    "country": "中国",
    "tz": "Asia/Shanghai",
    "utcOffset": "+08:00",
    "lat": "39.90498",
    "lon": "116.40528",
}
CURRENT = {
    "temperature": {"value": 22.3, "unit": "°C"},
    "feelsLike": {"value": 21, "unit": "°C"},
    "condition": {"text": "多云", "code": "101"},
    "wind": {"scale": 2},
    "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
}
KEY = "synthetic-qweather-key-never-real"


async def run(function, *args):
    return function(*args)


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "external"
        self.catalog = ExternalCatalog(self.directory, create=True)
        self.platform = SimpleNamespace(
            auth=SimpleNamespace(
                principals={
                    "admin": {"kind": "operator", "actions": ["external.manage", "life.read"]}
                }
            ),
            local_work=SimpleNamespace(run=run),
        )
        self.console = SimpleNamespace(
            config={"principal": "admin"},
            external=SimpleNamespace(catalog=self.catalog),
            life=SimpleNamespace(route=AsyncMock(return_value={})),
        )
        self.weather = WebWeather(self.platform, self.console)
        self.session = {}

    async def call(self, name, **body):
        return await self.weather.route(
            PREFIX + name, {"actor_id": "actor:one", **body}, self.session
        )

    async def configure(self, **overrides):
        return await self.call(
            "configure",
            **{
                "host": "example.qweatherapi.com",
                "credential": {"action": "replace", "value": KEY},
                "expected_revision": self.catalog.snapshot()[0],
                "client_id": str(uuid.uuid4()),
                **overrides,
            },
        )

    async def locate(self):
        with patch.object(
            self.weather, "request", AsyncMock(return_value={"code": "200", "location": [CITY]})
        ):
            return await self.call(
                "location",
                location_id=CITY["id"],
                expected_revision=self.catalog.snapshot()[0],
                client_id=str(uuid.uuid4()),
            )

    async def ready(self):
        await self.configure()
        await self.locate()

    async def test_configuration_secret_is_encrypted_and_survives_reopen(self):
        initial = await self.call("state")
        self.assertEqual(initial["code"], "weather_not_configured")
        self.assertLess(abs(initial["server_time"] - time.time()), 5)
        result = await self.configure()
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(KEY.encode(), self.catalog.database.read_bytes())
        reopened = ExternalCatalog(self.directory)
        self.assertEqual(reopened.snapshot()[1]["weather"]["credential"], KEY)
        self.assertEqual(result["code"], "weather_location_missing")

    async def test_location_is_confirmed_by_geo_and_per_actor(self):
        await self.configure()
        result = await self.locate()
        self.assertEqual(result["location"]["tz"], "Asia/Shanghai")
        self.assertEqual(result["location"]["latitude"], 39.90498)
        self.assertIsNone(self.weather.state("actor:other")["location"])
        self.console.life.route.assert_awaited_with(
            "/api/web/life/today", {"actor_id": "actor:one"}, self.session
        )

    async def test_revoked_life_access_prevents_read_and_write(self):
        self.console.life.route.side_effect = Fault("upstream_forbidden", 403)
        with self.assertRaisesRegex(Fault, "upstream_forbidden"):
            await self.configure()
        with self.assertRaisesRegex(Fault, "upstream_forbidden"):
            await self.call("current")
        self.assertEqual(self.catalog.snapshot()[0], 0)

    async def test_management_denied_but_authorized_weather_reads_remain(self):
        await self.ready()
        self.platform.auth.principals["admin"]["actions"].remove("external.manage")
        self.assertFalse((await self.call("state"))["can_manage"])
        with self.assertRaisesRegex(Fault, "external_manage_required"):
            await self.configure()
        with patch.object(self.weather, "request", AsyncMock(return_value=CURRENT)):
            self.assertEqual((await self.call("current"))["weather"]["temp"], "22.3")

    async def test_cache_and_failure_keeps_old_conditions_as_stale(self):
        await self.ready()
        with patch.object(self.weather, "request", AsyncMock(return_value=CURRENT)) as request:
            first = await self.call("current")
            second = await self.call("current")
            self.assertEqual(request.await_count, 1)
            self.assertEqual(first["weather"], second["weather"])
            self.assertIsNone(first["weather"]["observed_at"])
            self.assertEqual(request.call_args.args[1], "/weather/v1/current/39.90/116.41")
        for entry in self.weather.cache.values():
            entry["retry_after"] = 0
        with patch.object(
            self.weather, "request", AsyncMock(side_effect=Fault("weather_unavailable", 503))
        ):
            failed = await self.call("current")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["weather"], first["weather"])
        self.assertEqual(failed["fetched_at"], first["fetched_at"])
        self.assertEqual(failed["code"], "weather_unavailable")

    async def test_malformed_weather_does_not_display_success(self):
        await self.ready()
        with patch.object(self.weather, "request", AsyncMock(return_value={})):
            result = await self.call("current")
        self.assertIsNone(result["weather"])
        self.assertEqual(result["code"], "weather_invalid_response")

    async def test_clear_credentials_invalidate_cached_weather(self):
        await self.ready()
        with patch.object(self.weather, "request", AsyncMock(return_value=CURRENT)):
            await self.call("current")
        await self.configure(credential={"action": "clear"})
        result = await self.call("current")
        self.assertEqual(result["code"], "weather_not_configured")
        self.assertIsNone(result["weather"])
        self.assertEqual(self.weather.cache, {})

    async def test_revision_conflict_preserves_original_credentials(self):
        await self.configure()
        with self.assertRaisesRegex(Fault, "revision_conflict"):
            await self.configure(expected_revision=0, credential={"action": "clear"})
        self.assertEqual(self.catalog.snapshot()[1]["weather"]["credential"], KEY)

    async def test_lookup_empty_results(self):
        await self.configure()
        with patch.object(
            self.weather, "request", AsyncMock(side_effect=Fault("weather_location_not_found", 404))
        ):
            self.assertEqual(await self.call("locations", query="不存在的位置"), {"items": []})

    async def test_transport_credential_in_header_and_redirect_not_followed(self):
        await self.configure()
        row = self.catalog.snapshot()[1]["weather"]

        class Content:
            async def iter_chunked(self, size):
                yield json.dumps({"code": "200", "location": [CITY]}).encode()

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Client(Response):
            def get(client, url, **kwargs):
                self.assertNotIn(KEY, url)
                self.assertNotIn(KEY, json.dumps(kwargs["params"]))
                self.assertEqual(kwargs["headers"], {"X-QW-Api-Key": KEY})
                self.assertFalse(kwargs["allow_redirects"])
                return Response()

        with patch("services.platform.web_weather.aiohttp.ClientSession", return_value=Client()):
            self.assertEqual((await self.weather.lookup(row, "北京"))[0]["id"], CITY["id"])
            Response.status = 302
            with self.assertRaisesRegex(Fault, "weather_unavailable"):
                await self.weather.lookup(row, "北京")

    def test_only_official_https_hosts_and_valid_coordinates(self):
        for host in [
            "http://example.qweatherapi.com",
            "example.qweatherapi.com.evil.test",
            "https://evil@example.qweatherapi.com",
            "https://example.qweatherapi.com:443",
            "https://example.qweatherapi.com/a",
            "https://[bad",
            "localhost",
        ]:
            with self.subTest(host=host), self.assertRaises(Fault):
                host_url(host)
        with self.assertRaises(Fault):
            location_view({**CITY, "lat": "nan"})


if __name__ == "__main__":
    unittest.main()
