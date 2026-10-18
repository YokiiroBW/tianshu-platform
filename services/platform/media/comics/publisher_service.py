"""Linux Unix-socket host for the narrowly mounted comic file writer."""

import argparse
import asyncio
import json
import os
import socket
import stat
import struct
from pathlib import Path

from aiohttp import web

from ...contracts import Fault, require
from .publisher import CHUNK_BYTES, PREFIX, PROTOCOL, PublisherStore


def peer_uid(request):
    connection = request.transport.get_extra_info("socket") if request.transport else None
    require(
        connection is not None and hasattr(socket, "SO_PEERCRED"), "comic_publisher_forbidden", 403
    )
    try:
        _, uid, _ = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    except OSError:
        raise Fault("comic_publisher_forbidden", 403) from None
    return uid


def application(store, client_uid):
    require(type(client_uid) is int and client_uid > 0, "invalid_input", 400)

    @web.middleware
    async def boundary(request, handler):
        try:
            require(peer_uid(request) == client_uid, "comic_publisher_forbidden", 403)
            return await handler(request)
        except Fault as error:
            return web.json_response({"code": error.code}, status=error.status)
        except (ValueError, TypeError, UnicodeError):
            return web.json_response({"code": "invalid_input"}, status=400)
        except OSError:
            return web.json_response({"code": "comic_storage_unavailable"}, status=503)

    async def health(request):
        return web.json_response(
            {"protocol": PROTOCOL, "status": "alive", "targets": list(store.targets)}
        )

    async def transfer(request):
        target, key = request.match_info["target"], request.match_info["key"]
        if request.method == "GET":
            value = await asyncio.to_thread(store.get, target, key)
        elif request.match_info.get("offset") is not None:
            data = await request.read()
            value = await asyncio.to_thread(
                store.append,
                target,
                key,
                int(request.match_info["offset"]),
                data,
                request.headers.get("X-Chunk-Sha256", ""),
            )
        elif request.path.endswith("/complete"):
            value = await asyncio.to_thread(store.complete, target, key)
        else:
            require(
                request.content_length is not None and request.content_length <= 8192,
                "invalid_input",
                400,
            )
            value = await asyncio.to_thread(store.prepare, target, key, await request.json())
        return web.json_response(value)

    app = web.Application(middlewares=[boundary], client_max_size=CHUNK_BYTES + 1)
    app.router.add_get(PREFIX + "/health", health)
    path = PREFIX + "/{target}/{key}"
    app.router.add_get(path, transfer)
    app.router.add_post(path, transfer)
    app.router.add_put(path + "/chunks/{offset:\\d+}", transfer)
    app.router.add_post(path + "/complete", transfer)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    args = parser.parse_args()
    require(
        os.name == "posix" and hasattr(socket, "SO_PEERCRED"), "comic_publisher_linux_required", 409
    )
    settings = json.loads(Path(args.settings).read_bytes())
    require(set(settings) == {"targets", "socket", "client_uid"}, "invalid_input", 400)
    path = Path(settings["socket"])
    require(path.is_absolute() and path.parent.is_dir(), "invalid_input", 400)
    for parent in (path.parent, *path.parent.parents):
        require(not parent.is_symlink(), "unsafe_library_path", 409)
    if path.exists() or path.is_symlink():
        require(
            stat.S_ISSOCK(path.lstat().st_mode) and path.lstat().st_uid == os.getuid(),
            "unsafe_library_path",
            409,
        )
    # The containing volume is dedicated to this socket; the main app mounts it read-only.
    os.umask(0o007)
    import fcntl

    lock_path = path.with_suffix(".lock")
    require(not lock_path.is_symlink(), "unsafe_library_path", 409)
    with lock_path.open("ab") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            os.chmod(path, 0o660)
            listener.listen(128)
            web.run_app(
                application(PublisherStore(settings["targets"]), settings["client_uid"]),
                sock=listener,
                access_log=None,
                print=None,
            )


if __name__ == "__main__":
    main()
