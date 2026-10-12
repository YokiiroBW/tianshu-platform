"""Real Memory -> Gateway -> synthetic HTTPS model, Platform config authority loopback."""

import asyncio
import importlib.util
import os
import ssl
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from aiohttp import web, ClientSession

from fixtures import ENV, CONTRACT, TOKENS
from services.platform.external_catalog import ExternalCatalog
from services.platform.memory_retrieval import PATH
from services.platform.provider_catalog import ProviderCatalog
from services.platform.server import create_app, create_console
from services.platform.service import Platform
from web_fixtures import web_settings


@unittest.skipUnless(
    os.environ.get("TS_RETRIEVAL_GATEWAY") and os.environ.get("TS_RETRIEVAL_MEMORY"),
    "explicit isolated product paths required",
)
class RetrievalIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_real_routes_semantic_query_rerank_auth_and_model_replacement(self):
        gateway = Path(os.environ["TS_RETRIEVAL_GATEWAY"])
        memory = Path(os.environ["TS_RETRIEVAL_MEMORY"])
        sys.path[:0] = [str(gateway / "src"), str(gateway / "tests"), str(memory / "src")]
        from gateway_fixtures import registration, CONTRACT as GATEWAY_CONTRACT
        from observability_fixtures import start_tls, write_tls
        from test_provider_adapter import FixtureTargets
        from tianshu_gateway.config import ClientGrant
        from tianshu_gateway.server import Settings, GATEWAY, create_app as gateway_app
        from tianshu_gateway.provider_adapter import OpenAIAdapter
        from tianshu_memory.contracts import Contracts
        from tianshu_memory.retrieval import Retrieval
        from tianshu_memory.retrieval_transport import RetrievalTransport

        spec = importlib.util.spec_from_file_location(
            "retrieval_memory_fixture", memory / "tests/conftest.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        with patch.dict(
            os.environ,
            {
                **ENV,
                "TS_RETRIEVAL_MANAGEMENT": "synthetic-retrieval-management-only",
                "TS_RETRIEVAL_CLIENT": "synthetic-client",
                "TS_RETRIEVAL_PLATFORM": TOKENS["GATEWAY"],
                "TS_RETRIEVAL_MEMORY_KEY": "synthetic-memory-gateway-only",
                "TS_RETRIEVAL_ORIGIN": "synthetic-origin",
            },
        ):
            root = Path(directory)
            calls = []

            async def upstream(request):
                body = await request.json()
                calls.append((request.path, body))
                self.assertEqual(request.headers["Authorization"], "Bearer synthetic-model-key")
                if request.path == "/v1/embeddings":
                    return web.json_response(
                        {
                            "data": [
                                {"index": i, "embedding": [1.0, 0.0]}
                                for i in reversed(range(len(body["input"])))
                            ]
                        }
                    )
                return web.json_response(
                    {
                        "results": [
                            {"index": i, "relevance_score": 0.9}
                            for i in reversed(range(len(body["documents"])))
                        ]
                    }
                )

            models = web.Application()
            models.router.add_post("/v1/embeddings", upstream)
            models.router.add_post("/v1/rerank", upstream)
            runner, model_url = await start_tls(models, directory)
            self.addAsyncCleanup(runner.cleanup)
            cert, _ = write_tls(directory)
            tls = ssl.create_default_context(cafile=str(cert))
            settings = Settings(
                str(GATEWAY_CONTRACT),
                str(root / "gateway.sqlite"),
                "http://127.0.0.1:1",
                "secret:platform",
                "TS_RETRIEVAL_ORIGIN",
                {
                    "secret:platform": "TS_RETRIEVAL_PLATFORM",
                    "secret:client": "TS_RETRIEVAL_CLIENT",
                    "secret:management": "TS_RETRIEVAL_MANAGEMENT",
                    "secret:memory": "TS_RETRIEVAL_MEMORY_KEY",
                },
                [registration("http://127.0.0.1:1")],
                [ClientGrant("companion", "secret:client", "fixture", 1)],
                provider_management_credential_ref="secret:management",
                memory_retrieval_credential_ref="secret:memory",
            )

            async def resolve(host, port):
                return ("127.0.0.1",)

            catalog = ProviderCatalog(root / "providers", create=True)
            platform_settings = web_settings(directory)
            platform_settings["principals"]["gateway"]["actions"].append("memory.retrieval")
            platform_settings["provider_self_service"] = {
                "directory": str(catalog.directory),
                "gateway_url": "http://127.0.0.1:1",
                "gateway_token_env": "TS_RETRIEVAL_MANAGEMENT",
            }
            platform = Platform(platform_settings)
            console = create_console(platform)
            external = ExternalCatalog(root / "external", create=True)
            console.external.catalog = external
            source = catalog.sources.save(
                client_id="shared-source",
                name="Shared fixture",
                base_url=model_url + "/v1",
                api_key="synthetic-model-key",
            )
            model_ids = {}
            for kind in ("embed", "rerank"):
                model_ids[kind] = catalog.sources.add_models(
                    client_id="model-" + kind,
                    source_id=source["source_id"],
                    expected_revision=1,
                    models=[kind],
                    kind=kind,
                )["provider_ids"][0]
                external.save(
                    "retrieval:" + kind,
                    {"provider_id": model_ids[kind]},
                    {"action": "clear"},
                    {"action": "clear"},
                    external.snapshot()[0],
                    str(uuid.uuid4()),
                )
            external.save(
                "retrieval",
                {"mode": "hybrid", "rerank": True, "candidates": 40},
                {"action": "keep"},
                {"action": "clear"},
                external.snapshot()[0],
                str(uuid.uuid4()),
            )
            runner, platform_url = await start_tls(create_app(platform, console=console), directory)
            self.addAsyncCleanup(runner.cleanup)
            async with ClientSession() as client:
                async with client.post(
                    platform_url + PATH,
                    json={"operation": "settings"},
                    ssl=tls,
                    headers={"Authorization": "Bearer " + TOKENS["GATEWAY"]},
                ) as response:
                    self.assertEqual(response.status, 200, await response.text())
            settings.platform_base_url = platform_url
            settings.targets = [
                {"base_url": platform_url, "addresses": ["127.0.0.1"], "allow_private_http": False}
            ]
            app = gateway_app(settings)
            runner, gateway_url = await start_tls(app, directory)
            self.addAsyncCleanup(runner.cleanup)
            # The recorded loopback fixture uses its own CA; production verification stays enabled.
            app[GATEWAY].session.connector._ssl = tls
            app[GATEWAY].provider_adapter = OpenAIAdapter(
                policy=FixtureTargets(), resolver=resolve, tls_context=tls
            )
            h = module.Harness(root / "memory", Contracts(CONTRACT))
            h.seed()
            h.config["retrieval"] = {
                "url": gateway_url + PATH,
                "token": "synthetic-memory-gateway-only",
                "ca_file": str(cert),
            }
            h.save_config()
            h.service.retrieval = Retrieval(RetrievalTransport(h.config_path))
            answer = await asyncio.to_thread(h.select, query="睡前饮料推荐")
            self.assertTrue(answer["selected_units"])
            self.assertEqual({path for path, _ in calls}, {"/v1/embeddings", "/v1/rerank"})
            self.assertNotIn("receipt_id", str(calls))
            self.assertNotIn("immutable_account_id", str(calls))
            before = len([body for path, body in calls if path.endswith("embeddings")])
            await asyncio.to_thread(h.select, query="睡前饮料推荐")
            self.assertEqual(
                len([body for path, body in calls if path.endswith("embeddings")]), before + 1
            )
            catalog.sources.save_model(
                client_id="new-space",
                provider_id=model_ids["embed"],
                expected_revision=1,
                name="new",
                model_id="embed-new-space",
                kind="embed",
                enabled=True,
            )
            calls.clear()
            await asyncio.to_thread(h.select, query="睡前饮料推荐")
            embeddings = [body for path, body in calls if path.endswith("embeddings")]
            self.assertEqual(len(embeddings), 2)
            self.assertTrue(all(body["model"] == "embed-new-space" for body in embeddings))
            async with ClientSession() as client:
                async with client.post(
                    gateway_url + PATH,
                    json={"operation": "settings"},
                    ssl=tls,
                    headers={"Authorization": "Bearer wrong"},
                ) as response:
                    self.assertEqual(response.status, 401)
            h.client.close()
