import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

from services.platform.contracts import Fault
from services.platform.external_catalog import ExternalCatalog
from services.platform.memory_retrieval import MemoryRetrieval, PREFIX
from services.platform.provider_management import ProviderManagement


class RetrievalGatewayResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.document = {"vectors": [[0.1234567890123456] * 4096]}
        self.raw = json.dumps(self.document).encode()
        self.calls = []

        async def peer(request):
            self.calls.append((request.headers.get("Authorization"), await request.json()))
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(self.raw[:16])
            # A positive read(n) can return this first fragment before the JSON is complete.
            await asyncio.sleep(0.03)
            for offset in range(16, len(self.raw), 16384):
                await response.write(self.raw[offset : offset + 16384])
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/internal/v1/provider-self-service/embed", peer)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        platform = SimpleNamespace(provider_catalog=None, settings={"provider_self_service": {
            "gateway_url": f"http://127.0.0.1:{port}", "gateway_token_env": "SYNTHETIC_TOKEN",
        }})
        self.client = ProviderManagement(platform, SimpleNamespace())
        self.context = SimpleNamespace(provider_id="synthetic", revision=1,
            protocol="openai-chat-completions", base_url="https://unused.invalid",
            model_id="synthetic-4096", api_key="synthetic-only")

    async def test_delayed_vector_response_is_complete_through_retrieval(self):
        self.assertGreater(len(self.raw), 65536)
        retrieval = MemoryRetrieval(SimpleNamespace(), SimpleNamespace(providers=self.client))
        with patch.object(retrieval, "connection", return_value=vars(self.context)), patch(
            "services.platform.provider_management.secret", return_value="synthetic-token"
        ):
            result = await retrieval.execute({}, "embed", {"texts": ["synthetic"]})
        self.assertEqual(result, self.document)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "Bearer synthetic-token")
        self.assertEqual(self.calls[0][1]["payload"], {"texts": ["synthetic"]})

    async def test_response_byte_limit_accepts_boundary_and_rejects_one_extra_byte(self):
        self.raw = b'{"padding":"' + b'x' * (1048576 - 14) + b'"}'
        self.assertEqual(len(self.raw), 1048576)
        with patch("services.platform.provider_management.secret", return_value="synthetic-token"):
            result = await self.client.gateway_request("embed", self.context)
            self.assertEqual(len(result["padding"]), 1048576 - 14)
            self.raw += b' '
            with self.assertRaises(Fault) as error:
                await self.client.gateway_request("embed", self.context)
        self.assertEqual((error.exception.code, error.exception.status), ("dependency_unavailable", 503))


async def run(function, *args):
    return function(*args)


class RetrievalSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = ExternalCatalog(Path(self.temp.name) / "external", create=True)
        self.p = SimpleNamespace(
            auth=SimpleNamespace(
                principals={"admin": {"kind": "operator", "actions": ["external.manage"]}}
            ),
            local_work=SimpleNamespace(run=run),
        )
        self.console = SimpleNamespace(
            config={"principal": "admin"},
            external=SimpleNamespace(catalog=self.catalog),
            session_valid=lambda session: True,
            providers=SimpleNamespace(
                config=True, gateway_request=AsyncMock(return_value={"vectors": [[1.0, 0.0]]})
            ),
        )
        self.service = MemoryRetrieval(self.p, self.console)

    async def save(self, kind, value, credential=None, **extra):
        return await self.service.route(
            PREFIX + "save",
            dict(
                kind=kind,
                value=value,
                credential=credential or {"action": "keep"},
                expected_revision=self.catalog.snapshot()[0],
                client_id=str(uuid.uuid4()),
                **extra,
            ),
            {},
        )

    async def connection(self, kind="embed"):
        return await self.save(
            kind,
            {"base_url": "https://model.example/v1", "model_id": "semantic"},
            {"action": "replace", "value": "synthetic-retrieval-secret"},
        )

    async def test_modes_persist_and_credentials_never_project(self):
        with self.assertRaises(Fault):
            await self.save("policy", {"mode": "hybrid", "rerank": False, "candidates": 40})
        view = await self.connection()
        self.assertNotIn("synthetic-retrieval-secret", json.dumps(view))
        await self.connection("rerank")
        for mode in ("keyword", "vector", "hybrid"):
            view = await self.save("policy", {"mode": mode, "rerank": True, "candidates": 30})
            self.assertEqual(view["policy"], {"mode": mode, "rerank": True, "candidates": 30})
        reopened = ExternalCatalog(self.catalog.root)
        self.assertEqual(reopened.snapshot()[1]["retrieval"]["value"]["mode"], "hybrid")
        self.assertNotIn(b"synthetic-retrieval-secret", self.catalog.database.read_bytes())

    async def test_unauthorized_and_stale_saves_do_not_change_settings(self):
        await self.connection()
        original = self.catalog.snapshot()[0]
        self.p.auth.principals["admin"]["actions"] = []
        with self.assertRaises(Fault):
            await self.connection()
        self.assertEqual(self.catalog.snapshot()[0], original)
        self.p.auth.principals["admin"]["actions"] = ["external.manage"]
        with self.assertRaises(Fault):
            await self.service.route(
                PREFIX + "save",
                {
                    "kind": "policy",
                    "value": {"mode": "keyword", "rerank": False, "candidates": 40},
                    "credential": {"action": "keep"},
                    "expected_revision": 0,
                    "client_id": str(uuid.uuid4()),
                },
                {},
            )

    async def test_gateway_uses_saved_configuration_and_fixed_probe(self):
        await self.connection()
        result = await self.service.route(PREFIX + "test", {"kind": "embed"}, {})
        self.assertEqual(result, {"outcome": "succeeded"})
        call = self.console.providers.gateway_request.call_args
        self.assertEqual(call.args[0], "embed")
        self.assertEqual(call.args[1].api_key, "synthetic-retrieval-secret")
        self.assertEqual(call.kwargs["payload"], {"texts": ["记忆连接测试"]})

    async def test_embedding_space_changes_with_connection_not_policy(self):
        await self.connection()
        before = self.service.settings(self.service.snapshot()[1])
        await self.save("policy", {"mode": "vector", "rerank": False, "candidates": 30})
        changed = self.service.settings(self.service.snapshot()[1])
        self.assertNotEqual(before["revision"], changed["revision"])
        self.assertEqual(before["embedding_revision"], changed["embedding_revision"])
        await self.connection()
        self.assertNotEqual(
            changed["embedding_revision"],
            self.service.settings(self.service.snapshot()[1])["embedding_revision"],
        )


