"""Synthetic AssetLink upstream for browser tests: a real TLS recorder, synthetic data only.

The peer here is not AssetLibrary. It is an explicitly synthetic HTTPS service that speaks the
published read envelope over a real TLS socket, so the platform's real client, certificate
validation, budgets and cancellation all run unchanged. Nothing in this file is evidence about
the real product; the real joint run is a separate driver.

Scenarios are switched over a loopback-only control port so a browser test can make the peer
return a permission failure, an outage, an oversized answer or a scope mismatch on demand.
"""

import asyncio
import ssl
from urllib.parse import urlsplit

from aiohttp import web

CONNECTION = "library-a"
SECOND = "library-b"
TOKEN_A = "synthetic-ts019-asset-read-a"
TOKEN_B = "synthetic-ts019-asset-read-b"
# The synthetic peer's own control port, reached over loopback by the browser spec only.
CONTROL_PORT = 4819
PAGE = 100
ENTRIES = 137

LIBRARY_A = {
    "library_id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
    "display_name": "合成一号库",
    "availability": "online",
    "access_level": "read_only",
    "category": "documents",
}
LIBRARY_B = {
    "library_id": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
    "display_name": "合成二号库",
    "availability": "offline",
    "access_level": "read_only",
    "category": "images",
}
LIBRARY_C = {
    "library_id": "cccccccc-3333-4333-8333-cccccccccccc",
    "display_name": "合成只读照片库",
    "availability": "online",
    "access_level": "read_only",
    "category": "photos",
}
# Long Chinese names exercise wrapping and truncation; one contains a hostile-looking string that
# must stay text.
LONG_NAME = "非常长的中文目录名称用于检查换行与省略是否仍然可以键盘读完整值" * 2
HOSTILE_NAME = "<img src=x onerror=alert(1)>既有文件名.txt"


def parent_of(relative_path):
    """The peer's own parent of a relative path; an entry at the root has an empty parent."""
    return relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""


def directory(entry_id, library_id, path, name):
    return {
        "entry_id": entry_id,
        "library_id": library_id,
        "relative_path": path,
        "name": name,
        "kind": "directory",
        "content_length": None,
        "last_write_time_utc": "2026-09-17T02:00:00Z",
    }


def document(entry_id, library_id, path, name, size, modified="2026-09-17T03:04:05Z"):
    return {
        "entry_id": entry_id,
        "library_id": library_id,
        "relative_path": path,
        "name": name,
        "kind": "file",
        "content_length": str(size),
        "last_write_time_utc": modified,
    }


def library_a_entries():
    """A root with two directories and one file, then a documented subtree."""
    library_id = LIBRARY_A["library_id"]
    root = [
        directory("d-archives", library_id, "归档", "归档"),
        directory("d-long", library_id, LONG_NAME, LONG_NAME),
        document("f-readme", library_id, "说明_中文.txt", "说明_中文.txt", 2048),
        document("f-hostile", library_id, HOSTILE_NAME, HOSTILE_NAME, 17),
    ]
    archives = [
        document("f-2025", library_id, "归档/2025年度总结.md", "2025年度总结.md", 4096),
        document("f-2026", library_id, "归档/2026计划.md", "2026计划.md", 1024),
        directory("d-empty", library_id, "归档/空目录", "空目录"),
    ]
    return root, archives


def page_of(items, request):
    """Server-side bounded pagination; the cursor is the peer's own opaque token.

    The page the peer returns never exceeds the page size the client asked for, and the client
    refuses anything larger rather than silently truncating it.
    """
    cursor = request.get("cursor")
    start = 0
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.startswith("synthetic:"):
            return None, "invalid_cursor"
        try:
            start = int(cursor.split(":", 1)[1])
        except ValueError:
            return None, "invalid_cursor"
        if start < 0 or start > len(items):
            return None, "invalid_cursor"
    requested = request.get("page_size", PAGE)
    if not isinstance(requested, int) or requested < 1:
        requested = PAGE
    requested = min(requested, PAGE)
    end = start + requested
    chopped = items[start:end]
    return {
        "items": chopped,
        "next_cursor": f"synthetic:{end}" if end < len(items) else None,
    }, None


