"""The only upstream persona client this product has. One credential, one HTTPS peer, one page.

Everything that touches the character service lives here and nowhere else: the real TLS context
(never a skipped verification, never an environment proxy), the request byte shape, the response
byte budget, the request/response correspondence proof, the client-wide concurrency ceiling and the
one absolute deadline that bounds every wait - including the wait for an outbound slot.

Three rules are deliberately kept out of the route adapter and out of the console:

* **Bytes before meaning.** A peer answer is read as a bounded UTF-8 byte stream and only parsed
  once it fits the operation's budget, so an oversized or non-JSON answer is one refusal instead of
  a memory surprise. History pages get the tighter budget the candidate documents.
* **An answer must answer the question.** The operation, subject, history kind, revision ids,
  `count == entries.length`, the `has_more`/`next_cursor` relationship and - for every error - the
  echoed `request_id` are all proved before any field is read. A wrong answer, or one that answers
  somebody else's request, is `invalid_upstream`: never a success, never an empty page and never a
  refusal this product would otherwise have to guess at.
* **Failures are translated, never invented.** A peer code this consumer profile does not know
  becomes one honest `dependency_unavailable`; a permission or connection failure is never turned
  into an empty result, and a page is never partly shown as if the part that failed were absent.

The client owns no rule about what a browser may ask for and no rule about who may ask. It is a
transport boundary: `web_personas` decides scope, and `persona_page_config` owns the shapes. What it
does own is *when* that scope must be re-proved: the request's own guard travels with it
(`SCOPE`), and every outbound step asks again.
"""

import asyncio
import contextvars
import hmac
import ssl
import uuid

import aiohttp

from . import diagnostics
from .auth import secret
from .contracts import Fault, loads, require
from .persona_page_config import (
    CANDIDATE_OPERATIONS,
    CATALOG_WORKERS,
    validate_request,
    validate_response,
)

# Real UTF-8 JSON bytes, measured on the stream before anything is parsed.
HISTORY_MAX_BYTES = 256 * 1024
DOCUMENT_MAX_BYTES = 1024 * 1024
CHUNK = 65536
# The whole read is one bounded wait, and the outbound slot is part of that wait.
DEADLINE_SECONDS = 10.0
# At most four persona reads are ever in flight to the character service.
UPSTREAM_LIMIT = 4

# Peer codes this consumer may repeat, with the status the peer itself used. Anything else becomes
# one honest `dependency_unavailable`, so a vocabulary this product does not own is never echoed.
KNOWN_CODES = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid_input": 400,
    "version_conflict": 409,
    "scope_changed": 409,
    "idempotency_conflict": 409,
    "result_unknown": 409,
    "queue_full": 429,
    "budget_exceeded": 429,
    "timeout": 503,
    "dependency_unavailable": 503,
}
COMPARE_FIELDS = ("persona", "tone", "style", "address")


def request_id():
    """One server-generated correlation id; it never carries an operator or a scope."""
    return "web-persona-" + uuid.uuid4().hex


class ScopeLost(Exception):
    """One read whose authority stopped holding while it was in flight.

    It is deliberately *not* a `Fault`: a refused subject is a per-character fact, but a scope that
    ended is the whole request's, so nothing may record it as one row's error and carry on. The
    adapter that built the guard turns it back into the one honest fault it stands for.
    """

    def __init__(self, fault):
        super().__init__(fault.code)
        self.fault = fault


# The guard of the read that is in flight *in this task*. It is carried in a context variable
# because the read outlives the call that started it: waiting for an outbound slot, sending and
# reading the answer all happen inside the client, and each of them must be able to re-prove the
# very authority the request was admitted under. A task created by `catalog` inherits the context of
# the request that created it, so four workers share one request's guard and never another's.
SCOPE = contextvars.ContextVar("persona_read_scope", default=None)


def prove(document):
    """Re-prove the current request's own scope, when this task carries one."""
    guard = SCOPE.get()
    if guard is not None:
        guard(document)


