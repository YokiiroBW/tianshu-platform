"""Deployment rules for the read-only persona page: one narrow window, never a grant.

This module is pure configuration and contract reading. It knows nothing about HTTP, aiohttp,
sessions, the console or the page: it validates the two deployment sections that decide what the
persona page may reach, verifies the read-only candidate contract byte for byte, and owns the one
page cursor this product issues. `web_personas` (the adapter) and `platform.service` (the
composition root) both read the resulting rule, so a bad narrowing is fatal where it is configured
instead of becoming a surprise at read time.

Three properties are load-bearing, and each one only ever narrows:

* **The candidate is explicit or absent.** There is no "load it unverified" mode. The manifest
  hash, the status word, the allowed consumer task and the producer commit are checked first, then
  every file the manifest names is hashed, then both schemas are compiled. A deployment that
  enables the page without a verified candidate is refused; one that leaves the page disabled is
  simply not a persona deployment.
* **The candidate belongs to a rehearsal.** It is loaded only in `local_rehearsal`, so a
  `service_https` deployment can never quietly read an unpublished draft contract.
* **The subject list is bounded and closed.** `allowed_subjects` is an explicit 1..64 entry
  allowlist of actor ids; the page never enumerates another product's global catalogue and never
  widens itself from a response.

The page cursor is process-local and signed, and it is bound to the authority fingerprint, the
connection, the exact allowed-subject set, the page name and the page position. A restart, a
configuration change, a credential rotation or a revoked login therefore invalidates it outright:
the page reopens its first page instead of stitching a newer directory onto an older position.
"""

import base64
import hashlib
import hmac
import os
import re
import string
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, ValidationError

from .contracts import Fault, canonical, digest, loads, require

# --------------------------------------------------------------------- candidate

CANDIDATE_MANIFEST_SHA256 = "06a4e2c2be73952f9c369f4c1a540f7a6394a4f54b267e5487d23d64cd97e8fb"
CANDIDATE_MANIFEST_KEYS = {
    "package",
    "version",
    "status",
    "allowed_consumer_task",
    "producer_commit",
    "operations",
    "production_publish_authorized",
    "joint_runtime_acceptance",
    "sample_count",
    "hash_basis",
    "sha256",
}
CANDIDATE_STATUS = "candidate_not_published"
CANDIDATE_CONSUMER_TASK = "TS-025"
CANDIDATE_PRODUCER_COMMIT = "0a775631366f1b7c4bab94ac5b16e67aa15ee1ab"
CANDIDATE_FILES = ("examples.json", "README.md", "request.schema.json", "response.schema.json")
CANDIDATE_SCHEMAS = {"request.schema.json": "request", "response.schema.json": "response"}
# The four read operations this consumer profile may forward. Nothing else is ever sent.
CANDIDATE_OPERATIONS = ("get", "history_page", "revision", "compare")
MANIFEST_FILE = "manifest.json"

# ------------------------------------------------------------------- shape limits

CONNECTION_SETTING_KEYS = {"base_url", "token_env", "ca_file", "timeout_seconds"}
CONNECTION_LIMIT = 1
TIMEOUT_DEFAULT = 10
TIMEOUT_MAXIMUM = 10
PAGE_SETTING_KEYS = {
    "enabled",
    "connection_id",
    "allowed_subjects",
    "candidate_directory",
    "allow_candidate",
}
SUBJECT_LIMIT = 64
CURSOR_LIMIT = 2048
HISTORY_KINDS = ("revisions", "publications", "approvals", "rollbacks")
CATALOG_PAGE = 20
HISTORY_PAGE = 20
CATALOG_WORKERS = 4
CATALOG_SUBJECTS_PER_PAGE = 20
TOKEN_ENV_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}")

# One cursor is two unpadded base64url segments joined by one dot, so the whole token is ASCII
# and every segment stays inside this alphabet. Anything else is refused before it reaches
# base64, `hmac` or a comparison, where it would raise something this product cannot map.
CURSOR_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")
CURSOR_FIELDS = ("kind", "authority", "connection", "subjects", "page", "position")


class Candidate:
    """One verified candidate package: hashes proven, schemas compiled, nothing else."""

    def __init__(self, directory, manifest, request_validator, response_validator):
        self.directory = directory
        self.manifest = manifest
        self.request_validator = request_validator
        self.response_validator = response_validator

    @property
    def word(self):
        """One short, non-secret word for exactly this verified package."""
        return digest({"manifest": CANDIDATE_MANIFEST_SHA256, "sha256": self.manifest["sha256"]})


def _read_checked(path, expected=None):
    """One contract byte read: CRLF normalised, hash proven, never a second source of truth."""
    raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        raise Fault("dependency_unavailable", 503)
    return raw