class Synthetic:
    """One synthetic peer with the published read envelope and an explicit scenario switch."""

    def __init__(self):
        self.scenario = "ready"
        self.calls = []
        self.entered = asyncio.Event()
        # Set while a request is actually being served, so a test can wait for *its own* request
        # instead of for an event an earlier request already raised.
        self.stalled = asyncio.Event()
        # Set by the control port to release a request the `stall` scenario is holding open.
        self.release = asyncio.Event()

    def entries_for(self, library_id, parent):
        """Direct children of one synthesized parent; the peer's own directory listing.

        `parent` is always an explicit string: `""` is the library root, and `None` means the
        whole authorized snapshot, which is what a search matches against.
        """
        if library_id == LIBRARY_A["library_id"]:
            root, archives = library_a_entries()
            all_items = root + archives
        elif library_id == LIBRARY_C["library_id"]:
            all_items = [
                document(
                    "f-photo",
                    LIBRARY_C["library_id"],
                    "相册/春天.jpg",
                    "春天.jpg",
                    512000,
                    "2026-08-01T10:00:00Z",
                )
            ]
        elif library_id == LIBRARY_B["library_id"]:
            all_items = [
                document("f-offline", LIBRARY_B["library_id"], "旧盘/存档.txt", "存档.txt", 8)
            ]
        else:
            all_items = []
        if parent is None:
            return all_items
        return [
            item
            for item in all_items
            if parent_of(item["relative_path"]) == parent and item["relative_path"] != parent
        ]

    def library(self, library_id):
        for item in (LIBRARY_A, LIBRARY_B, LIBRARY_C):
            if item["library_id"] == library_id:
                return item
        return None

    def error(self, request_id, code, message):
        """The peer's own published error envelope, exactly as a real peer would send it."""
        return {
            "message_type": "error",
            "request_id": request_id,
            "error": {"code": code, "message": message, "retryable": False},
        }

    def result(self, request_id, body):
        return {
            "message_type": "control.result",
            "request_id": request_id,
            "ok": True,
            "body": body,
        }

    async def handle(self, data, token):
        request_id = data.get("request_id", "unknown")
        operation, body = data.get("operation"), data.get("body") or {}
        # The peer authorizes on its own, from the credential it was sent; which connection the
        # client claims is not what decides that, and neither is any scenario this page chose.
        credential = token.removeprefix("Bearer ")
        if credential not in {TOKEN_A, TOKEN_B}:
            return 401, self.error("unknown", "unauthorized", "synthetic credential rejected")
        scenario = self.scenario
        if scenario == "stall":
            # A peer that is thinking: the request stays open until the control port releases it,
            # so a page that stops waiting is the only thing that ends this read.
            self.stalled.set()
            await self.release.wait()
            return 200, self.result(request_id, {"items": [LIBRARY_A], "next_cursor": None})
        if scenario == "outage":
            # A document-less 503: the platform's own client turns this into one dependency
            # failure, and never into an empty listing.
            return 503, None
        if scenario == "deadline":
            return 504, self.error(request_id, "deadline_exceeded", "synthetic deadline")
        if scenario == "denied":
            return 403, self.error(request_id, "forbidden", "synthetic scope denial")
        if scenario == "revoked":
            return 404, self.error(request_id, "not_found", "synthetic absent-or-invisible")
        if scenario == "credential_rejected":
            return 401, self.error("unknown", "unauthorized", "synthetic credential revoked")
        if scenario == "unavailable":
            return 503, self.error("unknown", "dependency_unavailable", "synthetic unavailability")
        if scenario == "peer_vocabulary":
            return 409, self.error(request_id, "synthetic_new_word", "synthetic unknown code")
        if scenario == "oversized":
            # Real bytes above the platform's streaming response budget, so the client's own
            # `Content-Length` pre-check and its bounded stream both have something to refuse.
            return 200, self.result(request_id, {"items": [LIBRARY_A], "padding": "x" * 1_200_000})
        if scenario == "malformed":
            return 200, {
                "message_type": "control.result",
                "request_id": request_id,
                "ok": True,
                "body": {"items": [{"name": "no identifier"}], "next_cursor": None},
            }
        if scenario == "no_libraries":
            return 200, self.result(request_id, {"items": [], "next_cursor": None})
        if scenario == "not_an_operation":
            return 200, self.result(request_id, {"items": [LIBRARY_A], "next_cursor": None})
        if operation == "libraries.list":
            items = [LIBRARY_A]
            if scenario == "offline":
                # The peer still authorizes this index and reports its own index state; an
                # offline index is not a missing file and not a failed read.
                items = [LIBRARY_B]
            if scenario == "many_libraries":
                items = [LIBRARY_A, LIBRARY_C]
            return 200, self.result(request_id, {"items": items, "next_cursor": None})
        if operation == "libraries.get":
            item = self.library(body.get("library_id"))
            if item is None:
                return 404, self.error(request_id, "not_found", "synthetic absent library")
            return 200, self.result(request_id, {"library": item})
        if operation == "entries.browse":
            library = self.library(body.get("library_id"))
            if library is None:
                return 404, self.error(request_id, "not_found", "synthetic absent library")
            parent = body.get("parent_relative_path", "")
            items = self.entries_for(library["library_id"], parent)
            if scenario == "offline":
                # An authorized snapshot of an offline library: real indexed rows, offline index.
                parent = "旧盘"
                items = self.entries_for(LIBRARY_B["library_id"], parent)
            if scenario == "crossed":
                # A peer that answered with another library's entry must never reach the page.
                items = [document("f-other", LIBRARY_C["library_id"], "他库.txt", "他库.txt", 3)]
            payload, error = page_of(items, body)
            if error:
                return 400, self.error(request_id, "invalid_input", "synthetic cursor")
            payload.update(library=library, parent_relative_path=parent)
            return 200, self.result(request_id, payload)
        if operation == "entries.get":
            library = self.library(body.get("library_id"))
            entry = next(
                (
                    item
                    for item in self.entries_for(body.get("library_id"), None)
                    if item["entry_id"] == body.get("entry_id")
                ),
                None,
            )
            if library is None or entry is None:
                return 404, self.error(request_id, "not_found", "synthetic absent entry")
            if scenario == "crossed":
                entry = document("f-other", LIBRARY_C["library_id"], "他库.txt", "他库.txt", 3)
            return 200, self.result(request_id, {"library": library, "entry": entry})
        if operation == "assets.search":
            query = str(body.get("query", ""))
            scope = body.get("scope", "all")
            found = []
            for library in (LIBRARY_A, LIBRARY_C):
                if scope == "library" and library["library_id"] != body.get("library_id"):
                    continue
                for item in self.entries_for(library["library_id"], None):
                    if query.lower() in item["name"].lower():
                        found.append({"library": library, "entry": item, "hit_reason": "name"})
            if scenario == "crossed" and body.get("library_id"):
                found = [
                    {
                        "library": LIBRARY_C,
                        "entry": document(
                            "f-other", LIBRARY_C["library_id"], "他库.txt", "他库.txt", 3
                        ),
                        "hit_reason": "name",
                    }
                ]
            if scenario == "bad_hit":
                found = [
                    {
                        "library": LIBRARY_A,
                        "entry": document(
                            "f-readme", LIBRARY_A["library_id"], "说明_中文.txt", "说明_中文.txt", 1
                        ),
                        "hit_reason": "similarity",
                    }
                ]
            return 200, self.result(request_id, {"items": found, "next_cursor": None})
        # Anything outside the five published reads never reaches the peer's read protocol.
        return 403, self.error(request_id, "forbidden", "synthetic non-read operation")


