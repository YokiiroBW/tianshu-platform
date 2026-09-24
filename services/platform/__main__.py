"""Explicit local rehearsal command line; credentials come only from the server environment."""

import argparse
import asyncio
import os
import signal
import sqlite3
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import diagnostics, diagnostics_config, runtime_health
from .assets import REQUEST_LIMIT
from .auth import secret
from .contracts import Fault, canonical, loads, require
from .server import create_app
from .web_console import WebConsole
from .service import Platform, registered_credentials, validate_settings
from .transport import server_tls

# How long shutdown may spend making accepted events durable. Bounded on purpose: a stuck sink
# must not turn a stop into a hang, and the events that did not land stay a named, reconcilable
# gap rather than a claim of a clean flush.
SHUTDOWN_FLUSH_SECONDS = 5.0


def build_sink(settings):
    """Assemble the one process-wide event sink from the deployment's explicit inputs.

    With no directory configured this is the explicit development mode: events go to stderr, the
    adapter reports `non_durable`, and readiness stays red so nothing can mistake it for a
    production sink.
    """
    return diagnostics.Diagnostics(
        diagnostics_config.resolve_log_directory(settings),
        directory_bytes=diagnostics_config.resolve_log_directory_bytes(settings),
        admit_timeout=diagnostics_config.resolve_durability_timeout(settings),
        terminal_timeout=diagnostics_config.resolve_durability_timeout(settings),
    )


def install_signal_handlers(stop):
    """Ask the loop to set `stop` when the process is asked to terminate.

    Returns the signals actually installed. Where the running loop cannot take a handler (Windows
    has no `add_signal_handler`) the fallback registers a plain handler that wakes the loop from
    the main thread; a signal the platform cannot deliver is simply not claimed.
    """
    loop = asyncio.get_running_loop()
    installed = []
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, stop.set)
            installed.append(name)
            continue
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            signal.signal(number, lambda *_: loop.call_soon_threadsafe(stop.set))
            installed.append(name)
        except (ValueError, OSError, RuntimeError):
            continue
    return tuple(installed)


async def serve_forever(
    platform, sink, *, host, port, tls, install_signals=install_signal_handlers
):
    """Run the app in the foreground until asked to stop, then shut down in a fixed order.

    The order matters: stop accepting and drain in-flight work, retire the runtime so readiness
    stops presenting it as current, record the terminal event, and only then make the log durable
    and close it. Nothing is killed to look like a clean stop, and every step is bounded.
    """
    console = WebConsole(platform)
    runners = []
    try:
        runner = web.AppRunner(create_app(platform, console=console), access_log=None)
        runners.append(runner)
        await runner.setup()
        await web.TCPSite(runner, host, port, ssl_context=tls).start()
        if console.access.current is not None:
            access = console.access
            require(access.config["port"] != port, "invalid_input", 400)
            public = web.AppRunner(
                create_app(platform, console=console, public=True), access_log=None
            )
            runners.append(public)
            await public.setup()
            await web.TCPSite(
                public, access.config["host"], access.config["port"], ssl_context=access.tls
            ).start()
        stop = asyncio.Event()
        install_signals(stop)
        diagnostics.event("runtime.started", "INFO", "succeeded")
        await stop.wait()
    finally:
        diagnostics.event("runtime.stopping", "INFO", "started")
        for runner in reversed(runners):
            await runner.cleanup()
        platform.close()
        diagnostics.event("runtime.stopped", "INFO", "succeeded")
        sink.flush(SHUTDOWN_FLUSH_SECONDS)
        sink.close(SHUTDOWN_FLUSH_SECONDS)


def _preflight(settings):
    """Validate a deployment without constructing anything that could create or migrate.

    The validator is the entry point's own: the same function the real startup runs before it
    opens a store, so a settings file this preflight calls ready is one the service accepts.
    """
    return runtime_health.preflight(
        settings,
        credential_names=registered_credentials(settings),
        credential_present=lambda name: secret(name) is not None,
        validate=validate_settings,
    )