def load_candidate(directory):
    """Verify and load the read-only candidate package, or refuse to run at all.

    Every refusal is the same honest word: this deployment cannot read personas through an
    unverified contract, and claiming otherwise would be worse than having no page.
    """
    root = Path(directory)
    if not root.is_absolute() or not root.is_dir():
        raise Fault("dependency_unavailable", 503)
    try:
        manifest = loads(_read_checked(root / MANIFEST_FILE, CANDIDATE_MANIFEST_SHA256))
        require(
            isinstance(manifest, dict) and set(manifest) == CANDIDATE_MANIFEST_KEYS,
            "dependency_unavailable",
            503,
        )
        require(manifest["status"] == CANDIDATE_STATUS, "dependency_unavailable", 503)
        require(
            manifest["allowed_consumer_task"] == CANDIDATE_CONSUMER_TASK,
            "dependency_unavailable",
            503,
        )
        require(
            manifest["producer_commit"] == CANDIDATE_PRODUCER_COMMIT, "dependency_unavailable", 503
        )
        require(manifest["production_publish_authorized"] is False, "dependency_unavailable", 503)
        require(
            tuple(manifest["operations"]) == CANDIDATE_OPERATIONS, "dependency_unavailable", 503
        )
        hashes = manifest["sha256"]
        require(
            isinstance(hashes, dict) and set(hashes) == set(CANDIDATE_FILES),
            "dependency_unavailable",
            503,
        )
        schemas = {}
        for name in CANDIDATE_FILES:
            raw = _read_checked(root / name, hashes[name])
            if name in CANDIDATE_SCHEMAS:
                schemas[CANDIDATE_SCHEMAS[name]] = loads(raw)
    except Fault:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise Fault("dependency_unavailable", 503) from None
    return Candidate(
        root,
        manifest,
        Draft202012Validator(schemas["request"]),
        Draft202012Validator(schemas["response"]),
    )


# ------------------------------------------------------------------ configuration


def _text(value, limit, code="invalid_input", status=400):
    require(isinstance(value, str) and 0 < len(value) <= limit, code, status)
    require(all(ord(char) >= 32 and char != "\x7f" for char in value), code, status)
    return value


def validate_connection(connection_id, entry, check):
    """One `persona_connections` entry: HTTPS only, bounded timeout, absolute CA, own secret.

    The address rule is the one this product already reviews elsewhere (`transport.core_settings`),
    and the timeout is tightened here rather than relaxed: one persona read is a single bounded
    page, never a long poll. A bool is not a number and is refused like any other wrong value.
    """
    check("common#id", connection_id)
    require(isinstance(entry, dict) and set(entry) <= CONNECTION_SETTING_KEYS, "invalid_input", 400)
    require({"base_url", "token_env"} <= set(entry), "invalid_input", 400)
    _text(entry["base_url"], 2048)
    url = urlsplit(entry["base_url"])
    require(
        url.scheme == "https"
        and bool(url.hostname)
        and url.username is None
        and url.password is None
        and url.path in {"", "/"}
        and not url.query
        and not url.fragment,
        "invalid_input",
        400,
    )
    require(TOKEN_ENV_PATTERN.fullmatch(entry["token_env"]) is not None, "invalid_input", 400)
    if "ca_file" in entry:
        require(
            isinstance(entry["ca_file"], str) and Path(entry["ca_file"]).is_absolute(),
            "invalid_input",
            400,
        )
    timeout = entry.get("timeout_seconds", TIMEOUT_DEFAULT)
    require(type(timeout) in (int, float) and 0 < timeout <= TIMEOUT_MAXIMUM, "invalid_input", 400)


def connections(raw, check, other_token_envs=()):
    """The registered connection table, at most one entry, never a second reading identity.

    The persona credential is its own registered deployment credential: a variable already held
    by another service, or by the browser's own operator, is never reused here.
    """
    require(raw is None or isinstance(raw, dict), "invalid_input", 400)
    table = {} if raw is None else dict(raw)
    require(len(table) <= CONNECTION_LIMIT, "invalid_input", 400)
    others = set(other_token_envs)
    used = set()
    for connection_id, entry in table.items():
        validate_connection(connection_id, entry, check)
        require(entry["token_env"] not in others, "invalid_input", 400)
        require(entry["token_env"] not in used, "invalid_input", 400)
        used.add(entry["token_env"])
    return table