class SharedRetrievalModelsTests(unittest.IsolatedAsyncioTestCase):
    setUp = RetrievalSettingsTests.setUp
    save = RetrievalSettingsTests.save
    connection = RetrievalSettingsTests.connection

    async def test_shared_models_reuse_one_key_and_invalidate_vector_space(self):
        from services.platform.provider_catalog import ProviderCatalog

        providers = ProviderCatalog(Path(self.temp.name) / "providers", create=True)
        self.p.provider_catalog = providers
        source = providers.sources.save(
            client_id="source",
            name="one",
            base_url="https://example.invalid/v1",
            api_key="synthetic-shared",
        )
        identities = {}
        for kind in ("embed", "rerank"):
            identities[kind] = providers.sources.add_models(
                client_id="add-" + kind,
                source_id=source["source_id"],
                expected_revision=1,
                models=[kind],
                kind=kind,
            )["provider_ids"][0]
            view = await self.save(kind, {"provider_id": identities[kind]})
            self.assertEqual(identities[kind], view["connections"][kind]["provider_id"])
            self.assertNotIn("synthetic-shared", json.dumps(view))
        await self.save("policy", {"mode": "hybrid", "rerank": True, "candidates": 40})
        _, rows = self.service.snapshot()
        before = self.service.settings(rows)
        await self.service.execute(rows, "embed", {"texts": ["sample"]})
        self.assertEqual(
            "synthetic-shared", self.console.providers.gateway_request.call_args.args[1].api_key
        )
        self.assertIsNone(self.catalog.snapshot()[1]["retrieval:embed"]["credential"])
        providers.sources.save_model(
            client_id="change",
            provider_id=identities["embed"],
            expected_revision=1,
            name="new",
            model_id="new-vector-space",
            kind="embed",
            enabled=True,
        )
        with self.assertRaises(Fault):
            self.service.connection(rows, "embed")
        _, rows = self.service.snapshot()
        self.assertNotEqual(
            before["embedding_revision"], self.service.settings(rows)["embedding_revision"]
        )
        self.assertEqual("new-vector-space", self.service.connection(rows, "embed")["model_id"])
        self.assertEqual("rerank", self.service.connection(rows, "rerank")["model_id"])
        with self.assertRaises(Fault):
            await self.save("embed", {"provider_id": identities["rerank"]})
        providers.sources.clear_key(
            client_id="clear", source_id=source["source_id"], expected_revision=1
        )
        with self.assertRaises(Fault):
            await self.save("policy", {"mode": "hybrid", "rerank": True, "candidates": 40})
        view = self.service.view()
        self.assertFalse(view["connections"]["embed"]["has_key"])
        self.assertFalse(view["connections"]["rerank"]["has_key"])
        await self.save("policy", {"mode": "keyword", "rerank": False, "candidates": 40})

    async def test_replacing_standalone_with_shared_reference_removes_old_secret(self):
        from services.platform.provider_catalog import ProviderCatalog

        await self.connection()
        providers = ProviderCatalog(Path(self.temp.name) / "providers", create=True)
        self.p.provider_catalog = providers
        source = providers.sources.save(
            client_id="source",
            name="shared",
            base_url="https://example.invalid/v1",
            api_key="synthetic-shared",
        )
        identity = providers.sources.add_models(
            client_id="add",
            source_id=source["source_id"],
            expected_revision=1,
            models=["embed"],
            kind="embed",
        )["provider_ids"][0]
        await self.save("embed", {"provider_id": identity})
        stored = self.catalog.snapshot()[1]["retrieval:embed"]
        self.assertIsNone(stored["credential"])
        self.assertNotIn("base_url", stored["value"])
        self.assertEqual(identity, stored["value"]["provider_id"])
