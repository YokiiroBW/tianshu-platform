"""Same-origin adapter for the read-only persona page: who may ask, and what is projected back.

This module is the page's scope and transport adapter and nothing more. It owns exactly four
things:

* the four same-origin routes the page may call, and the exact body shape of each - no operation
  name, page size, endpoint, reader, permission or scope ever arrives from the browser;
* the session's own authority check, taken before the peer is called and repeated after every
  `await` and before any body is returned, so a logout, a revoked action or a narrowed subject
  allowlist can never be outrun by a read that was already in flight;
* the redacted projections - a persona body is reported as its revision metadata, its four
  interpreted fields and the *names* of any extension fields, never as a whole content document;
* one bounded read gate: at most four browser requests are served at once per process, and the
  fifth is refused immediately with 429 instead of queueing without a bound.

It owns no persona rule, no comparison rule and no cursor arithmetic: those live in
`persona_page_config` (shapes and cursor binding) and the peer's own answers are proved in
`persona_client`. The page never reads the character service's database, never keeps a copy of a
persona body and never re-derives the peer's whole-content verdict from the four fields.
"""

from .contracts import Fault, canonical, require
from .persona_client import PersonaClient, request_id
from .persona_page_config import (
    CATALOG_PAGE,
    CATALOG_SUBJECTS_PER_PAGE,
    HISTORY_KINDS,
    PageCursor,
    request_document,
)

PREFIX = "/api/web/personas/"
ROUTES = ("catalog", "history", "revision", "compare")
# Four browser business requests at once; the fifth is refused, never queued unbounded.
REQUEST_LIMIT = 4
# The finished page response, in real UTF-8 JSON bytes.
PAGE_MAX_BYTES = 256 * 1024
DOCUMENT_MAX_BYTES = 1024 * 1024
CURSOR_LIMIT = 2048
# The four fields a comparison interprets as persona text. Everything else in a revision document
# is reported as an extension by name only.
COMPARE_FIELDS = ("persona", "tone", "style", "address")
# One word per state this page may show. A peer state outside this vocabulary is refused rather
# than displayed as if it were understood.
STATES = ("published", "draft", "approved", "unpublished", "retired")
# The four history kinds each have their own entry shape, and each row is projected to exactly
# this page's own words for that kind. No row carries persona text: history is identity and
# decisions, and a body is read one revision at a time.
HISTORY_ENTRY_FIELDS = {
    "revisions": (
        "revision_id",
        "fingerprint",
        "parent",
        "source",
        "operator",
        "note",
        "created_at",
        "decision",
        "decided_by",
        "decided_at",
    ),
    "publications": (
        "publication_id",
        "revision_id",
        "supersedes",
        "generation",
        "kind",
        "state",
        "operator",
        "reason",
        "created_at",
    ),
    "approvals": (
        "approval_id",
        "revision_id",
        "fingerprint",
        "decision",
        "state",
        "operator",
        "reason",
        "created_at",
    ),
    "rollbacks": (
        "rollback_id",
        "restored_revision",
        "target_revision",
        "superseded_revision",
        "state",
        "operator",
        "reason",
        "created_at",
    ),
}


