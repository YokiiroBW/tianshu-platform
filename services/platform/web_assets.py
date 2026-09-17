"""Same-origin adapter for the read-only asset page.

This module is the page's transport and scope adapter, and nothing more. It owns exactly three
things:

* `web_assets` deployment settings: which registered service identity reads assets, and which of
  its connections this page is allowed to expose (`allowed_connections` narrows, never grants);
* the console session's own page scope: which one allowed connection the operator is looking at,
  re-verified on every request and bounded by a short lifetime;
* the call into the existing `platform.assets.read` application port, plus a bounded record of
  what was actually asked for.

It does not own any asset rule. Request normalization, cursor binding, path and identifier bounds,
projections and error mapping live in `services.platform.web_asset_queries`, which the adapter
depends on and which never depends back on this module. It never talks to AssetLink directly:
there is one AssetLibrary client in this product and it belongs to the TS-064 capability. It
copies no asset table, keeps no page cache, and holds no result between requests.

Authority is layered, never inherited: the console session is the operator's own login, this page
reads with its own registered identity, the peer authorizes independently on every request, and
nothing the browser sends — a credential, an endpoint, a physical path, an administrator claim —
is treated as authorization.
"""

import asyncio
import time
import uuid

from .auth import secret
from .contracts import Fault, require
from .web_asset_queries import (
    DEFAULT_PAGE,
    DIRECTIONS,
    KIND_FILTERS,
    MAX_PAGE,
    PAGE_SCOPES,
    PREVIEW,
    SEARCH_SCOPES,
    SORT_BY,
    Scope,
    browse_request,
    echo,
    entry as entry_projection,
    entry_request,
    failure,
    hit as hit_projection,
    library as library_projection,
    libraries_request,
    page_configuration,
    search_request,
    slice_page,
    text,
)

PREFIX = "/api/web/assets/"
# The page's own scope lifetime, and its bounds; this is page state, not a credential.
SCOPE_TTL = 900
READS_KEPT = 32
READS_SHOWN = 8


