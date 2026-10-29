"""The existing authenticated verifier accepts bounded segment source batches."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from fixtures import ENV, bearer, start_http
from test_bots import bot_settings

from services.platform.server import OBSERVATION_VERIFY, create_app
from services.platform.service import Platform


class ObservationRequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_segment_batch_http_budget_and_authentication(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {**ENV, "TS_OBSERVATION_BUDGET_CORE": "synthetic-core-budget"}),
        ):
            config = bot_settings(folder)
            config["core"] = {
                "base_url": "https://companion.invalid",
                "token_env": "TS_OBSERVATION_BUDGET_CORE",
            }
            config["principals"]["memory"]["actions"].append("observation.verify")
            config["bot_adapter_self_service"] = {
                "directory": str(Path(folder) / "adapters"),
                "allowed_cidrs": ["127.0.0.0/8"],
                "actors": [{"id": "actor:a", "label": "Synthetic"}],
            }
            platform = Platform(config)
            self.addCleanup(platform.close)
            runner, url = await start_http(create_app(platform))
            self.addAsyncCleanup(runner.cleanup)
            body = dict(
                operation="segment",
                actor_id="actor:a",
                instance_id="sdk:synthetic",
                self_id="10001",
                conversation_id="group:20002",
                sources=[
                    dict(
                        source_ref="observation:" + f"{index:064x}",
                        source_digest="a" * 64,
                        scope_version=1,
                        archive_epoch=1,
                    )
                    for index in range(32)
                ],
            )
            self.assertGreater(len(json.dumps(body).encode()), 4096)
            with (
                patch(
                    "services.platform.observation_segments.check_segment",
                    return_value={"valid": True},
                ) as checked,
                patch(
                    "services.platform.observation_segments.resolve_segment",
                    return_value={"valid": True},
                ) as resolved,
            ):
                async with aiohttp.ClientSession(trust_env=False) as client:
                    for operation in ("segment", "segment_resolve"):
                        request = dict(body, operation=operation)
                        async with client.post(
                            url + OBSERVATION_VERIFY,
                            json=request,
                            headers={"Authorization": bearer("MEMORY")},
                        ) as response:
                            self.assertEqual(response.status, 200, await response.text())
                        owner = checked if operation == "segment" else resolved
                        self.assertEqual(owner.call_args.args[1], request)
                    for request, token, status in (
                        (dict(body, padding="x" * (256 * 1024)), bearer("MEMORY"), 413),
                        (dict(body, operation="ingest"), bearer("MEMORY"), 413),
                        (body, "Bearer not-authorized", 401),
                    ):
                        async with client.post(
                            url + OBSERVATION_VERIFY, json=request, headers={"Authorization": token}
                        ) as response:
                            self.assertEqual(response.status, status, await response.text())
                    self.assertEqual((checked.call_count, resolved.call_count), (1, 1))
