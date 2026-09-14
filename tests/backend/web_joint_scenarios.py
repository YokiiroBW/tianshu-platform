"""Actual products and browser; only the external model service is a recording fixture.

Reuse pinned TS050 startup, never its Core DB/commit inspection methods. Assertions
use browser/API results and our own Platform store only.
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from aiohttp import web
from services.platform.server import PLATFORM, create_app
from services.platform.web_console import password_hash
from ts050_source_support import SourceChain, reserve

ROOT = Path(__file__).resolve().parents[2]


class WebJoint(SourceChain):
    silence_ms = 5000

    def platform_settings(self):
        settings = super().platform_settings()
        self.account = settings["principals"]["operator"]["account"]
        settings["principals"]["operator"]["actions"] += [
            "source.register",
            "source.dispatch",
            "mapping.prepare",
        ]
        settings["principals"]["core"]["resolver"]["caller"] = "platform"
        self.set_env("TS014_JOINT_SENDER", "synthetic-sender-" + secrets.token_urlsafe(30))
        settings["principals"]["web-sender"] = {
            "kind": "service",
            "service": "companion",
            "token_env": "TS014_JOINT_SENDER",
            "actions": ["dialogue.send"],
        }
        for audience, channel in self.channels.items():
            channel["namespace"] = "web"
            entry = settings["input_entries"][audience]
            entry.update(owner="operator", account=self.account)
            for actor_entry in entry["actor_entries"]:
                actor = settings["entries"][actor_entry]
                actor.update(owner="operator", account=self.account, kind="local_operator")
                actor["routes"][0]["caller"] = "platform"
                actor["routes"].append(
                    {"caller": "companion", "receiver": "platform", "purpose": "dialogue"}
                )
        sock = reserve()
        self.web_port = sock.getsockname()[1]
        sock.close()
        self.password = secrets.token_urlsafe(24)
        settings["web"] = {
            "origin": f"https://127.0.0.1:{self.web_port}",
            "username": "integration-admin",
            "password_hash": password_hash(self.password),
            "principal": "operator",
            "input_entries": ["self_private"],
            "static_directory": str(ROOT / "apps/web/dist"),
            "dialogue_enabled": True,
        }
        return settings

    def make_platform_app(self):
        # Do not use the generic wire recorder: it would log login passwords.
        app = create_app(self.platform)
        self.sender_attempts = getattr(self, "sender_attempts", 0)

        @web.middleware
        async def lose_second_receipt(request, handler):
            response = await handler(request)
            if request.path == "/internal/v1/conversation/send" and response.status == 200:
                self.sender_attempts += 1
                if self.sender_attempts == 2:
                    # Fault injection after real durable commit. Core must retain
                    # unknown and the browser must not read raw sender text.
                    request.transport.abort()
            return response

        app.middlewares.insert(0, lose_second_receipt)
        return app

    async def start_aio(self, app, *, port=0):
        return await super().start_aio(app, port=self.web_port if PLATFORM in app else port)

    def make_core_config(self):
        config = super().make_core_config()
        config["callers"]["platform"] = config["callers"].pop("nonebot")
        for binding in config["bindings"].values():
            binding.update(namespace="web", service="platform")
        config["services"]["platform_sender"] = {
            "url": self.platform_url,
            "token_env": "TS014_JOINT_SENDER",
            "ca_file": str(self.ca),
        }
        return config

    async def record_model(self, request):
        await request.read()
        if self.model_requests:
            await asyncio.sleep(4)
        return await super().record_model(request)

    async def browser(self, phase):
        env = dict(
            os.environ,
            TS014_WEB_URL=self.platform_url,
            TS014_WEB_PASSWORD=self.password,
            TS014_WEB_PHASE=phase,
            TS014_BROWSER_STATE=str(self.directory / "browser-state.json"),
            TS014_BROWSER_OUTPUT=str(self.directory),
        )
        node = "C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        result = await asyncio.to_thread(
            subprocess.run,
            [node, str(ROOT / "tests/backend/web_joint_browser.mjs")],
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.trace[phase] = json.loads(result.stdout)
        evidence = Path(os.environ["TS014_JOINT_RUNTIME"]) / "results"
        evidence.mkdir(exist_ok=True)
        shutil.copy2(self.directory / (phase + ".png"), evidence / (phase + ".png"))

    async def test_real_web_login_source_core_memory_gateway_and_persistent_sender(self):
        await self.browser("chat")
        self.assertEqual(self.channel_requests, [], "web must never use recorded NoneBot sender")
        self.assertGreaterEqual(len(self.model_requests), 1)
        self.assertEqual(self.sender_attempts, 2, "unknown must never trigger another send")
        self.check(
            "real_TLS_four_products_web_sender",
            external_model="recorded fixture",
            browser="actual Chromium",
            no_nonebot_fallback=True,
        )
        # Restart the Platform HTTP session owner; Core history and sender sidecar
        # remain durable, old browser login must expire without dropping replies.
        await self.platform_runner.cleanup()
        app = self.make_platform_app()
        await self.start_aio(app)
        await self.browser("reconnect")

    async def asyncTearDown(self):
        # Deliberately omit SourceChain.core_state(), commits(), and DB inspection.
        self.trace["wire"] = self.wire
        self.trace["external_model_requests"] = len(self.model_requests)
        self.trace["no_cross_product_SQL"] = True