def page_configuration(section, connection_table, mode, check):
    """Validate `web_personas` and return the frozen rule the page reads.

    Narrowing only. A disabled section is a deployment without this page; an enabled one must
    name a registered connection, an explicit 1..64 subject allowlist and a verified candidate
    package, and only a `local_rehearsal` deployment may load that candidate at all.
    """
    require(
        isinstance(section, dict)
        and set(section) <= PAGE_SETTING_KEYS
        and {"connection_id", "allowed_subjects"} <= set(section),
        "invalid_input",
        400,
    )
    require(type(section.get("enabled", False)) is bool, "invalid_input", 400)
    require(type(section.get("allow_candidate", False)) is bool, "invalid_input", 400)
    enabled = section.get("enabled", False)
    allow_candidate = section.get("allow_candidate", False)
    directory = section.get("candidate_directory")
    require(directory is None or isinstance(directory, str), "invalid_input", 400)
    if directory is not None:
        require(Path(directory).is_absolute(), "invalid_input", 400)
    connection_id = _text(section["connection_id"], 128)
    require(connection_id in connection_table, "invalid_input", 400)
    subjects = section["allowed_subjects"]
    require(
        isinstance(subjects, list)
        and 1 <= len(subjects) <= SUBJECT_LIMIT
        and len(set(subjects)) == len(subjects),
        "invalid_input",
        400,
    )
    for subject in subjects:
        check("common#id", subject)
    rule = {
        "enabled": enabled,
        "connection_id": connection_id,
        "allowed_subjects": tuple(sorted(subjects)),
        "candidate": None,
        "reason": "personas_disabled" if not enabled else "ready",
    }
    if not enabled:
        # Not a persona deployment; the section is still validated so a typo cannot look ready.
        return rule
    require(allow_candidate, "invalid_input", 400)
    require(directory is not None, "invalid_input", 400)
    # A candidate is a rehearsal read of an unpublished contract; a production TLS deployment
    # must never load it, whatever the section asks for.
    require(mode == "local_rehearsal", "invalid_input", 400)
    rule["candidate"] = load_candidate(directory)
    return rule


def subject_digest(allowed_subjects):
    """One non-secret word for exactly this closed subject set."""
    return digest(list(allowed_subjects))


# ---------------------------------------------------------------------- cursors


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _segment(value):
    require(isinstance(value, str) and value and value.isascii(), "invalid_input", 400)
    require(all(character in CURSOR_ALPHABET for character in value), "invalid_input", 400)
    return value


def issue_cursor(secret, binding):
    """One signed token over the exact fact a continuation cursor is bound to."""
    require(isinstance(binding, dict) and set(binding) == set(CURSOR_FIELDS), "invalid_input", 400)
    payload = _b64(canonical(binding).encode("utf-8"))
    tag = _b64(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())
    token = payload + "." + tag
    require(len(token) <= CURSOR_LIMIT, "invalid_input", 400)
    return token


class PageCursor:
    """The one owner of this page's continuation cursors: signed, bound, process-local."""

    def __init__(self):
        # One process-local signing key. It is never persisted, so a cursor issued by an earlier
        # process is refused rather than silently reinterpreted against a new directory.
        self.secret = os.urandom(32)

    def _expected(self, kind, authority, connection_id, subjects, page, position):
        return {
            "kind": kind,
            "authority": authority,
            "connection": connection_id,
            "subjects": subject_digest(subjects),
            "page": page,
            "position": position,
        }

    def issue(self, kind, authority, connection_id, subjects, page, position):
        """One cursor for exactly this page at exactly this position."""
        return issue_cursor(
            self.secret, self._expected(kind, authority, connection_id, subjects, page, position)
        )

    def read(self, token, kind, authority, connection_id, subjects, page):
        """The signed position one cursor continues from, or `0` when the page starts fresh.

        The signature is proved first, so a forged or truncated token never reaches the JSON
        decoder; a token that verifies but was issued for another authority, connection, subject
        set or page is one honest conflict, never a silent restart at page one.
        """
        if token is None:
            return 0
        require(isinstance(token, str) and 0 < len(token) <= CURSOR_LIMIT, "invalid_input", 400)
        payload, dot, tag = token.partition(".")
        require(bool(dot) and bool(payload) and bool(tag), "invalid_input", 400)
        _segment(payload)
        _segment(tag)
        signature = _b64(hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(tag, signature):
            raise Fault("cursor_conflict", 409)
        try:
            document = loads(_unb64(payload))
        except (ValueError, UnicodeError, Fault):
            raise Fault("invalid_input", 400) from None
        if not isinstance(document, dict) or set(document) != set(CURSOR_FIELDS):
            raise Fault("invalid_input", 400)
        position = document["position"]
        if type(position) is not int or position < 0:
            raise Fault("invalid_input", 400)
        expected = self._expected(kind, authority, connection_id, subjects, page, position)
        if document != expected:
            raise Fault("cursor_conflict", 409)
        return position


# ---------------------------------------------------------------------- requests


def validate_request(candidate, document):
    """Strictly validate one outgoing operation document against the candidate's own schema."""
    try:
        candidate.request_validator.validate(document)
    except (ValidationError, RecursionError):
        raise Fault("invalid_input", 400) from None
    return document


def validate_response(candidate, document):
    """Strictly validate one peer answer before any field of it is read."""
    try:
        candidate.response_validator.validate(document)
    except (ValidationError, RecursionError):
        raise Fault("invalid_upstream", 502) from None
    return document


def request_document(
    operation,
    subject,
    request_id,
    *,
    kind=None,
    cursor=None,
    revision_id=None,
    left=None,
    right=None,
):
    """The one place an outgoing document is built, so no adapter can widen the wire shape."""
    document = {"operation": operation, "subject": subject, "request_id": request_id}
    if operation == "history_page":
        document["kind"] = kind
        document["limit"] = HISTORY_PAGE
        if cursor is not None:
            document["cursor"] = cursor
    elif operation == "revision":
        document["revision_id"] = revision_id
    elif operation == "compare":
        document["left"] = left
        document["right"] = right
    return document
