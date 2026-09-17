"""The read-only asset page: real TLS peer, same-origin console, no browser authority.

The synthetic peer is a real HTTPS service speaking the published read envelope, so the platform's
real client, certificate validation, budgets and cancellation are exercised unchanged. It is not
AssetLibrary; the real implementation is covered by the separate joint driver.

What the browser may do here is a whitelist of reads plus identifiers and a query. Everything that
could reach a peer on its own — a credential, an endpoint, a physical path, an administrator claim
— is refused, and the reading identity is resolved from deployment settings.
"""

import asyncio
import copy
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp

from asset_fixtures import (
    CONNECTION,
    LIBRARY_A,
    LIBRARY_B,
    LIBRARY_C,
    LONG_NAME,
    SECOND,
    TOKEN_A,
    TOKEN_B,
    Synthetic,
    library_a_entries,
    start,
)
from fixtures import ENV, ROOT, start_http
from services.platform.contracts import Fault
from services.platform.server import create_app
from services.platform.service import Platform
from services.platform.web_console import WebConsole
from web_fixtures import PASSWORD, web_settings

ASSET_ENV = {
    "TS019_ASSET_READ": "synthetic-ts019-platform-asset-read",
    "TS019_ASSET_A": TOKEN_A,
    "TS019_ASSET_B": TOKEN_B,
}


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def asset_settings(directory, url, ca):
    """Console settings plus a checked-in read-only asset page over the synthetic peer."""
    c = web_settings(directory)
    c["principals"]["assetreader"] = {
        "kind": "service",
        "service": "platform",
        "token_env": "TS019_ASSET_READ",
        "actions": ["asset.read"],
        "asset_connections": [CONNECTION, SECOND],
    }
    c["asset_connections"] = {
        CONNECTION: {
            "endpoint": url + "/assetlink/v1/control",
            "ca_file": ca,
            "token_env": "TS019_ASSET_A",
        },
        SECOND: {
            "endpoint": url + "/assetlink/v1/control",
            "ca_file": ca,
            "token_env": "TS019_ASSET_B",
        },
    }
    c["web_assets"] = {
        "enabled": True,
        "principal": "assetreader",
        "allowed_connections": [CONNECTION],
    }
    return c


class AssetPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {**ENV, **ASSET_ENV})
        env.start()
        self.addCleanup(env.stop)
        subprocess.run(
            [
                os.environ["TS013_TLS_PYTHON"],
                str(ROOT / "tests/backend/make_tls_fixture.py"),
                self.temp.name,
            ],
            check=True,
            capture_output=True,
        )
        self.ca = str(Path(self.temp.name) / "localhost.pem")
        self.key = str(Path(self.temp.name) / "localhost-key.pem")
        self.peer = Synthetic()
        self.asset_port, self.control_port = free_port(), free_port()
        for runner in await start(self.peer, self.ca, self.key, self.asset_port, self.control_port):
            self.addAsyncCleanup(runner.cleanup)
        self.settings = asset_settings(
            self.temp.name, f"https://127.0.0.1:{self.asset_port}", self.ca
        )
        await self.boot(self.settings)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    async def boot(self, config):
        self.config = config
        self.platform = Platform(config)
        self.app = create_app(self.platform)
        self.runner, self.url = await start_http(self.app)
        self.addAsyncCleanup(self.runner.cleanup)
        config["web"]["origin"] = self.url
        return self.url

    # ------------------------------------------------------------- harness

    async def call(self, path, body, csrf, expected=200, session=None, **headers):
        client = session or self.client
        async with client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf, **headers},
        ) as response:
            data = await response.json()
            self.assertEqual(response.status, expected, data)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return data

    async def current(self, client=None):
        async with (client or self.client).get(self.url + "/api/web/session") as response:
            return response.status, await response.json()

    async def login(self, client=None):
        _, anonymous = await self.current(client)
        return await self.call(
            "login",
            {"username": "synthetic-admin", "password": PASSWORD},
            anonymous["csrf"],
            session=client,
        )

    async def page(self, logged, path, body=None, expected=200, session=None, **headers):
        return await self.call(
            f"assets/{path}", body or {}, logged["csrf"], expected, session=session, **headers
        )

    async def connect(self, logged, connection_id=CONNECTION, **kwargs):
        return await self.page(logged, "connection", {"connection_id": connection_id}, **kwargs)

    def last(self):
        return self.peer.calls[-1]

    def platform_console(self):
        """The very adapter the running app assembled, for its own bounded read record."""
        return next(
            handler.__self__
            for route in self.app.router.routes()
            if isinstance(getattr((handler := route.handler), "__self__", None), WebConsole)
        )

    # ------------------------------------------------------------- local scope

    async def test_unauthenticated_page_is_refused_without_touching_peer(self):
        _, anonymous = await self.current()
        for path in ("state", "connection", "libraries", "browse", "search", "entry"):
            await self.call(f"assets/{path}", {}, anonymous["csrf"], 401)
        self.assertEqual(self.peer.calls, [])

    async def test_page_offers_only_declared_connections(self):
        logged = await self.login()
        state = await self.page(logged, "state", {})
        self.assertTrue(state["available"])
        self.assertEqual(state["code"], "ready")
        self.assertIsNone(state["connection"])
        # `library-b` is readable by the identity but not offered by this page.
        self.assertEqual([item["connection_id"] for item in state["connections"]], [CONNECTION])
        self.assertFalse(state["preview"]["available"])
        self.assertEqual(state["preview"]["code"], "asset_media_port_absent")
        await self.connect(logged, SECOND, expected=403)
        self.assertEqual(self.peer.calls, [])

    async def test_state_reads_never_change_the_chosen_connection(self):
        """A page asking where it stands must not be able to lose the scope it already has.

        Choosing is `connection`; reading is `state`. The two are separate operations precisely so
        that a page which reads its state before every read cannot clear its own session scope —
        the failure mode that would leave a page permanently reporting "no connection chosen"
        while the peer was in fact authorized and answering.
        """
        logged = await self.login()
        self.assertIsNone((await self.page(logged, "state", {}))["connection"])
        chosen = await self.connect(logged)
        self.assertEqual(chosen["connection"], CONNECTION)
        for _ in range(3):
            again = await self.page(logged, "state", {})
            self.assertEqual(again["connection"], CONNECTION)
        # The listing still reads, because the scope the state reads reported is still in force.
        listing = await self.page(logged, "libraries", {"page_size": 10})
        self.assertTrue(listing["libraries"])
        reads = len(self.peer.calls)
        # `state` accepts no body at all: it is not a second way to choose.
        await self.page(logged, "state", {"connection_id": SECOND}, expected=400)
        self.assertEqual((await self.page(logged, "state", {}))["connection"], CONNECTION)
        self.assertEqual(len(self.peer.calls), reads)
        # Dropping the choice is still possible, and is still explicit.
        dropped = await self.page(logged, "connection", {})
        self.assertIsNone(dropped["connection"])
        await self.page(logged, "libraries", {}, expected=403)
        self.assertEqual(len(self.peer.calls), reads)

    async def test_reads_require_a_chosen_connection(self):
        logged = await self.login()
        for path, body in (
            ("libraries", {}),
            ("browse", {"library_id": LIBRARY_A["library_id"]}),
            ("search", {"query": "说明", "scope": "all"}),
            ("entry", {"library_id": LIBRARY_A["library_id"], "entry_id": "f-readme"}),
        ):
            await self.page(logged, path, body, expected=403)
        self.assertEqual(self.peer.calls, [])

    async def test_scope_is_the_session_s_own_choice(self):
        logged = await self.login()
        state = await self.connect(logged)
        self.assertEqual(state["connection"], CONNECTION)
        # The body cannot carry a connection for a read; only the session scope is consulted.
        await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "connection_id": SECOND},
            expected=400,
        )
        self.assertEqual(self.peer.calls, [])

    async def test_revoked_session_keeps_no_result(self):
        logged = await self.login()
        await self.connect(logged)
        listing = await self.page(logged, "libraries", {"page_size": 10})
        self.assertEqual(listing["libraries"][0]["library_id"], LIBRARY_A["library_id"])
        await self.call("logout", {}, logged["csrf"])
        await self.page(logged, "libraries", {"page_size": 10}, expected=401)
        # A fresh login starts with no connection scope at all: nothing is remembered for it.
        again = await self.login()
        await self.page(again, "libraries", {"page_size": 10}, expected=403)

    async def test_page_is_absent_and_old_settings_keep_working(self):
        # A deployment that never declares `web_assets` keeps its old policy digest and behaviour:
        # there is no page, and the refusal is honest rather than an empty listing.
        static = Path(self.temp.name) / "old-static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>old deployment</title>")
        old = web_settings(self.temp.name, static=static)
        self.assertNotIn("web_assets", old)
        console = WebConsole(Platform(old))
        self.assertIsNone(console.assets.config)
        self.assertFalse(console.assets.available())
        self.assertEqual(console.assets.connections(), [])
        with self.assertRaises(Fault) as raised:
            await console.assets.route("/api/web/assets/libraries", {}, {"assets": None})
        self.assertEqual((raised.exception.code, raised.exception.status), ("assets_disabled", 403))

    async def test_unavailable_local_authority_is_immediate_and_recovers(self):
        """The page never turns a local authority problem into a silent empty listing."""
        logged = await self.login()
        await self.connect(logged)
        # A connection this deployment does not declare is refused here, before any read: the
        # page never asks the peer on behalf of a connection the console did not choose.
        with patch.object(self.platform.assets, "connections", {}):
            started = asyncio.get_running_loop().time()
            await self.page(logged, "libraries", expected=403)
            self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        self.assertEqual(self.peer.calls, [])
        recovered = await self.page(logged, "libraries")
        self.assertTrue(recovered["libraries"])

    # ------------------------------------------------------------- reads

    async def test_libraries_browse_and_detail_are_real_peer_reads(self):
        logged = await self.login()
        await self.connect(logged)
        listing = await self.page(logged, "libraries")
        self.assertEqual(self.last()["operation"], "libraries.list")
        self.assertEqual(
            [item["display_name"] for item in listing["libraries"]], [LIBRARY_A["display_name"]]
        )
        library = listing["libraries"][0]
        self.assertEqual(library["category"], "documents")
        self.assertTrue(library["index_available"])
        # The index being online is never presented as the original being available.
        self.assertEqual(library["original_available"], "not_verified")

        browse = await self.page(logged, "browse", {"library_id": LIBRARY_A["library_id"]})
        self.assertEqual(self.last()["operation"], "entries.browse")
        self.assertEqual(browse["library"]["library_id"], LIBRARY_A["library_id"])
        self.assertEqual([item["name"] for item in browse["entries"]][:2], ["归档", LONG_NAME])
        self.assertTrue(browse["entries"][0]["directory"])
        self.assertEqual(browse["entries"][2]["content_length"], "2048")

        detail = await self.page(
            logged,
            "entry",
            {"library_id": LIBRARY_A["library_id"], "entry_id": "f-readme"},
        )
        self.assertEqual(detail["entry"]["name"], "说明_中文.txt")
        self.assertFalse(detail["entry"]["preview"]["available"])
        self.assertEqual(detail["entry"]["original_available"], "not_verified")

    async def test_only_the_fields_the_browser_sent_reach_the_peer(self):
        logged = await self.login()
        await self.connect(logged)
        await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "page_size": 25},
        )
        self.assertEqual(
            self.last()["body"],
            {
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "",
                "page_size": 25,
            },
        )
        await self.page(
            logged,
            "browse",
            {
                "library_id": LIBRARY_A["library_id"],
                "sort_by": "size",
                "sort_direction": "desc",
                "kind": "directories",
                "name_filter": "归档",
            },
        )
        self.assertEqual(self.last()["body"]["sort_by"], "size")
        self.assertEqual(self.last()["body"]["name_filter"], "归档")
        await self.page(
            logged,
            "search",
            {"query": "说明_中文", "scope": "library", "library_id": LIBRARY_A["library_id"]},
        )
        # The literal query is forwarded unchanged: the page tests for no escape sequence of its
        # own, and rewrites nothing the peer would otherwise match.
        self.assertEqual(self.last()["body"]["query"], "说明_中文")
        self.assertEqual(self.last()["body"]["scope"], "library")

    async def test_search_is_explicit_and_scope_is_validated(self):
        logged = await self.login()
        await self.connect(logged)
        await self.page(logged, "search", {"query": "", "scope": "all"}, expected=400)
        await self.page(logged, "search", {"query": "   ", "scope": "all"}, expected=400)
        await self.page(logged, "search", {"query": "x" * 201, "scope": "all"}, expected=400)
        await self.page(
            logged,
            "search",
            {"query": "说明", "scope": "all", "library_id": LIBRARY_A["library_id"]},
            expected=400,
        )
        await self.page(logged, "search", {"query": "说明", "scope": "directory"}, expected=400)
        await self.page(logged, "search", {"query": "说明", "scope": "library"}, expected=400)
        await self.page(logged, "search", {"query": "说明", "scope": "everything"}, expected=400)
        await self.page(logged, "search", {"query": "说\n明", "scope": "all"}, expected=400)
        self.assertEqual(self.peer.calls, [])
        hits = await self.page(logged, "search", {"query": "说明_中文", "scope": "all"})
        self.assertEqual([hit["entry"]["name"] for hit in hits["hits"]], ["说明_中文.txt"])
        self.assertEqual(hits["hits"][0]["hit_reason"], "name")

    async def test_relative_paths_only_and_identifier_bounds(self):
        logged = await self.login()
        await self.connect(logged)
        for parent in ("/etc", "C:\\Windows", "..", "归档/../..", "归档\x00", "x" * 4097):
            await self.page(
                logged,
                "browse",
                {"library_id": LIBRARY_A["library_id"], "parent_relative_path": parent},
                expected=400,
            )
        for size in (0, 101, True, "50"):
            await self.page(logged, "libraries", {"page_size": size}, expected=400)
        await self.page(logged, "entry", {"library_id": LIBRARY_A["library_id"]}, expected=400)
        await self.page(logged, "entry", {"library_id": "", "entry_id": "x"}, expected=400)
        await self.page(logged, "libraries", {"library_id": LIBRARY_A["library_id"]}, expected=400)
        self.assertEqual(self.peer.calls, [])
        inside = await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "parent_relative_path": "归档"},
        )
        self.assertEqual(
            [item["name"] for item in inside["entries"]],
            ["2025年度总结.md", "2026计划.md", "空目录"],
        )
        # An empty directory is its own state: a real, successful, empty listing.
        empty = await self.page(
            logged,
            "browse",
            {
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "归档/空目录",
            },
        )
        self.assertEqual(empty["entries"], [])
        self.assertEqual(empty["page"]["returned"], 0)
        self.assertFalse(empty["page"]["has_more"])
        self.assertIsNone(empty["page"]["next_cursor"])

    async def test_directory_scope_search_sends_the_reviewed_parent(self):
        logged = await self.login()
        await self.connect(logged)
        await self.page(
            logged,
            "search",
            {
                "query": "计划",
                "scope": "directory",
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "归档",
            },
        )
        self.assertEqual(self.last()["body"]["scope"], "directory")
        self.assertEqual(self.last()["body"]["parent_relative_path"], "归档")

    async def test_unknown_reads_are_not_forwarded(self):
        logged = await self.login()
        await self.connect(logged)
        await self.page(logged, "not-a-port", {}, expected=404)
        self.assertEqual(self.peer.calls, [])

    # ------------------------------------------------------------- paging

    async def test_page_is_bounded_and_continuation_is_forwarded_verbatim(self):
        logged = await self.login()
        await self.connect(logged)
        # The listing is real and whole; the bound is the page size the browser asked for, and a
        # page that would need truncating is refused rather than silently shortened.
        first = await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "parent_relative_path": "", "page_size": 2},
        )
        self.assertEqual(first["page"]["size"], 2)
        self.assertEqual(first["page"]["returned"], 2)
        self.assertTrue(first["page"]["has_more"])
        cursor = first["page"]["next_cursor"]
        # The continuation is this page's own opaque value, and the peer gets its token back.
        self.assertNotIn("synthetic", cursor)
        second = await self.page(
            logged,
            "browse",
            {
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "",
                "page_size": 2,
                "cursor": cursor,
            },
        )
        self.assertEqual(self.last()["body"]["cursor"], "synthetic:2")
        expected = [item["entry_id"] for item in library_a_entries()[0]]
        self.assertEqual(
            [item["entry_id"] for item in first["entries"] + second["entries"]], expected
        )

    async def test_authorized_library_list_pages_and_forwards_its_own_continuation(self):
        logged = await self.login()
        await self.connect(logged)
        # More authorized libraries than one page holds. The library list is the only place a
        # library can be opened from, so a continuation the page cannot follow is a set of libraries
        # nothing can reach: the page must be able to read on from here exactly as it can inside a
        # directory.
        self.peer.scenario = "paged_libraries"
        first = await self.page(logged, "libraries")
        self.assertEqual(len(first["libraries"]), 50)
        self.assertEqual(first["page"]["returned"], 50)
        self.assertTrue(first["page"]["has_more"])
        cursor = first["page"]["next_cursor"]
        self.assertTrue(cursor)
        # The continuation is this page's own opaque value, not the peer's token.
        self.assertNotIn("synthetic", cursor)
        self.assertNotIn("cursor", self.last()["body"])

        second = await self.page(logged, "libraries", {"cursor": cursor})
        self.assertEqual(self.last()["body"]["cursor"], "synthetic:50")
        self.assertEqual(len(second["libraries"]), 50)
        self.assertTrue(second["page"]["has_more"])
        third = await self.page(logged, "libraries", {"cursor": second["page"]["next_cursor"]})
        self.assertEqual(len(third["libraries"]), 37)
        self.assertFalse(third["page"]["has_more"])
        self.assertIsNone(third["page"]["next_cursor"])
        seen = [item["library_id"] for page in (first, second, third) for item in page["libraries"]]
        self.assertEqual(len(seen), 137)
        self.assertEqual(len(set(seen)), 137)

    async def test_library_continuation_is_bound_to_the_listing_that_made_it(self):
        logged = await self.login()
        await self.connect(logged)
        self.peer.scenario = "paged_libraries"
        first = await self.page(logged, "libraries")
        cursor = first["page"]["next_cursor"]
        # A continuation belongs to one listing of one connection: a directory listing's cursor is a
        # different query, so it is refused rather than answered with a page of something else.
        listing = await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "parent_relative_path": "", "page_size": 1},
        )
        await self.page(
            logged, "libraries", {"cursor": listing["page"]["next_cursor"]}, expected=409
        )
        # The library list's own continuation still continues the listing it came from.
        second = await self.page(logged, "libraries", {"cursor": cursor})
        self.assertEqual(len(second["libraries"]), 50)

    async def test_large_directory_pages_and_binds_its_cursor_to_the_query(self):
        logged = await self.login()
        await self.connect(logged)
        root = await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "parent_relative_path": ""},
        )
        self.assertEqual(len(root["entries"]), 4)
        self.assertFalse(root["page"]["has_more"])
        # The subtree listing is served by the peer's own bounded pagination.
        listing = await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "parent_relative_path": "归档", "page_size": 1},
        )
        self.assertEqual(len(listing["entries"]), 1)
        self.assertTrue(listing["page"]["has_more"])
        cursor = listing["page"]["next_cursor"]
        self.assertTrue(cursor)
        # The cursor is this page's own opaque value, not the peer's.
        self.assertNotIn("synthetic", cursor)
        # A first page never sends a continuation: there is nothing to continue from.
        self.assertNotIn("cursor", self.last()["body"])
        await self.page(
            logged,
            "browse",
            {
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "归档",
                "page_size": 1,
                "cursor": cursor,
            },
        )

    async def test_cursor_is_bound_to_connection_library_scope_and_query(self):
        logged = await self.login()
        await self.connect(logged)
        first = await self.page(
            logged,
            "browse",
            {"library_id": LIBRARY_A["library_id"], "parent_relative_path": "归档", "page_size": 1},
        )
        cursor = first["page"]["next_cursor"]
        # A different library is a different query, not a continuation of this one.
        await self.page(
            logged,
            "browse",
            {
                "library_id": LIBRARY_C["library_id"],
                "parent_relative_path": "归档",
                "page_size": 1,
                "cursor": cursor,
            },
            expected=409,
        )
        # So is a different parent, a different sort, a different filter or a different scope.
        for body in (
            {"library_id": LIBRARY_A["library_id"], "parent_relative_path": "", "page_size": 1},
            {
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "归档",
                "page_size": 1,
                "sort_by": "size",
            },
            {
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "归档",
                "page_size": 1,
                "kind": "directories",
            },
            {
                "library_id": LIBRARY_A["library_id"],
                "parent_relative_path": "归档",
                "page_size": 1,
                "name_filter": "计划",
            },
        ):
            await self.page(logged, "browse", {**body, "cursor": cursor}, expected=409)
        await self.page(
            logged,
            "search",
            {"query": "说明", "scope": "all", "cursor": cursor},
            expected=409,
        )
        # A search cursor belongs to that query and only that query.
        hits = await self.page(logged, "search", {"query": "说明_中文", "scope": "all"})
        hits = await self.page(logged, "search", {"query": "说明", "scope": "all"})
        self.assertEqual(hits["page"]["has_more"], False)
        for value in ("not-base64!", "e30", ""):
            await self.page(
                logged,
                "browse",
                {"library_id": LIBRARY_A["library_id"], "cursor": value},
                expected=400,
            )

    # ------------------------------------------------------------- failures

    async def test_peer_failures_are_distinct_and_never_echo_its_message(self):
        logged = await self.login()
        await self.connect(logged)
        for scenario, status, code in (
            ("denied", 403, "forbidden"),
            ("revoked", 404, "not_found"),
            ("credential_rejected", 401, "unauthorized"),
            ("unavailable", 503, "dependency_unavailable"),
            ("deadline", 504, "deadline_exceeded"),
            ("oversized", 413, "budget_exceeded"),
            # A peer code this product does not own is never repeated to the browser.
            ("peer_vocabulary", 502, "upstream_error"),
        ):
            self.peer.scenario = scenario
            failure = await self.page(logged, "libraries", expected=status)
            self.assertEqual(failure["code"], code)
            # The peer's own message never reaches the page, in any field.
            self.assertNotIn("synthetic", str(failure))
        # A peer that answers with something other than the read protocol at all is one honest
        # protocol failure, not an empty directory and not a fabricated dependency state.
        self.peer.scenario = "outage"
        outage = await self.page(logged, "libraries", expected=502)
        self.assertEqual(outage["code"], "invalid_upstream")
        self.peer.scenario = "ready"
        self.assertTrue((await self.page(logged, "libraries"))["libraries"])

    async def test_unusable_peer_answer_is_reported_not_rendered(self):
        logged = await self.login()
        await self.connect(logged)
        # A listing the page cannot read as the vocabulary it asked for.
        self.peer.scenario = "malformed"
        await self.page(logged, "libraries", expected=502)
        # A hit whose reason is not one of the peer's own two reasons.
        self.peer.scenario = "bad_hit"
        await self.page(logged, "search", {"query": "说明", "scope": "all"}, expected=502)
        # A browse answer carrying another library's entry is refused rather than mixed in.
        self.peer.scenario = "crossed"
        await self.page(logged, "browse", {"library_id": LIBRARY_A["library_id"]}, expected=502)
        detail = await self.page(
            logged,
            "entry",
            {"library_id": LIBRARY_A["library_id"], "entry_id": "f-readme"},
            expected=502,
        )
        self.assertEqual(detail["code"], "invalid_upstream")
        # A search the page scoped to one library never accepts another library's hits.
        self.peer.scenario = "ready"
        scoped = await self.page(
            logged,
            "search",
            {"query": "说明_中文", "scope": "library", "library_id": LIBRARY_A["library_id"]},
        )
        self.assertEqual([hit["entry"]["name"] for hit in scoped["hits"]], ["说明_中文.txt"])
        self.peer.scenario = "crossed"
        await self.page(
            logged,
            "search",
            {"query": "说明_中文", "scope": "library", "library_id": LIBRARY_A["library_id"]},
            expected=502,
        )

    async def test_offline_index_is_reported_as_its_own_state(self):
        """The peer's own index state is repeated as a state, never as missing files."""
        config = copy.deepcopy(self.settings)
        config["web_assets"]["allowed_connections"] = [CONNECTION, SECOND]
        config["web"]["origin"] = self.url
        console = WebConsole(Platform(config))
        # The page's own scope, exactly as the route sets one: an allowed connection, and a
        # deadline that has not passed.
        session = {"assets": {"connection_id": SECOND, "expires": console.clock() + 60}}
        self.assertEqual(console.assets.scope(session), SECOND)
        self.peer.scenario = "offline"
        state = await console.assets.route("/api/web/assets/libraries", {"page_size": 10}, session)
        item = state["libraries"][0]
        self.assertEqual(item["library_id"], LIBRARY_B["library_id"])
        self.assertEqual(item["availability"], "offline")
        self.assertFalse(item["index_available"])
        # An offline index keeps its authorized snapshot; the originals stay unverified, and the
        # page never presents an offline index as a missing directory.
        self.assertEqual(item["original_available"], "not_verified")
        page = await console.assets.route(
            "/api/web/assets/browse",
            {"library_id": LIBRARY_B["library_id"], "parent_relative_path": "旧盘"},
            session,
        )
        self.assertEqual([entry["name"] for entry in page["entries"]], ["存档.txt"])
        self.assertEqual(page["library"]["availability"], "offline")
        # An unavailable connection never becomes a page state either way.
        self.peer.scenario = "outage"
        with self.assertRaises(Fault) as raised:
            await console.assets.route("/api/web/assets/libraries", {}, session)
        self.assertEqual(
            (raised.exception.code, raised.exception.status), ("invalid_upstream", 502)
        )

    async def test_cancellation_is_recorded_and_delivers_nothing(self):
        """The page's own read lifecycle: a stopped read is recorded as stopped, not as a result.

        Cancellation is exercised on the exact coroutine the route runs, because a cancelled
        *client* is a transport concern: what belongs to this page is that its own read ends
        without a result, and that no late answer can refill it.
        """
        logged = await self.login()
        await self.connect(logged)
        console = self.platform_console()
        session = {"assets": {"connection_id": CONNECTION, "expires": console.clock() + 60}}
        self.peer.scenario = "stall"
        self.addCleanup(self.peer.release.set)
        task = asyncio.create_task(console.assets.route("/api/web/assets/libraries", {}, session))
        for _ in range(200):
            if self.peer.stalled.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.peer.stalled.is_set(), "the page never reached the stalling peer")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            [row["code"] for row in console.assets.history(CONNECTION)],
            ["cancelled"],
        )
        # Releasing the peer afterwards changes nothing: the late answer is dropped, and no page
        # was ever delivered for it.
        self.peer.release.set()
        self.peer.scenario = "ready"
        await asyncio.sleep(0.1)
        self.assertEqual(
            [row["code"] for row in console.assets.history(CONNECTION)],
            ["cancelled"],
        )
        recovered = await self.page(logged, "libraries")
        self.assertTrue(recovered["libraries"])

    async def test_reads_are_recorded_without_content(self):
        logged = await self.login()
        await self.connect(logged)
        await self.page(logged, "libraries")
        rows = self.platform_console().assets.history(CONNECTION)
        self.assertEqual(rows[0]["operation"], "libraries.list")
        self.assertEqual(rows[0]["code"], "ok")
        self.assertNotIn("说明", str(rows))
        self.assertNotIn(TOKEN_A, str(rows))

    # ------------------------------------------------------------- configuration

    async def test_configuration_is_narrowing_only(self):
        from services.platform.web_asset_queries import page_configuration

        def check(section, config, principals):
            return page_configuration(
                section, principals, config["asset_connections"], lambda *_: None
            )

        # This section is the only place a connection is chosen for the page, and it can only ever
        # narrow: the reading identity's own bindings are the ceiling.
        self.assertEqual(
            check(
                self.settings["web_assets"],
                self.settings,
                self.settings["principals"],
            )[2],
            (CONNECTION,),
        )
        # A connection the reading identity is not bound to is never granted by the page.
        broken = copy.deepcopy(self.settings)
        broken["principals"]["assetreader"]["asset_connections"] = [CONNECTION]
        broken["web_assets"]["allowed_connections"] = [SECOND]
        with self.assertRaises(Fault):
            check(broken["web_assets"], broken, broken["principals"])
        # An undeclared connection is not a connection at all.
        broken = copy.deepcopy(self.settings)
        broken["web_assets"]["allowed_connections"] = ["never-declared"]
        with self.assertRaises(Fault):
            check(broken["web_assets"], broken, broken["principals"])
        # An operator's browser identity can never be the asset reader.
        broken = copy.deepcopy(self.settings)
        broken["web_assets"]["principal"] = "admin"
        with self.assertRaises(Fault):
            check(broken["web_assets"], broken, broken["principals"])
        # A service identity without `asset.read` is not a reader either.
        broken = copy.deepcopy(self.settings)
        del broken["principals"]["assetreader"]["actions"]
        with self.assertRaises(Fault):
            check(broken["web_assets"], broken, broken["principals"])
        # A section that would widen its own identity, or name an unknown setting, is refused.
        for mutate in (
            lambda section: section.update(private=True),
            lambda section: section.update(allowed_connections=[CONNECTION, CONNECTION]),
            lambda section: section.update(enabled="yes"),
        ):
            broken = copy.deepcopy(self.settings)
            mutate(broken["web_assets"])
            with self.assertRaises(Fault):
                check(broken["web_assets"], broken, broken["principals"])
        # And the very same rule runs at deployment time, so a deployment that configures an
        # impossible page does not start at all.
        with self.assertRaises(Fault):
            Platform(broken)

    async def test_disabled_page_is_not_configured_as_a_page(self):
        config = copy.deepcopy(self.settings)
        config["web_assets"]["enabled"] = False
        platform = Platform(config)
        console = WebConsole(platform)
        self.assertFalse(console.assets.available())
        self.assertEqual(console.assets.code(), "assets_disabled")

    async def test_platform_identity_is_never_the_browser_session(self):
        """The page reads with the registered identity, and only with it."""
        logged = await self.login()
        await self.connect(logged)
        await self.page(logged, "libraries")
        self.assertEqual(self.last()["token"], "a")
        with patch.dict(os.environ, {"TS019_ASSET_A": ENV["TS012_ADMIN"]}):
            await self.page(logged, "libraries", expected=503)
        # A different page never reaches the peer with a shared credential value.
        with patch.dict(os.environ, {"TS019_ASSET_A": TOKEN_B}):
            await self.page(logged, "libraries", expected=503)


