"""Cancellation and hard timeout around the reused Legado parser."""

import asyncio
import json
import os
from pathlib import Path
import re
import sys

from ...contracts import Fault, require


def capabilities(source):
    serialized = json.dumps(source, ensure_ascii=False)
    unsupported = []
    if re.search(r"[\"']?webView[\"']?\s*:\s*true", serialized, re.I) or source.get(
        "ruleContent", {}
    ).get("webJs"):
        unsupported.append("webview")
    if source.get("loginUi") or source.get("loginCheckJs"):
        unsupported.append("interactive_login")
    return {
        "search": bool(source.get("searchUrl") and source.get("ruleSearch")),
        "content": bool(source.get("ruleToc") and source.get("ruleContent")),
        "javascript": "@js:" in serialized or "<js>" in serialized or "{{" in serialized,
        "unsupported": unsupported,
    }


class RuleEngine:
    def __init__(self, *, allowed_private_origins=(), timeout=30):
        self.allowed_private_origins = tuple(allowed_private_origins)
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(4)
        self.processes = set()

    async def call(self, source, operation, **values):
        request = {
            "source": source,
            "operation": operation,
            **values,
            "allowed_private_origins": self.allowed_private_origins,
        }
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        require(len(payload) <= 4 * 1024 * 1024, "book_rule_too_large", 413)
        root = Path(__file__).resolve().parents[4]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "TMPDIR"}
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        async with self.semaphore:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);from services.platform.media.books.engine_worker import main;main()",
                str(root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
            self.processes.add(process)
            try:
                try:
                    output, _ = await asyncio.wait_for(process.communicate(payload), self.timeout)
                except TimeoutError:
                    raise Fault("book_rule_timeout", 504) from None
                require(
                    process.returncode == 0 and len(output) <= 8 * 1024 * 1024,
                    "book_rule_failed",
                    502,
                )
                try:
                    result = json.loads(output)
                except ValueError:
                    raise Fault("book_rule_failed", 502) from None
                require(result.get("ok"), result.get("code", "book_rule_failed"), 502)
                return result["data"]
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                self.processes.discard(process)

    async def close(self):
        for process in tuple(self.processes):
            if process.returncode is None:
                process.kill()
            await process.wait()
