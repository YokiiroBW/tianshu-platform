"""Contract-response fixture and isolated browser server; never connects to a real node.

The canned executor exercises platform transport/state handling, not SSH parsing. The separate
joint test uses the actual fixed node executor and its OpenSSH synthetic endpoint.
"""

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def contract_directory():
    selected = os.environ.get("TS177_NODE_CONTRACT_DIR")
    if selected:
        return Path(selected)
    return Path(os.environ["TS012_CONTRACT_DIR"]).parents[1] / "infrastructure-node/candidate-v1"


def domain_configuration(directory, *, timeout_ms=2000):
    return {
        "directory": str(Path(directory) / "external"),
        "contract_directory": str(contract_directory()),
        "executor": {
            "command": [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--executor",
                "--directory",
                str(directory),
                "--contract",
                str(contract_directory()),
            ],
            "directory": str(Path(__file__).resolve().parents[2]),
        },
        "poll_interval_seconds": 5,
        "stale_after_seconds": 10,
        "timeout_ms": timeout_ms,
    }


def response_fixture(request, root, contract):
    operation = request["operation"]
    node = request["node_id"]
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    mode_file = root / "mode.json"
    mode = json.loads(mode_file.read_text()) if mode_file.exists() else {}
    with (root / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"operation": operation, "node_id": node, "time": now}) + "\n")
    if mode.get("spawn_child"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        (root / "child.pid").write_text(str(child.pid))
        if mode.get("exit_parent"):
            os._exit(0)
        time.sleep(60)
    if mode.get("delay") and operation != "node.catalog":
        time.sleep(mode["delay"])
    name = {
        "node.catalog": "catalog",
        "node.snapshot": "snapshot",
        "docker.inventory": "inventory",
        "docker.stats": "stats",
        "docker.logs": "logs",
        "compose.inventory": "compose",
    }[operation]
    response = json.loads((contract / "examples" / f"{name}.response.json").read_text())
    response.update(
        request_id=request["request_id"], node_id=node, operation=operation, observed_at=now
    )
    capabilities = ["node.snapshot"] if node == "fixture-router" else response["capabilities"]
    if operation != "node.catalog":
        response["capabilities"] = capabilities
    if (
        mode.get("offline")
        and operation != "node.catalog"
        or mode.get("catalog_unavailable")
        and operation == "node.catalog"
    ):
        response.update(
            status="unavailable",
            reason_codes=["ssh_unavailable"],
            data=None,
            completeness={
                "state": "unavailable",
                "returned": 0,
                "total": None,
                "next_cursor": None,
            },
        )
        return response
    if operation == "node.catalog":
        example = response["data"]["nodes"][0]
        example["label"] = "合成 NAS"
        response["data"]["nodes"].append(
            {
                "id": "fixture-router",
                "label": "合成路由器",
                "kind": "openwrt",
                "capabilities": ["node.snapshot"],
            }
        )
        if mode.get("empty_catalog"):
            response["data"]["nodes"] = []
        response["completeness"].update(
            returned=len(response["data"]["nodes"]), total=len(response["data"]["nodes"])
        )
        return response
    state_path = root / f"{node}-{name}.count"
    count = int(state_path.read_text()) + 1 if state_path.exists() else 1
    state_path.write_text(str(count))
    if operation == "node.snapshot":
        data = response["data"]
        data["identity"].update(
            node_id=node, kind="openwrt" if node == "fixture-router" else "synology"
        )
        if count > 1:
            data["cpu"].update(usage_ratio=0.125, state="ok", reason_codes=[])
            data["interfaces"][0].update(
                receive_bytes_per_second=1024,
                transmit_bytes_per_second=2048,
                state="ok",
                reason_codes=[],
            )
        data["memory"].update(
            total_bytes=32 * 1024**3, available_bytes=20 * 1024**3, used_bytes=12 * 1024**3
        )
        if node == "fixture-router":
            data["openwrt"] = {
                "board": {
                    "model": "Synthetic Router",
                    "board_name": "fixture",
                    "system": "synthetic",
                    "release": {
                        "distribution": "OpenWrt",
                        "version": "fixture",
                        "revision": None,
                        "target": "synthetic",
                        "description": "OpenWrt 合成固件",
                    },
                },
                "info": {
                    "uptime_seconds": 3600,
                    "load_average": [0.1, 0.2, 0.3],
                    "memory": {
                        "total_bytes": 1024**3,
                        "free_bytes": 512 * 1024**2,
                        "shared_bytes": None,
                        "buffered_bytes": None,
                        "available_bytes": 512 * 1024**2,
                    },
                },
                "interfaces": [
                    {
                        "name": "wan",
                        "up": True,
                        "pending": False,
                        "available": True,
                        "autostart": True,
                        "device": "eth0",
                        "l3_device": "eth0",
                        "protocol": "dhcp",
                        "uptime_seconds": 1200,
                        "ipv4_addresses": [{"address": "192.0.2.10", "mask": 24}],
                        "ipv6_addresses": [],
                        "source": "ubus_network_interface_dump",
                    }
                ],
                "per_device_traffic": {
                    "state": "unsupported",
                    "reason_code": "per_device_source_not_configured",
                    "items": [],
                },
                "reason_codes": [],
            }
    elif operation == "docker.inventory":
        template = response["data"]["containers"][0]
        rows = []
        for index in range(mode.get("containers", 130)):
            item = copy.deepcopy(template)
            item.update(id=f"{index + 1:064x}", name=f"synthetic-app-{index + 1:03d}")
            item["compose"]["project"] = "synthetic" if index < 65 else "utilities"
            item["created_at"] = item["started_at"] = now
            if index == 1:
                item["health"] = "healthy"
            elif index == 2:
                item["health"] = "unhealthy"
            elif index == 3:
                item.update(state="restarting", health="starting")
            elif index == 4:
                item.update(state="exited", running=False)
            elif index == 5:
                item.update(state="paused", health="unknown")
            rows.append(item)
        start = int(request["params"].get("cursor") or 0)
        limit = request["params"].get("limit", 100)
        response["data"]["containers"] = rows[start : start + limit]
        response["completeness"].update(
            returned=len(response["data"]["containers"]),
            total=len(rows),
            next_cursor=str(start + limit) if start + limit < len(rows) else None,
        )
        if mode.get("partial"):
            response.update(status="partial", reason_codes=["inventory_incomplete"])
            response["completeness"]["state"] = "partial"
    elif operation == "docker.stats":
        template = response["data"]["containers"][0]
        rows = []
        for identity in request["params"]["container_ids"]:
            item = copy.deepcopy(template)
            item.update(id=identity, sampled_at=now)
            item["cpu"].update(
                usage_ratio=None if count == 1 else 1.5,
                state="first_sample" if count == 1 else "ok",
                reason_codes=["first_sample"] if count == 1 else [],
            )
            item["network"].update(receive_bytes=count * 4096, transmit_bytes=count * 8192)
            item["block_io"].update(read_bytes=count * 1024, write_bytes=count * 2048)
            rows.append(item)
        response["data"]["containers"] = rows
        response["completeness"].update(returned=len(rows), total=len(rows))
        if count == 1:
            response.update(status="partial", reason_codes=["first_sample"])
            response["completeness"]["state"] = "partial"
    elif operation == "docker.logs":
        response["data"].update(
            container_id=request["params"]["container_id"],
            stdout="合成容器日志\n",
            stderr="",
            returned_bytes=len("合成容器日志\n".encode()),
            truncated=bool(mode.get("truncated")),
        )
    if mode.get("wrong_node"):
        response["node_id"] = "wrong-node"
    return response


def execute(arguments):
    root, contract = Path(arguments.directory), Path(arguments.contract)
    request = json.loads(sys.stdin.buffer.read())
    mode_path = root / "mode.json"
    mode = json.loads(mode_path.read_text()) if mode_path.exists() else {}
    if mode.get("invalid_json"):
        print("not JSON")
        return
    if mode.get("oversize"):
        sys.stdout.write("x" * (4 * 1024**2 + 1))
        return
    response = response_fixture(request, root, contract)
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


async def serve(arguments):
    import asyncio
    import tempfile
    from aiohttp import web
    from unittest.mock import patch

    from fixtures import ENV
    from web_fixtures import web_settings
    from services.platform.external_catalog import ExternalCatalog
    from services.platform.server import create_app
    from services.platform.service import Platform

    with (
        tempfile.TemporaryDirectory(prefix="ts177-browser-") as directory,
        patch.dict(os.environ, ENV),
    ):
        ExternalCatalog(Path(directory) / "external", create=True)
        settings = web_settings(
            directory,
            origin=f"http://127.0.0.1:{arguments.port}",
            static=Path(__file__).resolve().parents[2] / "apps/web/dist",
        )
        settings["principals"]["admin"]["actions"].append("external.manage")
        settings["infrastructure"] = domain_configuration(directory)
        platform = Platform(settings)
        runner = web.AppRunner(create_app(platform), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", arguments.port).start()
        print("synthetic_infrastructure_ready", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            platform.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executor", action="store_true")
    parser.add_argument("--directory")
    parser.add_argument("--contract")
    parser.add_argument("--port", type=int, default=4827)
    arguments = parser.parse_args()
    if arguments.executor:
        execute(arguments)
    else:
        import asyncio

        asyncio.run(serve(arguments))
