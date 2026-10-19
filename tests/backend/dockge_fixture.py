"""Isolated Platform HTTP and bounded synthetic node protocol, never Docker or SSH.

The producer's own integration tests own real Compose behaviour. This fixture drives the actual
platform executor processes, durable scheduler, web sessions and browser without a route stub.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from infrastructure_fixture import contract_directory, domain_configuration, response_fixture

STACK = "stack_" + "a" * 24
PROTECTED = "stack_" + "b" * 24
EXTERNAL = "stack_" + "c" * 24
COMPOSE = 'services:\n  web:\n    image: synthetic:v1\n    environment:\n      API_TOKEN: "{{tianshu.secret:fixture}}"\n    volumes:\n      - fixture-data:/data\nvolumes:\n  fixture-data: {}\n'
CAPABILITIES = [
    "control.catalog",
    "stack.list",
    "stack.inspect",
    "container.inspect",
    "stack.plan",
    "operation.submit",
    "operation.get",
    "operation.list",
    "stack.versions",
]


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def control_contract_directory():
    selected = os.environ.get("TS205_CONTROL_CONTRACT_DIR")
    if selected:
        return Path(selected)
    return (
        Path(__file__).resolve().parents[2]
        / ".runtime/contracts-export/contracts/infrastructure-control/candidate-v1"
    )


def configuration(directory, *, timeout_ms=2000):
    result = domain_configuration(directory, timeout_ms=timeout_ms)
    command = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "--executor",
        "--directory",
        str(directory),
        "--contract",
        str(contract_directory()),
        "--control-contract",
        str(control_contract_directory()),
    ]
    result["executor"]["command"] = command
    result["control"] = {
        "contract_directory": str(control_contract_directory()),
        "executor": {"command": command, "directory": result["executor"]["directory"]},
    }
    result["stale_after_seconds"] = 120
    return result


def _runtime(identity, *, state="running", health="not_configured"):
    return {
        "state": state,
        "health": health,
        "observed_at": now(),
        "containers": [
            {
                "id": identity,
                "name": "synthetic-" + identity[-2:],
                "state": "running" if state == "running" else "exited",
                "running": state == "running",
                "health": "none",
                "image_id": "sha256:" + "d" * 64,
                "reason_codes": [],
            }
        ],
    }


def _stack(identity, name, index, *, owner="platform", protected=False):
    return {
        "id": identity,
        "name": name,
        "project_name": name,
        "directory": "/synthetic/stacks/" + name,
        "compose_file": "compose.yaml",
        "revision": hashlib.sha256((name + COMPOSE).encode()).hexdigest(),
        "owner": owner,
        "protection": {
            "protected": protected,
            "owner": "resident" if protected else None,
            "reason": "protected_project" if protected else None,
            "maintenance_ref": "resident.update" if protected else None,
        },
        "allowed_actions": []
        if protected
        else [
            "stack." + action
            for action in (
                ["start", "stop", "restart", "update", "deploy", "restore"]
                if owner == "platform"
                else ["start", "stop", "restart", "update"]
            )
        ],
        "compose_content": COMPOSE,
        "runtime": _runtime(f"{index:064x}"),
        "reason_codes": [],
    }


def public_stack(value):
    result = copy.deepcopy(value)
    result["compose_content"] = re.sub(
        r"(?im)(API_TOKEN|PASSWORD|API_KEY):.*$",
        r'\1: "{{tianshu.secret:fixture}}"',
        result["compose_content"],
    )
    return result


def initial_state():
    return {
        "stacks": [
            _stack(STACK, "fixture-web", 1),
            _stack(PROTECTED, "tianshu-platform", 2, protected=True),
            _stack(EXTERNAL, "fixture-external", 3, owner="external"),
        ],
        "plans": {},
        "operations": {},
        "versions": {},
        "submits": 0,
    }


def state_read(root):
    with closing(sqlite3.connect(root / "synthetic-dockge.sqlite", timeout=5)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS fixture_state (id INTEGER PRIMARY KEY, document TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO fixture_state VALUES(1,?)", (json.dumps(initial_state()),)
        )
        return json.loads(db.execute("SELECT document FROM fixture_state WHERE id=1").fetchone()[0])


def mode(root):
    path = root / "control-mode.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _response(request, data=None, *, status="ok", reasons=None):
    count = (
        len(next(iter(data.values())))
        if data and isinstance(next(iter(data.values())), list)
        else int(data is not None)
    )
    return {
        **{key: request[key] for key in ("version", "request_id", "node_id", "operation")},
        "status": status,
        "reason_codes": reasons or [],
        "observed_at": now(),
        "capabilities": CAPABILITIES,
        "completeness": {
            "state": "complete" if status == "ok" else "unavailable",
            "returned": count,
            "total": count if data else None,
            "next_cursor": None,
        },
        "data": data,
    }


def _plan(request, state, selected):
    params = request["params"]
    target = next((row for row in state["stacks"] if row["id"] == params.get("stack_id")), None)
    if params["action"] != "create" and target is None:
        return _response(request, status="unavailable", reasons=["stack_not_found"])
    if target and target["revision"] != params["expected_revision"]:
        return _response(request, status="conflict", reasons=["stack_revision_conflict"])
    project = target["project_name"] if target else params["project_name"]
    blockers = []
    if target and target["protection"]["protected"]:
        blockers.append("protected_project")
    if target and target["owner"] != "platform" and params["action"] in {"deploy", "restore"}:
        blockers.append("stack_not_platform_owned")
    if not target and any(row["project_name"] == project for row in state["stacks"]):
        blockers.append("project_exists")
    if selected.get("port_conflict"):
        blockers.append("port_conflict")
    old = target["compose_content"] if target else ""
    content = params.get("compose_content", old)
    if params["action"] == "restore":
        restored = state["versions"].get(params["restore_version_id"])
        if not restored or restored["public"]["stack_id"] != target["id"]:
            blockers.append("version_not_found")
        else:
            content = restored["compose_content"]
    identity = (
        target["id"] if target else "stack_" + hashlib.sha256(project.encode()).hexdigest()[:24]
    )
    # Synthetic producer redacts all non-structural input instead of returning user content.
    safe = re.sub(
        r"(?im)(API_TOKEN|PASSWORD|API_KEY):.*$", r'\1: "{{tianshu.secret:fixture}}"', content
    )
    plan = {
        "plan_id": "plan:" + uuid.uuid4().hex,
        "stack_id": identity,
        "project_name": project,
        "action": params["action"],
        "state": "blocked" if blockers else "ready",
        "base_revision": target["revision"] if target else None,
        "proposed_revision": hashlib.sha256(content.encode()).hexdigest(),
        "created_at": now(),
        "expires_at": datetime.fromtimestamp(time.time() + 300, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "config_diff": "--- current\n+++ proposed\n" + safe,
        "ports": [
            {
                "service": "web",
                "published": "18080",
                "target": 8080,
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
            }
        ],
        "mounts": [
            {
                "service": "web",
                "kind": "volume",
                "source": "fixture-data",
                "target": "/data",
                "read_only": False,
            }
        ],
        "images": [
            {
                "service": "web",
                "from": "synthetic:v1",
                "to": "synthetic:v2",
                "pull": params["action"] == "update",
                "pull_policy": "missing",
                "current_image_id": "sha256:" + "d" * 64,
            }
        ],
        "warnings": ["configuration_restore_does_not_restore_data"],
        "blockers": blockers,
        "restore_version_id": params.get("restore_version_id"),
    }
    state["plans"][plan["plan_id"]] = {"public": plan, "compose_content": content}
    return _response(request, {"plan": plan})


def _receipt(request, state):
    params = request["params"]
    identity = params["operation_id"]
    old = state["operations"].get(identity)
    if old:
        if (
            old["receipt"]["action"] != params["action"]
            or old["receipt"]["target"] != params["target"]
        ):
            return _response(request, status="conflict", reasons=["operation_id_conflict"])
        return _response(request, {"operation": old["receipt"], "replayed": True})
    target = params["target"]
    stack_id = target.get("stack_id")
    planned = state["plans"].get(target.get("plan_id"))
    if planned:
        stack_id = planned["public"]["stack_id"]
    stack = next(
        (
            row
            for row in state["stacks"]
            if row["id"] == stack_id
            or any(
                item["id"] == target.get("container_id") for item in row["runtime"]["containers"]
            )
        ),
        None,
    )
    reason = None
    if stack and stack["protection"]["protected"]:
        reason = "protected_project"
    elif target.get("expected_revision") and (
        not stack or stack["revision"] != target["expected_revision"]
    ):
        reason = "stack_revision_conflict"
    elif params["action"] == "stack.apply" and not planned:
        reason = "plan_not_found"
    elif planned and (
        planned["public"]["blockers"]
        or (stack and stack["revision"] != planned["public"]["base_revision"])
    ):
        reason = "plan_stale"
    runtime = (
        copy.deepcopy(stack["runtime"])
        if stack
        else _runtime(target.get("container_id", f"{4:064x}"))
    )
    receipt = {
        "operation_id": identity,
        "node_id": request["node_id"],
        "action": params["action"],
        "target": target,
        "state": "rejected" if reason else "running",
        "phase": "complete" if reason else "applying",
        "created_at": now(),
        "updated_at": now(),
        "command": {
            "state": "not_sent" if reason else "sent",
            "exit_code": None,
            "reason_codes": [reason] if reason else [],
        },
        "runtime": runtime,
        "reason_codes": [reason] if reason else [],
        "version_id": None,
        "execution_active": not bool(reason),
    }
    state["operations"][identity] = {
        "receipt": receipt,
        "created": time.time(),
        "stack_id": stack_id or (stack["id"] if stack else None),
    }
    state["submits"] += 1
    return _response(request, {"operation": receipt, "replayed": False})


def _get(request, state, selected):
    operation = state["operations"].get(request["params"]["operation_id"])
    if not operation:
        return _response(request, status="unavailable", reasons=["operation_not_found"])
    receipt = operation["receipt"]
    if receipt["execution_active"] and time.time() - operation["created"] >= selected.get(
        "operation_seconds", 0.2
    ):
        stack = next((row for row in state["stacks"] if row["id"] == operation["stack_id"]), None)
        target = receipt["target"]
        planned = state["plans"].get(target.get("plan_id"))
        failed, unknown = selected.get("pull_failure"), selected.get("worker_unknown")
        if planned and not failed and not unknown:
            if stack is None:
                stack = _stack(
                    planned["public"]["stack_id"],
                    planned["public"]["project_name"],
                    len(state["stacks"]) + 1,
                )
                state["stacks"].append(stack)
            version = "version:" + uuid.uuid4().hex
            public = {
                "version_id": version,
                "stack_id": stack["id"],
                "created_at": now(),
                "operation_id": receipt["operation_id"],
                "revision": stack["revision"],
                "images": [],
                "restores_data": False,
            }
            state["versions"][version] = {
                "public": public,
                "compose_content": stack["compose_content"],
            }
            stack["compose_content"] = planned["compose_content"]
            stack["revision"] = hashlib.sha256(
                (stack["compose_content"] + version).encode()
            ).hexdigest()
            receipt["version_id"] = version
        if stack and not failed and not unknown:
            stopped = receipt["action"].endswith(".stop")
            stack["runtime"] = _runtime(
                stack["runtime"]["containers"][0]["id"], state="stopped" if stopped else "running"
            )
            if selected.get("partial_start"):
                stack["runtime"].update(state="partial", health="unhealthy")
            receipt["runtime"] = copy.deepcopy(stack["runtime"])
        receipt.update(
            state="unknown" if unknown else "failed" if failed else "succeeded",
            phase="unknown" if unknown else "complete",
            updated_at=now(),
            execution_active=False,
        )
        receipt["command"].update(
            state="unknown" if unknown else "failed" if failed else "succeeded",
            exit_code=None if unknown else 1 if failed else 0,
            reason_codes=["pull_failed"] if failed else [],
        )
        receipt["reason_codes"] = (
            ["worker_interrupted"] if unknown else ["pull_failed"] if failed else []
        )
    return _response(request, {"operation": receipt, "replayed": True})


def control_response(request, root):
    selected = mode(root)
    if selected.get("read_delay") and request["operation"] not in {
        "operation.submit",
        "operation.get",
    }:
        time.sleep(selected["read_delay"])
    state_read(root)
    with closing(sqlite3.connect(root / "synthetic-dockge.sqlite", timeout=5)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        state = json.loads(
            db.execute("SELECT document FROM fixture_state WHERE id=1").fetchone()[0]
        )
        operation, params = request["operation"], request["params"]
        stack = next((row for row in state["stacks"] if row["id"] == params.get("stack_id")), None)
        if operation == "control.catalog":
            response = _response(
                request,
                {
                    "nodes": [
                        {"id": "fixture-nas", "label": "合成 NAS", "capabilities": CAPABILITIES}
                    ]
                },
            )
        elif operation == "stack.list":
            response = _response(
                request, {"stacks": [public_stack(row) for row in state["stacks"]]}
            )
        elif operation == "stack.inspect":
            response = (
                _response(request, {"stack": public_stack(stack)})
                if stack
                else _response(request, status="unavailable", reasons=["stack_not_found"])
            )
        elif operation == "container.inspect":
            identity = params["container_id"]
            owner = next(
                (
                    row
                    for row in state["stacks"]
                    if any(item["id"] == identity for item in row["runtime"]["containers"])
                ),
                None,
            )
            protection = (
                owner["protection"]
                if owner
                else {"protected": False, "owner": None, "reason": None, "maintenance_ref": None}
            )
            value = {
                "id": identity,
                "name": "fixture-container",
                "protection": protection,
                "allowed_actions": []
                if protection["protected"]
                else ["container.start", "container.stop", "container.restart"],
                "runtime": owner["runtime"] if owner else _runtime(identity),
            }
            response = _response(request, {"container": value})
        elif operation == "stack.versions":
            response = _response(
                request,
                {
                    "versions": [
                        row["public"]
                        for row in state["versions"].values()
                        if row["public"]["stack_id"] == params["stack_id"]
                    ]
                },
            )
        elif operation == "stack.plan":
            response = _plan(request, state, selected)
        elif operation == "operation.submit":
            response = (
                _response(
                    request,
                    status=selected["reject_submit"],
                    reasons=["synthetic_explicit_refusal"],
                )
                if selected.get("reject_submit")
                else _receipt(request, state)
            )
        elif operation == "operation.get":
            response = _get(request, state, selected)
        else:
            response = _response(
                request, {"operations": [row["receipt"] for row in state["operations"].values()]}
            )
        db.execute("UPDATE fixture_state SET document=? WHERE id=1", (json.dumps(state),))
    return response


def execute(arguments):
    root = Path(arguments.directory)
    request = json.loads(sys.stdin.buffer.read())
    if request["version"] == "infrastructure-control.v1":
        with (root / "control-calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "operation": request["operation"],
                        "node_id": request["node_id"],
                        "operation_id": request["params"].get("operation_id"),
                    }
                )
                + "\n"
            )
        response = control_response(request, root)
        selected = mode(root)
        if request["operation"] == "operation.submit":
            if selected.get("submit_delay"):
                time.sleep(selected["submit_delay"])
            if selected.get("disconnect_after_submit"):
                raise SystemExit(2)
    else:
        response = response_fixture(request, root, Path(arguments.contract))
        if request["operation"] == "docker.inventory" and response.get("data"):
            state = state_read(root)
            for row in response["data"]["containers"]:
                stack = next(
                    (
                        item
                        for item in state["stacks"]
                        if any(c["id"] == row["id"] for c in item["runtime"]["containers"])
                    ),
                    None,
                )
                if stack:
                    row["name"] = stack["project_name"]
                    row["state"] = stack["runtime"]["containers"][0]["state"]
                    row["running"] = stack["runtime"]["containers"][0]["running"]
                    if row.get("compose"):
                        row["compose"]["project"] = stack["project_name"]
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


def seed_media_task_history(platform):
    """A retained synthetic ledger entry, without downloading or configuring an engine."""
    repo = platform.media.repo
    job = repo.enqueue(
        {
            "media_kind": "video",
            "title": "合成媒体任务详情（只读夹具）",
            "target_id": "synthetic-network-video",
            "bvid": "BV1xx411c7mD",
            "selected_cids": ["111111111"],
            "layout": "single",
            "quality": {"mode": "best", "quality_id": None, "allow_fallback": False},
        }
    )
    repo.update_job(
        job["job_id"],
        {
            "state": "unknown",
            "code": "media_server_unavailable",
            "library_results": [
                {
                    "server_id": "synthetic-jellyfin",
                    "kind": "jellyfin",
                    "state": "verified",
                    "code": "library_verified",
                    "item_id": "synthetic-item-1",
                },
                {
                    "server_id": "synthetic-plex",
                    "kind": "plex",
                    "state": "unavailable",
                    "code": "media_server_unavailable",
                    "item_id": None,
                },
            ],
        },
    )


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
        tempfile.TemporaryDirectory(prefix="ts206-browser-") as directory,
        patch.dict(os.environ, ENV),
    ):
        external = ExternalCatalog(Path(directory) / "external", create=True)
        settings = web_settings(
            directory,
            origin=f"http://127.0.0.1:{arguments.port}",
            static=Path(arguments.web_dist).resolve(),
        )
        settings["principals"]["admin"]["actions"].append("external.manage")
        settings["infrastructure"] = configuration(directory)
        if arguments.register_node:
            external.save(
                "infrastructure:fixture-nas",
                {
                    "node_id": "fixture-nas",
                    "label": "合成 NAS",
                    "kind": "synology",
                    "enabled": True,
                },
                {"action": "clear"},
                {"action": "clear"},
                0,
                str(uuid.uuid4()),
            )
        platform = Platform(settings)
        seed_media_task_history(platform)
        runner = web.AppRunner(create_app(platform), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", arguments.port).start()
        print("synthetic_dockge_ready", flush=True)
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
    parser.add_argument("--control-contract")
    parser.add_argument("--port", type=int, default=4837)
    parser.add_argument(
        "--web-dist",
        default=os.environ.get(
            "TS207_WEB_DIST", str(Path(__file__).resolve().parents[2] / "apps/web/dist")
        ),
    )
    parser.add_argument("--register-node", action="store_true")
    arguments = parser.parse_args()
    if arguments.executor:
        execute(arguments)
    else:
        import asyncio

        asyncio.run(serve(arguments))