class PersonaClient:
    """One registered connection: address, credential variable, CA, timeout and the candidate."""

    def __init__(self, connection_id, entry, candidate, reserved=()):
        self.connection_id = connection_id
        self.base_url = entry["base_url"].rstrip("/")
        self.token_env = entry["token_env"]
        self.ca_file = entry.get("ca_file")
        self.timeout = entry.get("timeout_seconds", DEADLINE_SECONDS)
        self.candidate = candidate
        # Variables that belong to other registered identities: this client never sends their
        # value, whichever name the deployment pointed it at.
        self.reserved = tuple(reserved)
        self.slots = asyncio.Semaphore(UPSTREAM_LIMIT)

    def credential(self):
        """The registered reading identity's own credential, resolved at call time.

        A revoked, rotated or missing variable is a refusal here, never a cached value: no earlier
        session may keep reading because it once held a working secret. A value that resolves to
        another registered identity's secret is refused too - rotation must not be able to lend the
        page somebody else's identity after the deployment was accepted.
        """
        value = secret(self.token_env)
        require(value is not None, "dependency_unavailable", 503)
        for name in self.reserved:
            if name == self.token_env:
                continue
            other = secret(name)
            require(other is None or not hmac.compare_digest(value, other), "forbidden", 403)
        return value

    def _tls(self):
        try:
            return ssl.create_default_context(cafile=self.ca_file)
        except (OSError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None

    async def call(self, document):
        """Send one operation document and return the peer's verified answer.

        The document is built from `persona_page_config` and validated against the candidate's own
        request schema before it leaves this process, so no adapter can widen the wire shape. The
        request's own scope is proved three times on the way out and back: before anything is
        attempted, once the outbound slot has been granted and before the socket is written, and
        once the answer is in hand and before it is trusted. A scope that ended during any of those
        waits stops this read instead of returning under an authority it no longer has.
        """
        operation = document.get("operation")
        require(operation in CANDIDATE_OPERATIONS, "invalid_input", 400)
        validate_request(self.candidate, document)
        prove(document)
        span = diagnostics.Span("persona_call")
        sent = False
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                # Waiting for an outbound slot is part of the same bounded wait: a queued read
                # never gets its own fresh ten seconds.
                diagnostics.outbound("queued")
                async with self.slots:
                    # The wait is over and nothing has been sent yet: this is the last moment at
                    # which a request that lost its authority can still be stopped for free.
                    prove(document)
                    token = self.credential()
                    sent = True
                    diagnostics.outbound("started")
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False
                    ) as session:
                        async with session.post(
                            self.base_url + "/internal/v1/persona/manage",
                            json=document,
                            headers={
                                "Authorization": "Bearer " + token,
                                **diagnostics.correlation_header(),
                            },
                            ssl=self._tls(),
                            allow_redirects=False,
                        ) as response:
                            answer = await self._read(
                                response, operation, document.get("request_id")
                            )
            # The answer answered *something*; whether it may still be read here is this request's
            # own question, and it is asked again after every await above.
            prove(document)
        except asyncio.CancelledError:
            # A cancelled read always gets its terminal outbound outcome, queued or sent: an
            # outbound event with no end reads as a call still in flight, which is a worse
            # misreport than the one it replaces. The fixed code separates a read that could not
            # have reached the peer from one that may have, without inventing a field to say so.
            diagnostics.outbound(
                "cancelled",
                duration_ms=span.elapsed() * 1000.0,
                error_code=None if sent else "outbound_not_sent",
            )
            raise
        except TimeoutError:
            diagnostics.outbound(
                "timed_out", duration_ms=span.elapsed() * 1000.0, error_code="timeout"
            )
            raise Fault("timeout", 503) from None
        except (aiohttp.ClientError, OSError, ssl.SSLError):
            diagnostics.outbound(
                "failed",
                duration_ms=span.elapsed() * 1000.0,
                error_code="dependency_unavailable",
            )
            raise Fault("dependency_unavailable", 503) from None
        except Fault as exc:
            # Only a call that actually left this process is an outbound result; a scope that ended
            # before the socket was written is reported as this request's own refusal.
            if sent:
                diagnostics.outbound(
                    "failed",
                    duration_ms=span.elapsed() * 1000.0,
                    error_code=diagnostics.safe_code(exc.code),
                )
            raise
        try:
            result = self._verify(answer, document)
        except Fault as exc:
            # The peer answered, but not with something this page may use: the call still has a
            # terminal outbound outcome and it is not a success.
            diagnostics.outbound(
                "failed",
                duration_ms=span.elapsed() * 1000.0,
                error_code=diagnostics.safe_code(exc.code),
            )
            raise
        diagnostics.outbound("succeeded", duration_ms=span.elapsed() * 1000.0)
        return result

    async def _read(self, response, operation, request_id=None):
        """Bounded stream read, then parse. The byte budget is applied to real bytes.

        A peer answer past the budget is stopped mid-stream and reported as what it is: an answer
        this product cannot use. The page-response budget is the adapter's own, and a refused
        upstream read never becomes either a partial page or an empty one. The correlation id of
        the request that was actually sent travels with the read, so an error packet is only ever
        accepted as the answer to *this* call.
        """
        limit = HISTORY_MAX_BYTES if operation == "history_page" else DOCUMENT_MAX_BYTES
        require(response.content_type == "application/json", "invalid_upstream", 502)
        chunks, total = [], 0
        async for chunk in response.content.iter_chunked(CHUNK):
            total += len(chunk)
            require(total <= limit, "invalid_upstream", 502)
            chunks.append(chunk)
        answer = self._parse(b"".join(chunks))
        require(isinstance(answer, dict), "invalid_upstream", 502)
        if response.status != 200:
            return self._fault(answer, response.status, operation, request_id)
        return answer

    def _parse(self, raw):
        """One peer answer, parsed here so a body this product cannot read is stated as such.

        `loads` refuses malformed bytes with this platform's own input word, which is right for a
        request and wrong for an answer: an unparsable *answer* is an unusable upstream, so it is
        mapped to that and never mistaken for something the browser asked for badly.
        """
        try:
            return loads(raw)
        except Fault:
            raise Fault("invalid_upstream", 502) from None
        except (ValueError, UnicodeError, RecursionError, TypeError):
            raise Fault("invalid_upstream", 502) from None

    def _fault(self, answer, status, operation, request_id=None):
        """One peer refusal, echoed only when this product owns it *and* it answers this request.

        An error that does not carry the correlation id of the request that was sent cannot be
        proved to belong to it, so it is refused as an unusable answer instead of being read as
        this call's refusal - a mixed-up error must never be able to look like "this character does
        not exist". The same rule applies when no correlation id is available to compare with: a
        refusal this product cannot attribute is not a refusal it may repeat.
        """
        require(
            isinstance(operation, str) and operation in CANDIDATE_OPERATIONS, "invalid_input", 400
        )
        validate_response(self.candidate, answer)
        require(
            isinstance(request_id, str)
            and request_id != ""
            and answer.get("request_id") == request_id,
            "invalid_upstream",
            502,
        )
        code = answer.get("code")
        require(isinstance(code, str) and code in KNOWN_CODES, "dependency_unavailable", 503)
        require(status == KNOWN_CODES[code], "invalid_upstream", 502)
        raise Fault(code, status)

    # ------------------------------------------------------------------ verification

    def _verify(self, answer, document):
        """Prove the answer answers this exact question before any field of it is read."""
        operation = document["operation"]
        validate_response(self.candidate, answer)
        require(answer.get("operation") == operation, "invalid_upstream", 502)
        if operation == "get":
            persona = answer.get("persona")
            require(isinstance(persona, dict), "invalid_upstream", 502)
            require(persona.get("subject") == document["subject"], "invalid_upstream", 502)
            require(type(persona.get("version")) is int, "invalid_upstream", 502)
            return answer
        require(answer.get("subject") == document["subject"], "invalid_upstream", 502)
        if operation == "history_page":
            return self._verify_history(answer, document)
        if operation == "revision":
            return self._verify_revision(answer, document)
        return self._verify_compare(answer, document)

    def _verify_history(self, answer, document):
        require(answer.get("kind") == document["kind"], "invalid_upstream", 502)
        require(answer.get("consistency") == "version_bound", "invalid_upstream", 502)
        require(answer.get("limit") == document["limit"], "invalid_upstream", 502)
        require(type(answer.get("persona_version")) is int, "invalid_upstream", 502)
        return self._page(answer)

    def _verify_revision(self, answer, document):
        revision = answer.get("revision")
        require(isinstance(revision, dict), "invalid_upstream", 502)
        require(revision.get("revision_id") == document["revision_id"], "invalid_upstream", 502)
        require(type(revision.get("content")) is dict, "invalid_upstream", 502)
        require(
            answer.get("state") in {"published", "draft", "approved", "unpublished", "retired"},
            "invalid_upstream",
            502,
        )
        return answer

    def _verify_compare(self, answer, document):
        for side in ("left", "right"):
            revision = answer.get(side)
            require(isinstance(revision, dict), "invalid_upstream", 502)
            require(revision.get("revision_id") == document[side], "invalid_upstream", 502)
            # The comparison carries the values; a revision object here never repeats them.
            require("content" not in revision, "invalid_upstream", 502)
        require(answer.get("comparison_scope") == list(COMPARE_FIELDS), "invalid_upstream", 502)
        require(type(answer.get("content_identical")) is bool, "invalid_upstream", 502)
        require(type(answer.get("identical_revision")) is bool, "invalid_upstream", 502)
        require(type(answer.get("additional_fields_changed")) is bool, "invalid_upstream", 502)
        require(type(answer.get("additional_fields_present")) is bool, "invalid_upstream", 502)
        require(isinstance(answer.get("additional_field_names"), list), "invalid_upstream", 502)
        fields = answer.get("fields")
        require(
            isinstance(fields, dict) and set(fields) == set(COMPARE_FIELDS), "invalid_upstream", 502
        )
        for name, field in fields.items():
            require(isinstance(field, dict), "invalid_upstream", 502)
            presence = field.get("presence")
            require(
                isinstance(presence, dict)
                and set(presence) == {"left", "right"}
                and all(type(value) is bool for value in presence.values()),
                "invalid_upstream",
                502,
            )
            require(
                field.get("change") in {"added", "removed", "modified", "unchanged"},
                "invalid_upstream",
                502,
            )
        return answer

    def _page(self, answer):
        """The pagination arithmetic must agree with itself, entry for entry."""
        entries = answer.get("entries")
        count = answer.get("count")
        more = answer.get("has_more")
        cursor = answer.get("next_cursor")
        require(isinstance(entries, list), "invalid_upstream", 502)
        require(type(count) is int and count == len(entries), "invalid_upstream", 502)
        require(type(more) is bool, "invalid_upstream", 502)
        require(more == (cursor is not None), "invalid_upstream", 502)
        if more:
            require(isinstance(cursor, str) and 0 < len(cursor) <= 2048, "invalid_upstream", 502)
            require(count == answer.get("limit"), "invalid_upstream", 502)
        return answer

    # ---------------------------------------------------------------------- catalog

    async def catalog(self, subjects):
        """One bounded directory page: at most 20 allowed subjects, at most 4 read at once.

        The peer's own global catalogue is deliberately never consulted; a deployment's closed
        subject list is enumerated through the single-character read, so nothing outside the
        allowlist can appear here even if the peer would happily list it.

        Only one refusal is a per-character fact: the peer says, in an answer correlated to this
        exact request, that it does not hold that character. Everything else - a network failure, a
        refused credential, a timeout, an answer that belongs to another request - makes the page
        unshowable, so it stops the whole directory at once, cancels the work still in flight and
        lets the caller state the failure instead of presenting a partly-readable page as a success.
        """
        require(isinstance(subjects, (list, tuple)) and len(subjects) <= 20, "invalid_input", 400)
        results = [None] * len(subjects)
        failures = [None] * len(subjects)

        async def one(index, subject):
            document = {"operation": "get", "subject": subject, "request_id": request_id()}
            # This item's own turn to prove the scope: a read that lost its session, its action or
            # its allowlist must not even start the next character's request.
            prove(document)
            try:
                results[index] = await self.call(document)
            except Fault as error:
                # One character the peer really does not hold stays that character's own row; the
                # caller decides how to present it, and a refusal is never an empty row. Anything
                # else is the whole page's failure and stops the remaining work here.
                if error.code != "not_found":
                    raise
                failures[index] = error

        queue = asyncio.Queue()
        for item in enumerate(subjects):
            queue.put_nowait(item)

        async def worker():
            while True:
                try:
                    index, subject = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await one(index, subject)

        tasks = []
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                for _ in range(min(CATALOG_WORKERS, len(subjects))):
                    tasks.append(asyncio.create_task(worker()))
                await asyncio.gather(*tasks)
        except TimeoutError:
            raise Fault("timeout", 503) from None
        finally:
            # Cancelling the caller stops these children too: nothing keeps reading after the page
            # stopped waiting, and no outbound slot is left behind.
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        return results, failures
