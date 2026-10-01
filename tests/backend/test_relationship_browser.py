"""Real three-product fixture: loopback HTTP UI, CA-verified HTTPS peers.

Use the existing WebAccess HTTP mode only for the browser entry. Internal RPC
uses explicit temporary CA verification. No OS/browser trust changes or bypass.
"""

import asyncio
import os
import sys
from pathlib import Path

from fixtures import ENV
from test_relationship_joint import Joint


async def run(arguments):
    os.environ.update(ENV)
    os.environ.setdefault("TIANSHU_CONTRACTS", os.environ["TS012_CONTRACT_DIR"])
    root = Path(__file__).resolve().parents[2]
    output = Path(os.environ["TS116_BROWSER_OUTPUT"]).resolve()
    if not output.is_relative_to(root / ".runtime"):
        raise RuntimeError("Browser artifacts must stay in this worktree's .runtime")
    output.mkdir(parents=True, exist_ok=False)
    fixture, child = Joint(), None
    try:
        await fixture.start(port=4844, static=root / "apps/web/dist", browser_http=True)
        environment = {
            **os.environ,
            "TS116_BROWSER_EXTERNAL": "1",
            "TS116_BROWSER_OUTPUT": str(output),
        }
        environment.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
        node = os.environ.get(
            "TS116_NODE",
            str(
                Path(os.environ["LOCALAPPDATA"]).parents[1]
                / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
            ),
        )
        child = await asyncio.create_subprocess_exec(
            node,
            str(root / "node_modules/@playwright/test/cli.js"),
            "test",
            "--config",
            "apps/web/playwright.relationships.config.ts",
            *arguments,
            cwd=root,
            env=environment,
        )
        result = await child.wait()
        print("browser-artifacts:" + str(output), flush=True)
        return result
    finally:
        try:
            if child is not None and child.returncode is None:
                child.terminate()
                await child.wait()
        finally:
            await fixture.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
