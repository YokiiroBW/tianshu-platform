"""Real plugin workers and loopback HTTP through the existing host authorization hooks."""

import asyncio
import json
import unittest

from aiohttp import web

from services.platform.content_discovery import Discovery
from services.platform.contracts import Fault, require
from services.platform.plugins import PluginManager
from services.platform.plugins.manager import AVAILABLE
from test_content_discovery import CatalogFixture


class DiscoveryPluginTests(CatalogFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare_catalog()
        self.requests = []
        self.status = 200
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

        async def search(request):
            self.requests.append(
                {"query": dict(request.query), "auth": request.headers.get("Authorization")}
            )
            self.entered.set()
            await self.release.wait()
            return web.json_response(
                {
                    "results": [
                        {
                            "title": "Interesting book",
                            "url": "https://example.org/article",
                            "content": "Actual search snippet",
                        }
                    ]
                },
                status=self.status,
            )

        app = web.Application()
        app.router.add_get("/search", search)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.url = "http://127.0.0.1:" + str(self.runner.addresses[0][1])
        self.addAsyncCleanup(self.runner.cleanup)
        self.host = PluginManager(self.settings, content_discovery=self.discovery)
        self.addAsyncCleanup(self.close_host)
        await self.host.start()
        operation = await self.host.install(
            {"type": "builtin", "id": "content-discovery"}, "install"
        )
        for _ in range(400):
            operation = self.host.repository.get("operations", operation["id"])
            if operation["execution"] not in {"accepted", "running"}:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(operation["execution"], "succeeded", operation)
        self.instance = await self.host.create(operation["package_id"], "Discover", "instance")
        self.instance = await self.host.configure(
            self.instance["id"],
            {"base_url": self.url, "include_books": True},
            {"api_key": "synthetic-search-key"},
            self.instance["revision"],
        )
        self.instance = await self.host.state(self.instance["id"], True, self.instance["revision"])

    async def close_host(self):
        self.release.set()
        await self.host.close()

    async def settled(self, identifier):
        for _ in range(500):
            document = self.host.repository.call(identifier)
            if document["execution"] not in {"accepted", "running"}:
                return self.host.executions.public(document)
            await asyncio.sleep(0.01)
        self.fail("Plugin invocation did not settle")

    async def invoke(
        self, value=None, intent="one", principal="actor-capabilities:actor:a", authorize=None
    ):
        call = await self.host.executions.invoke(
            self.instance["id"],
            "discover",
            value or {"query": "book"},
            intent,
            principal,
            authorize=authorize,
        )
        return await self.settled(call["id"])

    async def test_actual_worker_search_vault_transport_and_receipt_replay_after_restart(self):
        self.book("a" * 24, "Interesting book")
        value = await self.invoke()
        self.assertEqual(value["execution"], "succeeded", value)
        self.assertEqual(value["effect"], "none")
        self.assertEqual(len(value["result"]["candidates"]), 2)
        self.assertEqual(
            self.requests[0],
            {
                "query": {"q": "book", "format": "json", "pageno": "1"},
                "auth": "Bearer synthetic-search-key",
            },
        )
        self.assertNotIn("synthetic-search-key", json.dumps(value))
        again = await self.invoke()
        self.assertEqual(again["id"], value["id"])
        self.assertEqual(len(self.requests), 1)
        await self.host.close()
        self.host = PluginManager(self.settings, content_discovery=self.discovery)
        await self.host.start()
        self.assertEqual((await self.invoke())["id"], value["id"])
        self.assertEqual(len(self.requests), 1)
        other = await self.invoke(principal="actor-capabilities:actor:b")
        self.assertNotEqual(other["id"], value["id"])
        self.assertEqual(len(self.requests), 2)

    async def test_http_403_keeps_other_sources_and_no_reading_state_is_written(self):
        self.book("a" * 24, "Interesting book")
        self.status = 403
        result = await self.invoke()
        self.assertEqual(result["execution"], "succeeded", result)
        self.assertEqual([r["source"] for r in result["result"]["candidates"]], ["books"])
        self.assertEqual(result["result"]["sources"][0]["state"], "unavailable")
        with self.host.repository.transaction(False) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM plugin_state").fetchone()[0], 0)

    async def test_no_provider_keeps_original_five_ports_and_blocks_real_handler(self):
        await self.host.close()
        self.host = PluginManager(self.settings)
        await self.host.start()
        self.assertEqual(self.host.available_capabilities, AVAILABLE)
        overview = await self.host.view()
        self.assertNotIn("content.discovery", overview["capabilities"])
        instance = await self.host.projection(await self.host.instance(self.instance["id"]))
        self.assertEqual(instance["state"], "blocked")
        self.assertFalse(instance["contributions"]["actions"][0]["availability"]["available"])
        with self.assertRaises(Fault):
            await self.invoke()
        self.assertEqual(self.requests, [])

    async def test_revocation_during_http_read_withholds_all_candidate_results(self):
        self.release.clear()
        allowed = True

        def authorize():
            require(allowed, "forbidden", 403)
            return True

        task = asyncio.create_task(self.invoke(authorize=authorize))
        await asyncio.wait_for(self.entered.wait(), 5)
        allowed = False
        self.release.set()
        result = await task
        self.assertEqual(result["execution"], "failed")
        self.assertIsNone(result["result"])

    async def test_configuration_revision_changed_during_media_read_withholds_results(self):
        entered, release = asyncio.Event(), asyncio.Event()
        inner = self.discovery

        class Pending:
            async def discover(self, config, value, *, request):
                answer = await inner.discover(config, value, request=request)
                entered.set()
                await release.wait()
                return answer

        self.host.content_discovery = Pending()
        task = asyncio.create_task(self.invoke())
        await asyncio.wait_for(entered.wait(), 5)
        row = self.host.repository.get("instances", self.instance["id"])
        self.host.repository.put(
            "instances", {**row, "revision": row["revision"] + 1}, expected=row["revision"]
        )
        release.set()
        result = await task
        self.assertEqual(result["execution"], "failed")
        self.assertIsNone(result["result"])

    async def test_model_cannot_choose_endpoint_account_scope_or_identity(self):
        for key in ("base_url", "account_id", "music_scopes", "actor_id", "path"):
            with self.assertRaises(Fault):
                await self.invoke({"query": "book", key: "caller-controlled"}, intent=key)
        self.assertEqual(self.requests, [])

    async def test_absent_search_and_absent_media_owner_are_reported_honestly(self):
        self.host.content_discovery = Discovery()
        self.instance = await self.host.configure(
            self.instance["id"], {}, {}, self.instance["revision"]
        )
        result = await self.invoke()
        self.assertEqual(result["execution"], "succeeded", result)
        self.assertEqual(result["result"]["candidates"], [])
        self.assertTrue(all(s["state"] == "not_configured" for s in result["result"]["sources"]))
