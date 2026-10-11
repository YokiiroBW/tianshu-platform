import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.platform.contracts import Fault
from services.platform.external_catalog import ExternalCatalog
from services.platform.memory_retrieval import MemoryRetrieval, PREFIX


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