def _healthcheck(url, ca_file):
    """Ask a running container's own liveness endpoint over verified TLS.

    Liveness only, and read-only in the strictest sense: the public document, no credential of any
    kind, no admission, no event and no write. Verification is not weakened for the container's
    convenience - the CA is explicit, hostname checking stays on and the certificate chain is
    required - so a certificate that does not cover the URL's name fails the check instead of
    reporting a healthy process behind an entry point nobody can actually use.
    """
    parts = urlsplit(url)
    require(parts.scheme == "https" and bool(parts.hostname), "invalid_input", 400)
    require(isinstance(ca_file, str) and bool(ca_file), "invalid_input", 400)
    context = ssl.create_default_context(cafile=ca_file)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    with urllib.request.urlopen(url, context=context, timeout=3) as response:
        document = loads(response.read(4096))
    require(
        isinstance(document, dict) and document == {"status": "alive"},
        "dependency_unavailable",
        503,
    )
    return {"status": "alive", "checked": parts.hostname}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", help="deployment settings JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="explicit local rehearsal or authenticated TLS service"
    )
    serve.add_argument("--port", required=True, type=int)
    serve.add_argument("--host", default="127.0.0.1")
    commands.add_parser(
        "preflight", help="validate configuration, contracts and existing data without writing"
    )
    # The container healthcheck needs no deployment settings: it asks the process that is already
    # running, and it must keep working when the settings file itself is the thing under suspicion.
    check = commands.add_parser("healthcheck", help="verify a running live listener over TLS")
    check.add_argument("--url", required=True)
    check.add_argument("--ca-file", required=True)
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
    if args.command == "healthcheck":
        # Before any settings file is read: a healthcheck that needed the deployment's own
        # configuration could not report a deployment whose configuration is the problem.
        try:
            print(canonical(_healthcheck(args.url, args.ca_file)))
            return 0
        except (Fault, OSError, ssl.SSLError, ValueError, TypeError, KeyError):
            print(canonical({"code": "dependency_unavailable", "status": 503}))
            return 1
    if args.settings is None:
        print(canonical({"code": "invalid_input", "status": 400}))
        return 1
    try:
        settings = loads(Path(args.settings).read_bytes())
        require(isinstance(settings, dict), "invalid_input", 400)
    except (Fault, OSError, ValueError, TypeError, KeyError, RecursionError):
        # The settings file has to be read before a sink can exist, so this is the one failure with
        # nowhere to log to. It exits with a fixed code and no message: the path, the offending
        # byte and the exception text never reach the terminal, and nothing is started or created.
        # `loads` reports a malformed document as a `Fault`, so a settings file that is not JSON is
        # an ordinary configuration failure here rather than an uncaught traceback.
        print(canonical({"code": "config_load_failed", "status": 503}))
        return 1

    if args.command == "preflight":
        # A preflight is a pure question about a deployment: it starts nothing, creates nothing and
        # reports the safe reason codes an operator needs before a container is allowed up.
        report = _preflight(settings)
        print(canonical(report))
        return 0 if report["status"] == "ready" else 1

    sink = build_sink(settings)
    diagnostics.activate(sink)
    diagnostics.event("runtime.starting", "INFO", "started")
    try:
        platform = Platform(settings)
    except Fault as exc:
        # A deployment that cannot be assembled is recorded with a fixed code, never the message.
        diagnostics.event(
            "config.load_failed",
            "ERROR",
            "failed",
            error_code=diagnostics.safe_code(exc.code),
        )
        diagnostics.event(
            "runtime.startup_failed", "ERROR", "failed", error_code="config_load_failed"
        )
        sink.close(SHUTDOWN_FLUSH_SECONDS)
        raise
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        diagnostics.event("config.load_failed", "ERROR", "failed", error_code="config_load_failed")
        diagnostics.event(
            "runtime.startup_failed", "ERROR", "failed", error_code="config_load_failed"
        )
        sink.close(SHUTDOWN_FLUSH_SECONDS)
        print(canonical({"code": "dependency_unavailable", "status": 503}))
        return 1
    diagnostics.event("config.loaded", "INFO", "succeeded")
    if platform.contract_problem is None and platform.diagnostics_contract is not None:
        diagnostics.event("contract.loaded", "INFO", "succeeded")
    elif platform.contract_problem is not None:
        diagnostics.event(
            "contract.verify_failed",
            "ERROR",
            "failed",
            error_code=diagnostics.safe_code(platform.contract_problem),
        )

    try:
        if args.command == "serve":
            require(0 < args.port < 65536, "invalid_input", 400)
            tls = None
            if platform.auth.mode == "local_rehearsal":
                require(args.host == "127.0.0.1")
            else:
                tls = server_tls(platform.settings.get("tls"))
            asyncio.run(serve_forever(platform, sink, host=args.host, port=args.port, tls=tls))
            return 0
        if not diagnostics.cli_admit(args.action):
            # The same admission a request passes, and it sits strictly before the first side
            # effect: a refused action has not started, so nothing has to be undone and the local
            # store is untouched.
            print(canonical({"code": "dependency_unavailable", "status": 503}))
            return 1
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
        # The action's own result is what the caller gets: the terminal record is confirmed
        # durable, bounded, and a sink that cannot confirm it has already stopped admitting new
        # work rather than changing an outcome that has already happened.
        diagnostics.confirm(diagnostics.event("cli.action.finished", "INFO", "succeeded"))
        print(canonical(result))
        return 1 if action == "asset-read" and not result["ok"] else 0
    except Fault as exc:
        diagnostics.confirm(
            diagnostics.event(
                "cli.action.finished",
                "INFO",
                "failed",
                error_code=diagnostics.safe_code(exc.code),
            )
        )
        print(canonical({"code": exc.code, "status": exc.status}))
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        diagnostics.confirm(
            diagnostics.event(
                "cli.action.finished", "INFO", "failed", error_code="dependency_unavailable"
            )
        )
        print(canonical({"code": "dependency_unavailable", "status": 503}))
    finally:
        sink.flush(SHUTDOWN_FLUSH_SECONDS)
        sink.close(SHUTDOWN_FLUSH_SECONDS)
        platform.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
