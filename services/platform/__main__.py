"""Explicit local rehearsal command line; credentials come only from the server environment."""

import argparse
import asyncio
import os
import sqlite3
from pathlib import Path

from aiohttp import web

from .contracts import Fault, canonical, loads, require
from .assets import REQUEST_LIMIT
from .server import create_app
from .service import Platform
from .transport import server_tls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="explicit local rehearsal or authenticated TLS service"
    )
    serve.add_argument("--port", required=True, type=int)
    serve.add_argument("--host", default="127.0.0.1")
    local = commands.add_parser("local", help="authenticated local adapter; not web/QQ login")
    local.add_argument("--credential-env", required=True)
    local.add_argument(
        "action",
        choices=[
            "issue",
            "publish",
            "publish-native",
            "revoke-origin",
            "revoke-entry",
            "revoke-principal",
            "revoke-config",
            "revoke-native",
            "view-config",
            "view-native",
            "capabilities",
            "tasks",
            "prepare-mapping",
            "confirm-mapping",
            "observe-source",
            "verify-current-sources",
            "project-task",
            "register-input",
            "prepare-fanout",
            "confirm-fanout",
            "dispatch-fanout",
            "source-access",
            "asset-read",
        ],
    )
    local.add_argument("--input", help="local JSON input file; never service credentials")
    args = parser.parse_args()
    try:
        platform = Platform(loads(Path(args.settings).read_bytes()))
        if args.command == "serve":
            require(0 < args.port < 65536, "invalid_input", 400)
            tls = None
            if platform.auth.mode == "local_rehearsal":
                require(args.host == "127.0.0.1")
            else:
                tls = server_tls(platform.settings.get("tls"))
            web.run_app(
                create_app(platform),
                host=args.host,
                port=args.port,
                access_log=None,
                ssl_context=tls,
            )
            return 0
        header = "Bearer " + os.environ.get(args.credential_env, "")
        if args.action == "asset-read" and args.input:
            with Path(args.input).open("rb") as source:
                data = source.read(REQUEST_LIMIT + 1)
        else:
            data = loads(Path(args.input).read_bytes()) if args.input else None
        action = args.action
        if action == "issue":
            require(isinstance(data, dict) and set(data) == {"entry_id"}, "invalid_input", 400)
            result = platform.origins.issue(header, data["entry_id"])
        elif action == "publish":
            result = platform.models.publish(header, data)
        elif action == "publish-native":
            result = platform.models.native_publish(header, data)
        elif action == "revoke-native":
            require(isinstance(data, dict) and set(data) == {"id"}, "invalid_input", 400)
            platform.models.native_revoke(header, data["id"])
            result = {"revoked": True}
        elif action == "view-native":
            result = platform.models.view_native(header)
        elif action.startswith("revoke-"):
            require(isinstance(data, dict) and set(data) == {"id"}, "invalid_input", 400)
            kind = action.removeprefix("revoke-")
            if kind == "config":
                platform.models.revoke(header, data["id"])
            else:
                platform.origins.revoke(header, kind, data["id"])
            result = {"revoked": True}
        elif action == "view-config":
            result = platform.models.view(header)
        elif action == "capabilities":
            result = platform.projections.capabilities(header)
        elif action == "tasks":
            result = platform.projections.tasks(header)
        elif action == "prepare-mapping":
            require(
                isinstance(data, dict) and set(data) == {"kind", "request"}, "invalid_input", 400
            )
            result = {
                "ticket": platform.origins.prepare_mapping(header, data["kind"], data["request"])
            }
        elif action == "confirm-mapping":
            require(
                isinstance(data, dict) and set(data) == {"ticket", "response"}, "invalid_input", 400
            )
            platform.origins.confirm_mapping(header, data["ticket"], data["response"])
            result = {"confirmed": True}
        elif action == "observe-source":
            require(
                isinstance(data, dict) and set(data) == {"entry_id", "message_key", "tombstone"},
                "invalid_input",
                400,
            )
            platform.origins.observe_source(
                header, data["entry_id"], data["message_key"], tombstone=data["tombstone"]
            )
            result = {"observed": True, "mode": "local_rehearsal"}
        elif action == "verify-current-sources":
            result = platform.origins.verify_current_sources(header, data)
        elif action == "register-input":
            require(
                isinstance(data, dict) and set(data) == {"entry_id", "input"}, "invalid_input", 400
            )
            result = platform.sources.register_input(header, data["entry_id"], data["input"])
        elif action == "prepare-fanout":
            result = {"ticket": platform.sources.prepare_mapping(header, data)}
        elif action == "confirm-fanout":
            require(
                isinstance(data, dict) and set(data) == {"ticket", "response"}, "invalid_input", 400
            )
            platform.sources.confirm_mapping(header, data["ticket"], data["response"])
            result = {"confirmed": True, "external_response": "adapter_owned"}
        elif action == "dispatch-fanout":
            result = asyncio.run(platform.sources.dispatch(header, data))
        elif action == "source-access":
            result = platform.sources.read(header, data)
        elif action == "asset-read":
            result = asyncio.run(platform.assets.read(header, data))
        else:
            platform.projections.project(header, data)
            result = {"projected": True, "execution_owned_by": data["owner"]}
        print(canonical(result))
        return 1 if action == "asset-read" and not result["ok"] else 0
    except Fault as exc:
        print(canonical({"code": exc.code, "status": exc.status}))
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        print(canonical({"code": "dependency_unavailable", "status": 503}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