class WebAssets:
    """Console-side read-only window over the peer's authorized index."""

    def __init__(self, platform, console):
        self.p = platform
        self.console = console
        self.config = platform.settings.get("web_assets")
        self.enabled = False
        self.principal = None
        self.allowed = ()
        self.preview = dict(PREVIEW)
        self.scope_ttl = SCOPE_TTL
        self.reads = []
        if self.config is not None:
            self._configure()

    def _configure(self):
        # The narrowing rules live in the pure rule layer, so the same function validates this
        # section at deployment time and here; this adapter only remembers the outcome.
        self.enabled, self.principal, self.allowed = page_configuration(
            self.config,
            self.p.auth.principals,
            self.p.assets.connections,
            self.p.contracts.check,
        )

    # ------------------------------------------------------------------ state

    def available(self):
        return self.config is not None and self.enabled

    def code(self):
        """One honest word for the page; an unconfigured deployment never looks ready."""
        if self.config is None or not self.enabled:
            return "assets_disabled"
        principal = self.p.auth.principals.get(self.principal, {})
        if "asset.read" not in set(principal.get("actions", [])):
            return "operator_not_authorized"
        if not self.allowed:
            return "no_asset_connections"
        return "ready"

    def connections(self):
        """The connection names this page may offer, with nothing that could reach a peer."""
        return [
            {
                "connection_id": connection_id,
                "label": connection_id,
                # Whether the deployment holds a credential is not a secret; whether it works is
                # not claimed here, because only a real read can establish that.
                "credential_registered": secret(
                    self.p.assets.connections[connection_id]["token_env"]
                )
                is not None,
            }
            for connection_id in self.allowed
        ]

    def scope(self, session):
        """Which allowed connection this session is looking at, and until when."""
        current = session.get("assets")
        live = (
            isinstance(current, dict)
            and current.get("expires", 0) > self.console.clock()
            and current.get("connection_id") in self.allowed
        )
        return current["connection_id"] if live else None

    def state(self, session):
        connection = self.scope(session)
        return {
            "available": True,
            "code": self.code(),
            "enabled": self.enabled,
            "connection": connection,
            "scope_ttl_seconds": self.scope_ttl,
            "connections": self.connections(),
            "preview": self.preview,
            "page_size": {"default": DEFAULT_PAGE, "maximum": MAX_PAGE},
            "sort": {"fields": list(SORT_BY), "directions": list(DIRECTIONS)},
            "kinds": list(KIND_FILTERS),
            "scopes": list(PAGE_SCOPES),
            "search_scopes": list(SEARCH_SCOPES),
            # Every scope, query or sorting change starts a new read: the browser clears its old
            # page and aborts the read in flight, and this adapter holds no page that could be
            # shown again or refilled by a late answer.
            "reselect": "clear_and_refetch",
            "cancel": "abort_in_flight",
            # What this page actually asked for, so it can state its own freshness instead of
            # implying a live directory.
            "reads": self.history(connection),
        }

    def history(self, connection_id=None):
        """The bounded record of reads this page performed, newest first; never content."""
        return [
            row
            for row in reversed(self.reads)
            if connection_id is None or row["connection_id"] == connection_id
        ][:READS_SHOWN]

    def _record(self, connection_id, operation, code, request_id, started):
        self.reads.append(
            {
                "at": self.console.clock(),
                "connection_id": connection_id,
                "operation": operation,
                "code": code,
                "request_id": request_id,
                "seconds": round(max(0.0, self.console.clock() - started), 3),
            }
        )
        del self.reads[:-READS_KEPT]

    # ----------------------------------------------------------------- routes

    async def route(self, path, body, session):
        require(self.available(), self.code(), 403)
        require(isinstance(body, dict), "invalid_input", 400)
        if path == PREFIX + "connection":
            return self.connection(body, session)
        if path == PREFIX + "libraries":
            return await self.libraries(body, session)
        if path == PREFIX + "browse":
            return await self.browse(body, session)
        if path == PREFIX + "search":
            return await self.search(body, session)
        if path == PREFIX + "entry":
            return await self.entry(body, session)
        raise Fault("not_found", 404)

    def connection(self, body, session):
        """Choose one allowed connection, or drop the choice; the page's scope lives here."""
        require(set(body) <= {"connection_id"}, "invalid_input", 400)
        if body == {}:
            session.pop("assets", None)
            return self.state(session)
        connection_id = text(body["connection_id"], 128)
        require(connection_id in self.allowed, "connection_not_allowed", 403)
        session["assets"] = {
            "connection_id": connection_id,
            "expires": self.console.clock() + self.scope_ttl,
        }
        return self.state(session)

    def _locked(self, session):
        """The session's own chosen connection, or a real refusal; never a body-supplied one."""
        connection_id = self.scope(session)
        require(connection_id is not None, "connection_required", 403)
        return connection_id

    # ------------------------------------------------------------------- reads

    async def libraries(self, body, session):
        connection_id = self._locked(session)
        page_scope = self._scope(connection_id, "libraries")
        request, options = libraries_request(body, page_scope)
        result = await self._read(session, connection_id, "libraries.list", request)
        found = slice_page(result, request["page_size"], page_scope, options, library_projection)
        return {
            "operation": "libraries.list",
            "connection": connection_id,
            "libraries": found["items"],
            "page": found["page"],
            "preview": self.preview,
        }

    async def browse(self, body, session):
        connection_id = self._locked(session)
        # The continuation's identity is derived from the very options the request carries, by the
        # rule module that also validates them; the adapter never reconstructs it.
        page_scope = self._scope(connection_id, "browse")
        request, options = browse_request(body, page_scope)
        result = await self._read(session, connection_id, "entries.browse", request)
        require(isinstance(result, dict), "invalid_upstream", 502)
        found = library_projection(result.get("library"))
        # A page for one library is only ever shown as that library's page.
        require(found["library_id"] == request["library_id"], "invalid_upstream", 502)
        listed = slice_page(result, request["page_size"], page_scope, options, entry_projection)
        for item in listed["items"]:
            require(item["library_id"] == request["library_id"], "invalid_upstream", 502)
        return {
            "operation": "entries.browse",
            "connection": connection_id,
            "library": found,
            "parent_relative_path": echo(
                result.get("parent_relative_path"), request["parent_relative_path"]
            ),
            "entries": listed["items"],
            "page": listed["page"],
            "options": {
                "sort_by": request.get("sort_by", "name"),
                "sort_direction": request.get("sort_direction", "asc"),
                "kind": request.get("kind", "all"),
                "name_filter": request.get("name_filter", ""),
            },
            "preview": self.preview,
        }

    async def search(self, body, session):
        connection_id = self._locked(session)
        page_scope = self._scope(connection_id, "search")
        request, options = search_request(body, page_scope)
        result = await self._read(session, connection_id, "assets.search", request)
        listed = slice_page(result, request["page_size"], page_scope, options, hit_projection)
        # A hit list that mixed libraries would silently widen the scope the user asked for.
        target = request.get("library_id")
        if target is not None:
            for item in listed["items"]:
                require(item["library"]["library_id"] == target, "invalid_upstream", 502)
        return {
            "operation": "assets.search",
            "connection": connection_id,
            "query": request["query"],
            "scope": request["scope"],
            "library_id": target,
            "parent_relative_path": request.get("parent_relative_path"),
            "hits": listed["items"],
            "page": listed["page"],
            "preview": self.preview,
        }

    async def entry(self, body, session):
        """One entry re-read from the peer; a listing is a snapshot, a detail is current."""
        connection_id = self._locked(session)
        request = entry_request(body)
        result = await self._read(session, connection_id, "entries.get", request)
        require(isinstance(result, dict), "invalid_upstream", 502)
        found = library_projection(result.get("library"))
        item = entry_projection(result.get("entry"))
        # A detail for another library or another entry is not this page's detail.
        require(
            found["library_id"] == request["library_id"]
            and item["library_id"] == request["library_id"]
            and item["entry_id"] == request["entry_id"],
            "invalid_upstream",
            502,
        )
        return {
            "operation": "entries.get",
            "connection": connection_id,
            "library": found,
            "entry": item,
            "preview": self.preview,
        }

    # ------------------------------------------------------------------ scopes

    def _scope(self, connection_id, page_name):
        return Scope(self.principal, connection_id, page_name)

    # ---------------------------------------------------------------- plumbing

    async def _read(self, session, connection_id, operation, body):
        """One read through the TS-064 port, with the browser's own cancellation propagated."""
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        request = {"connection_id": connection_id, "operation": operation, "body": body}
        try:
            result = await self.p.assets.read(self._header(), request)
        except asyncio.CancelledError:
            # Cancelling stops this page from waiting; it never claims the read succeeded.
            self._record(connection_id, operation, "cancelled", request_id, started)
            raise
        if result["ok"]:
            self._record(connection_id, operation, "ok", request_id, started)
            return result["body"]
        code, status = failure(result["code"], result["status"])
        self._record(connection_id, operation, code, request_id, started)
        raise Fault(code, status)

    def _header(self):
        """The registered reading identity's own credential, taken from the environment."""
        principal = self.p.auth.principals[self.principal]
        return "Bearer " + (secret(principal["token_env"]) or "")