class WebPersonas:
    """Console-side read-only window over one registered character service."""

    def __init__(self, platform, console):
        self.p = platform
        self.console = console
        self.rule = platform.personas
        self.enabled = bool(self.rule and self.rule["enabled"])
        self.connection_id = self.rule["connection_id"] if self.rule else None
        self.subjects = tuple(self.rule["allowed_subjects"]) if self.rule else ()
        self.candidate = self.rule["candidate"] if self.rule else None
        self.cursor = PageCursor()
        self.active = 0
        self.client = None
        if self.enabled:
            entry = self.p.settings["persona_connections"][self.connection_id]
            self.client = PersonaClient(self.connection_id, entry, self.candidate)

    # ------------------------------------------------------------------- state

    def available(self):
        """An unconfigured, disabled or unauthorised deployment never looks ready."""
        return self.enabled and self.code() == "ready"

    def code(self):
        """One honest word for the page; nothing here is inferred from a failed read."""
        if self.p.settings.get("web_personas") is None:
            return "personas_not_configured"
        if not self.enabled:
            return "personas_disabled"
        if not self._authorised():
            return "persona_read_required"
        return "ready"

    def _principal(self):
        name = self.console.config["principal"] if self.console.config else None
        return self.p.auth.principals.get(name, {})

    def _authorised(self):
        principal = self._principal()
        # The deployment's local operator identity must be exactly that, and it must carry the
        # explicit persona read action. Login, config.publish and device.control grant nothing.
        return (
            principal.get("kind") == "operator"
            and principal.get("service") == "platform"
            and "persona.read" in set(principal.get("actions", []))
        )

    # ------------------------------------------------------------------ routing

    async def route(self, path, body, session):
        """Serve one of the four fixed read routes for one live console session."""
        require(isinstance(body, dict), "invalid_input", 400)
        name = path[len(PREFIX) :] if path.startswith(PREFIX) else ""
        require(name in ROUTES, "not_found", 404)
        # Authority first: a session that stopped being readable - logged out, expired, rotated or
        # re-pointed - is exactly that, never a deployment problem and never a page.
        require(self.console.session_valid(session), "session_expired", 401)
        self._ready()
        self._admit()
        try:
            result = await self._serve(name, body, session)
        finally:
            self.active -= 1
        # The same two proofs again after every `await`: a session, a read action or an authority
        # that stopped holding while the peer was answering does not get a body.
        require(self.console.session_valid(session), "session_expired", 401)
        self._ready()
        return result

    def _ready(self):
        """This deployment may read personas for this operator, or the honest reason why not.

        Each refusal keeps the page's own word, so the browser can tell a deployment that has no
        persona page from an operator who may not read one - and neither is ever shown as a page
        with nothing in it.
        """
        state = self.code()
        if state == "ready":
            return
        # A missing read action is a permission refusal; a page that was never configured or never
        # enabled is not a page with nothing in it.
        raise Fault(state, 403 if state == "persona_read_required" else 503)

    def _admit(self):
        if self.active >= REQUEST_LIMIT:
            raise Fault("too_many_requests", 429)
        self.active += 1

    async def _serve(self, name, body, session):
        if name == "catalog":
            self._shape(body, {"cursor"})
            return await self.catalog(session, body["cursor"])
        if name == "history":
            self._shape(body, {"subject", "kind", "cursor"})
            return await self.history(session, body["subject"], body["kind"], body["cursor"])
        if name == "revision":
            self._shape(body, {"subject", "revision_id"})
            return await self.revision(session, body["subject"], body["revision_id"])
        self._shape(body, {"subject", "left", "right"})
        return await self.compare(session, body["subject"], body["left"], body["right"])

    def _shape(self, body, keys):
        require(set(body) == keys, "invalid_input", 400)

    # ------------------------------------------------------------------- scope

    def _fingerprint(self):
        return self.console.authority()[0]

    def _subject(self, value):
        require(isinstance(value, str) and value in self.subjects, "forbidden", 403)
        return value

    def _revision_id(self, value):
        require(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value),
            "invalid_input",
            400,
        )
        return value

    # ----------------------------------------------------------------- catalog

    async def catalog(self, session, cursor):
        """One page of the deployment's own closed subject list, ascending, fixed at 20."""
        require(cursor is None or isinstance(cursor, str), "invalid_input", 400)
        authority = self._fingerprint()
        position = self.cursor.read(
            cursor, "catalog", authority, self.connection_id, self.subjects, CATALOG_PAGE
        )
        window = self.subjects[position : position + CATALOG_SUBJECTS_PER_PAGE]
        results, failures = await self.client.catalog(window)
        read = [result for result in results if result is not None]
        unreadable = [
            failure for failure in failures if failure is not None and failure.code != "not_found"
        ]
        # A subject this deployment allows but the character service does not hold is a stated
        # absence inside a real page. A page where *nothing* could be read is not a directory with
        # entries missing - it is a failed read, and it is reported as the fault it was.
        if unreadable and not read:
            raise unreadable[0]
        entries = []
        for index, subject in enumerate(window):
            entries.append(self._entry(subject, results[index], failures[index]))
        more = position + len(window) < len(self.subjects)
        return self._bounded(
            {
                "code": "ready",
                # No registration name, endpoint, credential or reader identity is projected: the
                # browser is told how many characters this deployment allows, nothing about where
                # they are read from.
                "page": CATALOG_PAGE,
                "subjects": len(self.subjects),
                "count": len(entries),
                "entries": entries,
                "has_more": more,
                "next_cursor": (
                    self.cursor.issue(
                        "catalog",
                        authority,
                        self.connection_id,
                        self.subjects,
                        CATALOG_PAGE,
                        position + len(window),
                    )
                    if more
                    else None
                ),
            },
            PAGE_MAX_BYTES,
        )

    def _entry(self, subject, answer, failure):
        """One directory row: identity and pointers only, never a persona body.

        Every row carries the same keys, so a subject the deployment allows but the character
        service does not hold is one stated absence inside a real page - never a missing row and
        never an empty directory.
        """
        row = {
            "subject": subject,
            "state": None,
            "version": None,
            "published_revision": None,
            "draft_revision": None,
            "retired": None,
            "updated_at": None,
            "error": failure.code if failure is not None else None,
        }
        if failure is not None:
            return row
        persona = answer["persona"]
        require(persona.get("state") in STATES, "invalid_upstream", 502)
        return {
            "subject": persona["subject"],
            "state": persona["state"],
            "version": persona["version"],
            "published_revision": persona.get("published_revision"),
            "draft_revision": persona.get("draft_revision"),
            "retired": bool(persona.get("retired")),
            "updated_at": persona.get("updated_at"),
            "error": None,
        }

    # ----------------------------------------------------------------- history

    async def history(self, session, subject, kind, cursor):
        """One bounded history page; the peer's own opaque cursor is kept, never decoded."""
        require(isinstance(kind, str) and kind in HISTORY_KINDS, "invalid_input", 400)
        require(cursor is None or isinstance(cursor, str), "invalid_input", 400)
        require(cursor is None or 0 < len(cursor) <= CURSOR_LIMIT, "invalid_input", 400)
        subject = self._subject(subject)
        document = request_document("history_page", subject, request_id(), kind=kind, cursor=cursor)
        answer = await self.client.call(document)
        entries = [self._history_entry(kind, entry) for entry in answer["entries"]]
        return self._bounded(
            {
                "subject": subject,
                "kind": kind,
                "persona_version": answer["persona_version"],
                "consistency": answer["consistency"],
                "limit": answer["limit"],
                "count": answer["count"],
                "entries": entries,
                "has_more": answer["has_more"],
                "next_cursor": answer["next_cursor"],
            },
            PAGE_MAX_BYTES,
        )

    def _history_entry(self, kind, entry):
        """One history row in this page's own words for that kind, and nothing else.

        The candidate proves the exact per-kind shape before this runs, so the four kinds get four
        fixed projections: a revision row names revisions, a publication row names a generation, an
        approval row names a decision, a rollback row names the revisions it moved between. A peer
        field outside that list is dropped rather than forwarded, and no row ever carries text.
        """
        require(isinstance(entry, dict), "invalid_upstream", 502)
        row = {}
        for name in HISTORY_ENTRY_FIELDS[kind]:
            row[name] = entry.get(name)
        for name in (
            "revision_id",
            "fingerprint",
            "parent",
            "supersedes",
            "restored_revision",
            "target_revision",
            "superseded_revision",
        ):
            if row.get(name) is not None:
                require(self._is_digest(row[name]), "invalid_upstream", 502)
        for name in ("publication_id", "approval_id", "rollback_id"):
            if row.get(name) is not None:
                require(self._is_digest(row[name]), "invalid_upstream", 502)
        return row

    def _is_digest(self, value):
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    # ---------------------------------------------------------------- revision

    async def revision(self, session, subject, revision_id):
        """One revision body, projected: four interpreted fields plus extension field names."""
        subject = self._subject(subject)
        revision_id = self._revision_id(revision_id)
        answer = await self.client.call(
            request_document("revision", subject, request_id(), revision_id=revision_id)
        )
        revision = answer["revision"]
        require(answer.get("state") in STATES, "invalid_upstream", 502)
        return self._bounded(
            {
                "subject": subject,
                "revision": self._revision(revision),
                "is_published": bool(answer.get("is_published")),
                "is_draft": bool(answer.get("is_draft")),
                "persona_version": answer["persona_version"],
                "state": answer["state"],
                "published_revision": answer.get("published_revision"),
                "draft_revision": answer.get("draft_revision"),
            },
            DOCUMENT_MAX_BYTES,
        )

    def _revision(self, revision):
        content = revision.get("content")
        require(isinstance(content, dict) and "persona" in content, "invalid_upstream", 502)
        return {
            "revision_id": revision["revision_id"],
            "fingerprint": revision.get("fingerprint"),
            "content": {name: content[name] for name in COMPARE_FIELDS if name in content},
            "present": [name for name in COMPARE_FIELDS if name in content],
            "additional_fields": sorted(name for name in content if name not in COMPARE_FIELDS),
            "parent": revision.get("parent"),
            "source": revision.get("source"),
            "note": revision.get("note"),
            "created_at": revision.get("created_at"),
        }

    # ----------------------------------------------------------------- compare

    async def compare(self, session, subject, left, right):
        """One comparison of two revisions of the same character, exactly as the peer stated it."""
        subject = self._subject(subject)
        left, right = self._revision_id(left), self._revision_id(right)
        answer = await self.client.call(
            request_document("compare", subject, request_id(), left=left, right=right)
        )
        require(answer.get("state") in STATES, "invalid_upstream", 502)
        fields = {}
        for name in COMPARE_FIELDS:
            field = answer["fields"][name]
            fields[name] = {
                "change": field["change"],
                "presence": {
                    "left": field["presence"]["left"],
                    "right": field["presence"]["right"],
                },
                "left": field.get("left"),
                "right": field.get("right"),
            }
        return self._bounded(
            {
                "subject": subject,
                "left": self._reference(answer["left"]),
                "right": self._reference(answer["right"]),
                "fields": fields,
                "persona_version": answer["persona_version"],
                "state": answer["state"],
                "published_revision": answer.get("published_revision"),
                "draft_revision": answer.get("draft_revision"),
                "additional_fields_present": answer["additional_fields_present"],
                "additional_fields_changed": answer["additional_fields_changed"],
                "additional_field_names": list(answer["additional_field_names"]),
                "identical_revision": answer["identical_revision"],
                "content_identical": answer["content_identical"],
            },
            DOCUMENT_MAX_BYTES,
        )

    def _reference(self, revision):
        """One side of a comparison without its text; the values live in the comparison itself."""
        return {
            "revision_id": revision["revision_id"],
            "fingerprint": revision.get("fingerprint"),
            "parent": revision.get("parent"),
            "source": revision.get("source"),
            "note": revision.get("note"),
            "created_at": revision.get("created_at"),
        }

    # ------------------------------------------------------------------ budget

    def _bounded(self, document, limit):
        require(len(canonical(document).encode("utf-8")) <= limit, "budget_exceeded", 413)
        return document