def control_app(peer):
    app = web.Application()

    async def switch(request):
        body = await request.json()
        peer.scenario = str(body.get("scenario", "ready"))
        if body.get("release"):
            peer.release.set()
        return web.json_response({"scenario": peer.scenario, "released": peer.release.is_set()})

    app.router.add_post("/scenario", switch)
    return app


def asset_app(peer):
    app = web.Application()

    async def control(request):
        for name in request.headers:
            lowered = name.lower()
            assert not lowered.startswith("sec-fetch-"), lowered
            assert lowered not in {"cookie", "origin", "x-assetlibrary-csrf"}, lowered
        data = await request.json()
        token = request.headers.get("Authorization", "")
        peer.calls.append(
            {
                "operation": data.get("operation"),
                "body": data.get("body"),
                "token": "a" if token.endswith(TOKEN_A) else "b",
            }
        )
        peer.entered.set()
        status, document = await peer.handle(data, token)
        if document is None:
            # A transport-level failure with no protocol document at all.
            return web.Response(status=status, text="synthetic peer outage")
        return web.json_response(document, status=status)

    app.router.add_post("/assetlink/v1/control", control)
    return app


def server_tls(certificate, private_key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private_key)
    return context


async def start(peer, certificate, private_key, port, control_port=CONTROL_PORT):
    """Serve the synthetic peer over real TLS, plus its own loopback scenario control port."""
    runner = web.AppRunner(asset_app(peer), access_log=None)
    await runner.setup()
    await web.TCPSite(
        runner, "127.0.0.1", port, ssl_context=server_tls(certificate, private_key)
    ).start()
    control = web.AppRunner(control_app(peer), access_log=None)
    await control.setup()
    await web.TCPSite(control, "127.0.0.1", control_port).start()
    return runner, control


def endpoint(port):
    return f"https://127.0.0.1:{port}/assetlink/v1/control"


def authority(port):
    return urlsplit(endpoint(port)).netloc
