"""Explicit local rehearsal command line; credentials come only from the server environment."""

import argparse
import os
import sqlite3
from pathlib import Path

from aiohttp import web

from .contracts import Fault, canonical, loads, require
from .server import create_app
from .service import Platform


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="loopback only, no production mode")
    serve.add_argument("--port", required=True, type=int)
    local = commands.add_parser("local", help="authenticated local adapter; not web/QQ login")
    local.add_argument("--credential-env", required=True)
    local.add_argument(
        "action",
        choices=[
            "issue",
            "publish",
            "revoke-origin",
            "revoke-entry",
            "revoke-principal",
            "revoke-config",
            "view-config",
            "capabilities",
            "tasks",
            "prepare-mapping",
            "confirm-mapping",
            "observe-source",
            "verify-current-sources",
            "project-task",
        ],
    )
    local.add_argument("--input", help="local JSON input file; never service credentials")
    args = parser.parse_args()
    try:
        platform = Platform(loads(Path(args.settings).read_bytes()))
        if args.command == "serve":
            require(0 < args.port < 65536, "invalid_input", 400)
            web.run_app(create_app(platform), host="127.0.0.1", port=args.port, access_log=None)
            return 0
        header = "Bearer " + os.environ.get(args.credential_env, "")
        data = loads(Path(args.input).read_bytes()) if args.input else None
        action = args.action
        if action == "issue":
            require(isinstance(data, dict) and set(data) == {"entry_id"}, "invalid_input", 400)
            result = platform.origins.issue(header, data["entry_id"])
        elif action == "publish":
            result = platform.models.publish(header, data)
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
        else:
            platform.projections.project(header, data)
            result = {"projected": True, "execution_owned_by": data["owner"]}
        print(canonical(result))
        return 0
    except Fault as exc:
        print(canonical({"code": exc.code, "status": exc.status}))
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        print(canonical({"code": "dependency_unavailable", "status": 503}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