class AssetPageShapeTests(unittest.TestCase):
    """Pure shape checks that need no server and no peer."""

    def test_operation_whitelist_is_exactly_the_five_reads(self):
        from services.platform import web_asset_queries

        # The page forwards exactly the five published reads, and nothing else exists to forward.
        self.assertEqual(
            set(web_asset_queries.OPERATIONS),
            {"libraries.list", "libraries.get", "entries.browse", "entries.get", "assets.search"},
        )
        self.assertEqual(web_asset_queries.MAX_PAGE, 100)
        self.assertEqual(web_asset_queries.DEFAULT_PAGE, 50)
        self.assertEqual(web_asset_queries.CURSOR_VERSION, 1)

    def test_preview_is_absent_and_says_why(self):
        from services.platform.web_asset_queries import PREVIEW

        self.assertFalse(PREVIEW["available"])
        self.assertEqual(PREVIEW["code"], "asset_media_port_absent")

    def test_unknown_codes_become_one_honest_error(self):
        from services.platform.web_asset_queries import failure

        # A code this page knows keeps its own meaning; anything else collapses to one error that
        # never repeats the peer's word to the browser.
        self.assertEqual(failure("not_found", 404), ("not_found", 404))
        self.assertEqual(failure("dependency_unavailable", 503), ("dependency_unavailable", 503))
        self.assertEqual(failure("some_new_peer_word", 418), ("upstream_error", 502))

    def test_module_boundary_directions_are_one_way(self):
        import re

        from services.platform import asset_page_config, web_asset_queries, web_assets

        rules = Path(web_asset_queries.__file__).read_text(encoding="utf-8")
        # The rule layer is pure: it imports no transport, holds no session, opens no socket.
        self.assertNotRegex(
            rules, re.compile(r"^\s*(import|from)\s+(aiohttp|ssl|socket|time)\b", re.M)
        )
        self.assertNotIn("self.p.assets", rules)
        for forbidden in ("web.json_response", "self.console"):
            self.assertNotIn(forbidden, rules)
        adapter = Path(web_assets.__file__).read_text(encoding="utf-8")
        # The adapter is transport only: it declares no query vocabulary of its own, holds no ACL
        # and reaches the peer through the one existing platform port.
        self.assertIn("self.p.assets.read", adapter)
        self.assertNotRegex(
            adapter, re.compile(r"^\s*(import|from)\s+(aiohttp|ssl|socket)\b", re.M)
        )
        self.assertIn("from .web_asset_queries import", adapter)
        console = (
            Path(web_assets.__file__).parent.joinpath("web_console.py").read_text(encoding="utf-8")
        )
        # The console assembles the route and re-verifies the session; it owns no asset rule.
        self.assertIn("ASSETS_PREFIX", console)
        self.assertIn("self.assets.route(", console)
        for forbidden in ("libraries_request", "slice_page", "asset_connections"):
            self.assertNotIn(forbidden, console)
        # The application client must not depend on the page that consumes it: the configuration
        # rule both sides need lives in a neutral module, and `assets` imports only that one.
        assets = Path(asset_page_config.__file__).parent.joinpath("assets.py").read_text("utf-8")
        self.assertNotIn("web_asset_queries", assets)
        self.assertNotRegex(assets, re.compile(r"^\s*(import|from)\s+\S*\bweb_assets\b", re.M))
        self.assertIn("from .asset_page_config import page_configuration", assets)
        neutral = Path(asset_page_config.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(
            neutral, re.compile(r"^\s*(import|from)\s+(aiohttp|ssl|socket|time)\b", re.M)
        )
        for forbidden in ("web_asset_queries", "self.p.assets", "self.console"):
            self.assertNotIn(forbidden, neutral)
        # Naming the two sides in a docstring is not a dependency; importing either one would be.
        self.assertNotRegex(
            neutral, re.compile(r"^\s*(import|from)\s+\S*\b(web_assets|web_console)\b", re.M)
        )
