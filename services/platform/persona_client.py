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
  `count == entries.length`, the `has_more`/`next_cursor` relationship and (for errors) the echoed
  `request_id` are all proved before any field is read. A wrong answer is `invalid_upstream`, never
  a success and never an empty page.
* **Failures are translated, never invented.** A peer code this consumer profile does not know
  becomes one honest `dependency_unavailable`; a permission or connection failure is never turned
  into an empty result.

The client owns no rule about what a browser may ask for and no rule about who may ask. It is a
transport boundary: `web_personas` decides scope, and `persona_page_config` owns the shapes.
"""

import asyncio
import ssl
import uuid

import aiohttp

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


class PersonaClient:
    """One registered connection: address, credential variable, CA, timeout and the candidate."""

    def __init__(self, connection_id, entry, candidate):
        self.connection_id = connection_id
        self.base_url = entry["base_url"].rstrip("/")
        self.token_env = entry["token_env"]
        self.ca_file = entry.get("ca_file")
        self.timeout = entry.get("timeout_seconds", DEADLINE_SECONDS)
        self.candidate = candidate
        self.slots = asyncio.Semaphore(UPSTREAM_LIMIT)

    def credential(self):
        """The registered reading identity's own credential, resolved at call time.

        A revoked, rotated or missing variable is a refusal here, never a cached value: no earlier
        session may keep reading because it once held a working secret.
        """
        value = secret(self.token_env)
        require(value is not None, "dependency_unavailable", 503)
        return value

    def _tls(self):
        try:
            return ssl.create_default_context(cafile=self.ca_file)
        except (OSError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None

    async def call(self, document):
        """Send one operation document and return the peer's verified answer.

        The document is built from `persona_page_config` and validated against the candidate's own
        request schema before it leaves this process, so no adapter can widen the wire shape.
        """
        operation = document.get("operation")
        require(operation in CANDIDATE_OPERATIONS, "invalid_input", 400)
        validate_request(self.candidate, document)
        token = self.credential()
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                # Waiting for an outbound slot is part of the same bounded wait: a queued read
                # never gets its own fresh ten seconds.
                async with self.slots:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False
                    ) as session:
                        async with session.post(
                            self.base_url + "/internal/v1/persona/manage",
                            json=document,
                            headers={"Authorization": "Bearer " + token},
                            ssl=self._tls(),
                            allow_redirects=False,
                        ) as response:
                            answer = await self._read(response, operation)
        except TimeoutError:
            raise Fault("timeout", 503) from None
        except (aiohttp.ClientError, OSError, ssl.SSLError):
            raise Fault("dependency_unavailable", 503) from None
        return self._verify(answer, document)

    async def _read(self, response, operation):
        """Bounded stream read, then parse. The byte budget is applied to real bytes.

        A peer answer past the budget is stopped mid-stream and reported as what it is: an answer
        this product cannot use. The page-response budget is the adapter's own, and a refused
        upstream read never becomes either a partial page or an empty one.
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
            return self._fault(answer, response.status, operation)
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

    def _fault(self, answer, status, operation):
        """One peer refusal, echoed only when this product owns the code and the answer agrees."""
        code = answer.get("code")
        require(isinstance(code, str) and code in KNOWN_CODES, "dependency_unavailable", 503)
        require(status == KNOWN_CODES[code], "invalid_upstream", 502)
        validate_response(self.candidate, answer)
        raise Fault(code, status)

    # ------------------------------------------------------------------ verification

    def _verify(self, answer, document):
        """Prove the answer answers this exact question before any field of it is read."""
        operation = document["operation"]
        validate_response(self.candidate, answer)
        require(answer["operation"] == operation, "invalid_upstream", 502)
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
        """
        require(isinstance(subjects, (list, tuple)) and len(subjects) <= 20, "invalid_input", 400)
        results = [None] * len(subjects)
        failures = [None] * len(subjects)

        async def one(index, subject):
            try:
                results[index] = await self.call(
                    {"operation": "get", "subject": subject, "request_id": request_id()}
                )
            except Fault as error:
                # One subject's refusal or absence is kept as that exact fault; the caller decides
                # whether the page can still be shown, and a refusal is never an empty row.
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
