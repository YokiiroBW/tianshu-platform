"""Isolated pieces for the TS-025 joint acceptance: a fixed real Core, over real HTTPS.

This module owns everything the joint test must not invent for itself:

* **One fixed producer revision.** The companion Core is exported from its own repository at the
  exact producer commit the verified candidate names, with `merge-base --is-ancestor` proving that
  commit is on `main`. The export and its marker live under this worktree's ignored `.runtime`, so
  another product's working tree is read and never written, and a second run with a different
  commit is refused instead of silently reusing the first snapshot.
* **One real transport.** The Core is served by uvicorn on a real loopback socket with a real TLS
  certificate generated into a temporary directory. Nothing is installed into a system trust store
  and no request is faked at the ASGI layer.
* **One deployment document.** `core_config` writes the JSON the Core reads for itself: an isolated
  SQLite file, the published `text-dialogue/v1` contracts and synthetic characters only.
* **One honest absence.** Every missing piece - no `git`, no repository, a commit that is not on
  `main`, no companion runtime dependencies - is a stated skip carrying the exact reason. A joint
  acceptance that cannot be run is never reported as passed.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Both `python tests/backend/personas_fixture.py` (the browser fixture) and an importing test suite
# must find the platform package: this file lives in tests/backend, the product lives at the root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from services.platform.contracts import canonical, digest, loads  # noqa: E402

RUNTIME = ROOT / ".runtime/companion-joint"
SOURCES = RUNTIME / "sources"
# The exact producer revision the verified candidate names; not a branch, not "latest".
COMPANION_COMMIT = "0a775631366f1b7c4bab94ac5b16e67aa15ee1ab"
COMPANION_TASK = "TS-025"
CANDIDATE_MANIFEST_SHA256 = "c738630f6641f47d86d352c929752af86d7287098b55aa90741dd4d5ca8cef5e"
SUBJECTS = ("actor:alpha", "actor:beta", "actor:gamma")


class Unavailable(Exception):
    """One exact reason this joint acceptance cannot run here; never a silent pass."""


def _workspace():
    context = json.loads((ROOT / ".runtime/workspace-context.json").read_text(encoding="utf-8"))
    return Path(context["workspace"]), context.get("task")


def git_executable():
    return os.environ.get("TS025_GIT") or shutil.which("git")


def companion_repo():
    override = os.environ.get("TS025_COMPANION_REPO")
    if override:
        return Path(override)
    workspace, _ = _workspace()
    return workspace / "projects/tianshu-companion"


def _git(*arguments, text=True):
    tool = git_executable()
    if tool is None:
        raise Unavailable("git is not installed and TS025_GIT is not set")
    return subprocess.run([tool, *arguments], check=True, capture_output=True, text=text).stdout


def export_companion():
    """The fixed Core revision in this worktree's own ignored runtime, byte for byte."""
    repo = companion_repo()
    if not (repo / ".git").exists():
        raise Unavailable(f"no companion repository at {repo}")
    _git("-C", str(repo), "merge-base", "--is-ancestor", COMPANION_COMMIT, "main")
    destination = SOURCES / "companion"
    marker = destination / ".ts025-commit"
    if destination.exists():
        if not marker.is_file() or marker.read_text(encoding="ascii") != COMPANION_COMMIT:
            raise Unavailable(f"{destination} holds a different snapshot")
        return destination
    raw = _git("-C", str(repo), "archive", "--format=tar", COMPANION_COMMIT, text=False)
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(destination, filter="data")
    marker.write_text(COMPANION_COMMIT, encoding="ascii")
    return destination


def companion_dependencies():
    override = os.environ.get("TS025_COMPANION_DEPS")
    directory = Path(override) if override else ROOT / ".runtime/companion-deps"
    if not (directory / "uvicorn").is_dir() or not (directory / "fastapi").is_dir():
        raise Unavailable(
            f"no companion runtime dependencies at {directory} "
            "(pip install --target <dir> -r <core>/requirements-dev.txt)"
        )
    return directory


def companion_source():
    """Everything the joint acceptance needs, or one stated reason it cannot run."""
    source = export_companion()
    dependencies = companion_dependencies()
    for entry in (str(dependencies), str(source / "src")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    return source


def contracts_directory():
    workspace, _ = _workspace()
    override = os.environ.get("TS012_CONTRACT_DIR")
    return Path(override) if override else workspace / "contracts/text-dialogue/v1"


def candidate_digest():
    """One word for the package this page verifies: manifest hash, then every named file."""
    directory = Path(os.environ["TS025_CANDIDATE_DIR"])
    combined = hashlib.sha256()
    for name in sorted(
        (
            "manifest.json",
            "examples.json",
            "README.md",
            "request.schema.json",
            "response.schema.json",
        )
    ):
        combined.update(
            name.encode("ascii")
            + b":"
            + hashlib.sha256((directory / name).read_bytes().replace(b"\r\n", b"\n")).digest()
        )
    return combined.hexdigest()


def core_config(directory, token_env, roles):
    """The deployment document the real Core reads for itself: isolated, synthetic, explicit."""
    return {
        "contracts_path": str(contracts_directory()),
        "database_path": str(Path(directory) / "core.sqlite"),
        "config_version": "ts025-joint-1",
        "personas": {"admin_token_env": token_env, "roles": roles},
        "callers": {},
        "services": {},
    }


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class CoreServer:
    """The real Core application on a real TLS socket, started and stopped in this loop."""

    def __init__(self, server, task, url):
        self.server = server
        self.task = task
        self.url = url

    @classmethod
    async def start(cls, app, certificate, key):
        import uvicorn

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        url = "https://127.0.0.1:%d" % sock.getsockname()[1]
        config = uvicorn.Config(
            app,
            access_log=False,
            log_level="warning",
            lifespan="on",
            ws="none",
            ssl_certfile=str(certificate),
            ssl_keyfile=str(key),
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    await task
                    raise Unavailable("the companion Core exited before it served a request")
                await asyncio.sleep(0.01)
        return cls(server, task, url)

    async def close(self):
        if self.task.done():
            return
        self.server.should_exit = True
        try:
            async with asyncio.timeout(5):
                await asyncio.shield(self.task)
        except TimeoutError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def call_core(url, ca, token, document, *, timeout=10):
    """One operator call straight to the Core, for seeding only; the page never does this."""
    import ssl

    import aiohttp

    context = ssl.create_default_context(cafile=str(ca))
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
    ) as session:
        async with session.post(
            url + "/internal/v1/persona/manage",
            json=document,
            headers={"Authorization": "Bearer " + token},
            ssl=context,
        ) as response:
            answer = json.loads(await response.read())
            if response.status != 200:
                raise Unavailable(f"seed call failed: {response.status} {answer}")
            return answer


# --------------------------------------------------------------------------- controlled double
#
# One double, two readers: the shape/identity/budget suite in `test_personas_web.py` and the browser
# fixture in `serve()` below both use this state, so neither invents a persona surface of its own.
# It keeps real revisions, real version numbers and real version-bound cursors and refuses exactly
# what the producer refuses; it is still a double, and the joint suite uses the fixed real Core.

PERSONA_TOKEN = "synthetic-ts025-persona-admin-credential"
CONNECTION = "characters-local"
PERSONA_ENV = {"TS025_PERSONA_ADMIN": PERSONA_TOKEN}
REVISION_FIELDS = ("persona", "tone", "style", "address")
CONTENT_SUFFIX = "persona-revision-v1"
UNIT_SUBJECTS = ("actor:a", "actor:b")


def candidate_directory():
    """The verified candidate package, or one stated reason it cannot be read.

    `TS025_CANDIDATE_DIR` wins; otherwise the workspace's own contract directory is used, so the
    browser fixture does not need a second environment variable to state the same fact. The package
    this card consumes is the coordinator's corrected revision `candidate-v1-r2`; the original
    `candidate-v1` bytes are left exactly as they were.
    """
    override = os.environ.get("TS025_CANDIDATE_DIR")
    if override:
        directory = Path(override).resolve()
    else:
        workspace, _ = _workspace()
        directory = workspace / "contracts/persona-management/candidate-v1-r2"
    if not (directory / "manifest.json").is_file():
        raise Unavailable(f"no candidate package at {directory}")
    return str(directory)


def fingerprint(content):
    """The producer's own content fingerprint; the double must speak the same identity."""
    return digest({"suffix": CONTENT_SUFFIX, "content": content})


def revision_id(subject, content, parent):
    return digest(
        {"suffix": CONTENT_SUFFIX, "subject": subject, "content": content, "parent": parent}
    )


class Refusal(Exception):
    def __init__(self, code):
        self.code = code
        self.status = {
            "not_found": 404,
            "version_conflict": 409,
            "unavailable": 503,
            "forbidden": 403,
            "unauthorized": 401,
        }.get(code, 400)
        super().__init__(code)

    def wire(self, request_id=None):
        return {
            "schema_version": 1,
            "request_id": request_id or "req:synthetic",
            "code": self.code,
            "execution_state": "not_started",
            "retryable": False,
        }


class Personas:
    """Small but real persona state: revisions, pointers and version-bound pages."""

    def __init__(self, subjects=UNIT_SUBJECTS):
        self.subjects = {}
        for subject in subjects:
            first = self.write(subject, {"persona": "角色 " + subject[-1].upper()}, None)
            self.subjects[subject] = {
                "version": 1,
                "published": first,
                "draft": None,
                "retired": False,
                "revisions": [first],
                "publications": [first],
                "approvals": [],
                "rollbacks": [],
            }

    def write(self, subject, content, parent):
        content = {key: content[key] for key in sorted(content)}
        return {
            "revision_id": revision_id(subject, content, parent),
            "fingerprint": fingerprint(content),
            "content": content,
            "parent": parent,
            "source": "initial_config" if parent is None else "editor",
            "operator": "deployment" if parent is None else "operator:fixture",
            "note": None,
            "created_at": 1789976090.25,
            "approved_by": None,
            "approved_at": None,
            "approval_reason": None,
        }

    def draft(self, subject, content):
        """Advance one character; every cursor issued before this becomes stale."""
        state = self.subjects[subject]
        revision = self.write(subject, content, state["published"]["revision_id"])
        state["draft"] = revision
        state["revisions"].append(revision)
        state["version"] += 1
        return revision

    def publish(self, subject, revision, reason):
        """Record one publication of an existing revision, as the producer's own history does."""
        state = self.subjects[subject]
        row = dict(revision)
        row["note"] = reason
        row["generation"] = len(state["publications"]) + 1
        row["publication_kind"] = "seed" if revision["parent"] is None else "publish"
        state["publications"].append(row)
        state["published"] = revision
        state["version"] += 1
        return row

    def approve(self, subject, revision, decision="approved", reason="合成批准"):
        state = self.subjects[subject]
        row = dict(revision)
        row["decision"] = decision
        row["approval_reason"] = reason
        state["approvals"].append(row)
        if decision == "approved":
            revision["approved_by"] = "operator:fixture"
            revision["approved_at"] = revision["created_at"]
            revision["approval_reason"] = reason
        state["version"] += 1
        return row

    def rollback(self, subject, target, reason="合成回退"):
        state = self.subjects[subject]
        superseded = state["draft"] or state["published"]
        row = dict(target)
        row["note"] = reason
        row["restored_revision"] = target["revision_id"]
        row["target_revision"] = target["revision_id"]
        row["superseded_revision"] = superseded["revision_id"] if superseded else None
        state["rollbacks"].append(row)
        state["version"] += 1
        return row

    def persona(self, subject):
        state = self.subjects[subject]
        published, draft = state["published"], state["draft"]
        if state["retired"]:
            view = "retired"
        elif draft is None:
            view = "published"
        elif draft["approved_at"] is not None:
            view = "approved"
        else:
            view = "draft"
        return {
            "subject": subject,
            "version": state["version"],
            "state": view,
            "published_revision": published["revision_id"] if published else None,
            "draft_revision": draft["revision_id"] if draft else None,
            "published": published,
            "published_at": 1789976090.25 if published else None,
            "publisher": "deployment" if published else None,
            "draft": draft,
            "pending_approval": False,
            "retired": state["retired"],
            "retired_at": None,
            "imported": 1,
            "updated_at": 1789976090.25,
        }

    def history(self, subject, kind, position=0):
        state = self.subjects[subject]
        rows = {
            "revisions": state["revisions"],
            "publications": state["publications"],
            "approvals": state["approvals"],
            "rollbacks": state["rollbacks"],
        }[kind]
        return rows[position:]

    def owner(self, wanted):
        """Which character a revision belongs to, or None; Core refuses a cross-character read."""
        for subject, state in self.subjects.items():
            if any(row["revision_id"] == wanted for row in state["revisions"]):
                return subject
        return None

    def revision_of(self, wanted):
        owner = self.owner(wanted)
        if owner is None:
            raise Refusal("not_found")
        return self.subjects[owner]["revisions"]


class Synthetic:
    """A real HTTPS persona surface with fault injection; never a source of truth."""

    def __init__(self, personas=None, token=PERSONA_TOKEN):
        self.personas = personas if personas is not None else Personas()
        self.token = token
        self.calls = []
        self.active = 0
        self.peak = 0
        self.mode = "normal"
        # One named deployment state the browser fixture may switch at run time.
        self.scenario = "ready"
        self.delay = 0.0
        self.secret = os.urandom(32)

    # ------------------------------------------------------------------ wire

    async def handle(self, request):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            raw = await request.read()
            document = loads(raw) if raw else {}
            if request.headers.get("Authorization") != "Bearer " + self.token:
                # A real peer answers the request it was given, correlation id included.
                return web.json_response(
                    Refusal("unauthorized").wire(document.get("request_id")), status=401
                )
            self.calls.append(document)
            if self.mode == "disconnect":
                request.transport.abort()
                return web.Response()
            if self.delay:
                await asyncio.sleep(self.delay)
            return await self.answer(document)
        except Refusal as refusal:
            return web.json_response(refusal.wire(), status=refusal.status)
        finally:
            self.active -= 1

    def usable(self, subject):
        """A character whose answer cannot be used, exactly as an unhealthy peer would answer."""
        return subject.endswith(":broken") or self.scenario == "unreadable"

    async def answer(self, document):
        request_id = document.get("request_id")
        operation = document.get("operation")
        subject = document.get("subject")
        if self.mode == "garbage":
            return web.Response(body=b"not json at all", content_type="application/json")
        if self.mode == "oversized":
            body = json.dumps({"pad": "x" * (300 * 1024)}, ensure_ascii=False).encode()
            return web.Response(body=body, content_type="application/json")
        try:
            if operation not in {"get", "history_page", "revision", "compare"}:
                raise Refusal("invalid_input")
            if subject not in self.personas.subjects:
                raise Refusal("not_found")
            if self.mode == "one-forbidden" and subject.endswith(":b"):
                # One character this deployment may not read: not an absence, and never allowed to
                # make the rest of the page look like a finished directory.
                raise Refusal("forbidden")
            if self.usable(subject):
                # A truncated document: the page must say the answer was unusable, not that the
                # character has no history.
                return web.Response(body=b'{"schema_version": 1,', content_type="application/json")
            if operation == "get":
                answer = {
                    "schema_version": 1,
                    "operation": "get",
                    "persona": self.personas.persona(subject),
                }
            elif operation == "history_page":
                answer = self.page_document(document)
            elif operation == "revision":
                answer = self.revision_document(document)
            else:
                answer = self.compare_document(document)
        except Refusal as refusal:
            # An error packet that carries somebody else's correlation id: the reader may not read
            # it as this call's refusal, so the double can prove the association is checked.
            if self.mode == "wrong-request-id":
                return web.json_response(
                    refusal.wire("web-persona-somebody-else"), status=refusal.status
                )
            return web.json_response(refusal.wire(request_id), status=refusal.status)
        if self.mode == "wrong-operation":
            answer["operation"] = "catalog"
        if self.mode == "wrong-subject":
            answer["subject"] = "actor:z"
            if "persona" in answer:
                answer["persona"]["subject"] = "actor:z"
        if self.mode == "broken-count":
            answer["count"] = answer.get("count", 0) + 1
        if self.mode == "broken-cursor":
            answer["has_more"] = True
            answer["next_cursor"] = None
        if self.mode == "wrong-kind":
            answer["kind"] = "rollbacks"
        if self.mode == "wrong-revision":
            answer["revision"]["revision_id"] = "0" * 64
        return web.json_response(answer)

    # -------------------------------------------------------------- documents

    def cursor(self, payload):
        raw = base64.urlsafe_b64encode(canonical(payload).encode()).decode().rstrip("=")
        tag = (
            base64.urlsafe_b64encode(hmac.new(self.secret, raw.encode(), hashlib.sha256).digest())
            .decode()
            .rstrip("=")
        )
        return raw + "." + tag

    def read_cursor(self, token, subject, kind, limit):
        payload, _, tag = token.partition(".")
        if not payload or not tag:
            raise Refusal("invalid_input")
        expected = (
            base64.urlsafe_b64encode(
                hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()
            )
            .decode()
            .rstrip("=")
        )
        if not hmac.compare_digest(tag, expected):
            raise Refusal("invalid_input")
        document = loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if document.get("subject") != subject or document.get("kind") != kind:
            raise Refusal("invalid_input")
        if document.get("limit") != limit:
            raise Refusal("invalid_input")
        if document.get("version") != self.personas.subjects[subject]["version"]:
            raise Refusal("version_conflict")
        return document["position"]

    def page_document(self, document):
        subject, kind = document["subject"], document["kind"]
        limit = document["limit"]
        position = 0
        if document.get("cursor") is not None:
            position = self.read_cursor(document["cursor"], subject, kind, limit)
        rows = self.personas.history(subject, kind)[position:]
        kept = rows[:limit]
        more = len(kept) < len(rows)
        version = self.personas.subjects[subject]["version"]
        return {
            "schema_version": 1,
            "operation": "history_page",
            "subject": subject,
            "kind": kind,
            "persona_version": version,
            "consistency": "version_bound",
            "limit": limit,
            "count": len(kept),
            "entries": [self.entry(kind, row) for row in kept],
            "has_more": more,
            "next_cursor": (
                self.cursor(
                    {
                        "operation": "history_page",
                        "subject": subject,
                        "kind": kind,
                        "limit": limit,
                        "version": version,
                        "position": position + len(kept),
                    }
                )
                if more
                else None
            ),
        }

    def entry(self, kind, row):
        """The candidate's own per-kind entry shape, exactly as the producer writes it."""
        if kind == "revisions":
            return {
                "revision_id": row["revision_id"],
                "fingerprint": row["fingerprint"],
                "parent": row["parent"],
                "source": row["source"],
                "operator": row["operator"],
                "note": row["note"],
                "created_at": row["created_at"],
                "decision": row.get("decision"),
                "decided_by": row["approved_by"],
                "decided_at": row["approved_at"],
            }
        if kind == "publications":
            return {
                "publication_id": digest(
                    {"suffix": "publication-v1", "revision": row["revision_id"]}
                ),
                "revision_id": row["revision_id"],
                "supersedes": row["parent"],
                "generation": row.get("generation", 1),
                "kind": row.get("publication_kind")
                or ("seed" if row["parent"] is None else "publish"),
                "state": row.get("publication_state", "published"),
                "operator": row["operator"],
                "reason": row.get("reason")
                or row.get("note")
                or ("initial_config import" if row["parent"] is None else "合成发布"),
                "created_at": row["created_at"],
            }
        if kind == "approvals":
            return {
                "approval_id": digest({"suffix": "approval-v1", "revision": row["revision_id"]}),
                "revision_id": row["revision_id"],
                "fingerprint": row["fingerprint"],
                "decision": row.get("decision", "approved"),
                "state": "approved"
                if row.get("decision", "approved") == "approved"
                else "rejected",
                "operator": "operator:fixture",
                "reason": row.get("approval_reason") or "合成批准",
                "created_at": row["created_at"],
            }
        return {
            "rollback_id": digest({"suffix": "rollback-v1", "revision": row["revision_id"]}),
            "restored_revision": row.get("restored_revision", row["revision_id"]),
            "target_revision": row.get("target_revision", row["revision_id"]),
            "superseded_revision": row.get("superseded_revision", row["revision_id"]),
            "state": row.get("rollback_state", "applied"),
            "operator": "operator:fixture",
            "reason": row.get("reason") or row.get("note") or "合成回退",
            "created_at": row["created_at"],
        }

    def revision_document(self, document):
        subject, wanted = document["subject"], document["revision_id"]
        if self.personas.owner(wanted) not in {None, subject}:
            # The producer's own word for a revision that belongs to another character.
            raise Refusal("invalid_input")
        for row in self.personas.revision_of(wanted):
            if row["revision_id"] == wanted:
                state = self.personas.persona(subject)
                return {
                    "schema_version": 1,
                    "operation": "revision",
                    "subject": subject,
                    "revision": row,
                    "is_published": state["published_revision"] == wanted,
                    "is_draft": state["draft_revision"] == wanted,
                    "persona_version": state["version"],
                    "state": state["state"],
                    "published_revision": state["published_revision"],
                    "draft_revision": state["draft_revision"],
                }
        raise Refusal("not_found")

    def compare_document(self, document):
        subject, left, right = document["subject"], document["left"], document["right"]
        for wanted in (left, right):
            if self.personas.owner(wanted) not in {None, subject}:
                raise Refusal("invalid_input")
        rows = {row["revision_id"]: row for row in self.personas.revision_of(left)}
        for wanted in (left, right):
            if wanted not in rows:
                raise Refusal("not_found")
        state = self.personas.persona(subject)
        names = sorted(
            name
            for name in set(rows[left]["content"]) | set(rows[right]["content"])
            if name not in REVISION_FIELDS
        )
        return {
            "schema_version": 1,
            "operation": "compare",
            "subject": subject,
            "left": {k: v for k, v in rows[left].items() if k != "content"},
            "right": {k: v for k, v in rows[right].items() if k != "content"},
            "comparison_scope": list(REVISION_FIELDS),
            "fields": self.fields(rows[left]["content"], rows[right]["content"]),
            "additional_fields_present": bool(names),
            "additional_fields_changed": self.extra(rows[left], rows[right]),
            "additional_field_names": names,
            "identical_revision": left == right,
            "content_identical": rows[left]["fingerprint"] == rows[right]["fingerprint"],
            "persona_version": state["version"],
            "state": state["state"],
            "published_revision": state["published_revision"],
            "draft_revision": state["draft_revision"],
        }

    def fields(self, left, right):
        result = {}
        for name in REVISION_FIELDS:
            here, there = name in left, name in right
            if not here and not there:
                change = "unchanged"
            elif not here:
                change = "added"
            elif not there:
                change = "removed"
            else:
                change = (
                    "unchanged" if canonical(left[name]) == canonical(right[name]) else "modified"
                )
            result[name] = {
                "presence": {"left": here, "right": there},
                "change": change,
                "left": left.get(name),
                "right": right.get(name),
            }
        return result

    def extra(self, left, right):
        def rest(row):
            return {k: v for k, v in row["content"].items() if k not in REVISION_FIELDS}

        return canonical(rest(left)) != canonical(rest(right))


def page_section(subjects=UNIT_SUBJECTS, **override):
    section = {
        "enabled": True,
        "connection_id": CONNECTION,
        "allowed_subjects": list(subjects),
        "candidate_directory": candidate_directory(),
        "allow_candidate": True,
    }
    section.update(override)
    return section


def persona_settings(directory, base_url, ca, **override):
    """Console settings plus one registered read-only persona page over the synthetic peer.

    Each call gets its own subdirectory: the shared console fixture creates its static directory on
    demand, and a test that builds several settings objects must not reuse one.
    """
    import tempfile as _tempfile

    from web_fixtures import web_settings

    settings = web_settings(_tempfile.mkdtemp(dir=directory))
    settings["principals"]["admin"]["actions"] += ["persona.read"]
    settings["persona_connections"] = {
        CONNECTION: {"base_url": base_url, "token_env": "TS025_PERSONA_ADMIN", "ca_file": ca}
    }
    settings["web_personas"] = page_section(**override)
    return settings


def copy_candidate(directory, **changes):
    """A byte-identical copy of the verified package, optionally with one field changed."""
    source = Path(candidate_directory())
    root = Path(directory) / "candidate-v1"
    root.mkdir(parents=True)
    for name in ("examples.json", "README.md", "request.schema.json", "response.schema.json"):
        (root / name).write_bytes((source / name).read_bytes())
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(changes)
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root


# ------------------------------------------------------------------------- browser fixture
#
# `python tests/backend/personas_fixture.py` serves four consoles from one process, each with its
# own isolated SQLite files, plus one synthetic persona peer over real TLS. The browser therefore
# sees only same-origin answers, and each deployment state is a real deployment setting rather than
# a stubbed response in the page. Nothing here is a product credential, a real account or a device.
#
# The MAIN positive chain is the real producer: the `core` console's registered persona connection
# points at the fixed companion Core (commit `COMPANION_COMMIT`) exported into this worktree's own
# `.runtime` and served by uvicorn on a real loopback socket with the same temporary certificate the
# probe peer uses. Every directory, history, revision and comparison the browser reads on 4820 is
# therefore the producer's own answer, not a double's. The synthetic double on `BROWSER_PEER_PORT`
# keeps only the states a healthy producer cannot be asked to produce - a refused character, an
# unreadable answer, a mixed-up correlation id - so it is extra fault coverage and never the chain
# the browser proves the page against.

BROWSER_PEER_PORT = 4823
# Four deployment states, one console each. `core` is the real producer over HTTPS; `faulty` is the
# synthetic double, which exists only to make the page's own fault handling reachable from a browser.
BROWSER_PORTS = {"core": 4820, "unconfigured": 4821, "forbidden": 4822, "faulty": 4824}
# One console is served from the double, so the browser can drive a fault the real Core never gives.
BROWSER_FAULT_MODE = "faulty"
BROWSER_CONNECTION = "characters-local"
BROWSER_TOKEN_ENV = "TS025_PERSONA_ADMIN"
# The 21 characters the real Core is seeded with: the three named ones plus a wide tail that makes
# the directory page at 20 rows per page. `actor:missing` is deliberately NOT seeded - it is the
# allowlisted character this deployment declares and the producer never held.
BROWSER_EXTRA = tuple("actor:extra-%02d" % index for index in range(1, 19))
BROWSER_CORE_SUBJECTS = ("actor:alpha", "actor:beta", "actor:epsilon", *BROWSER_EXTRA)
BROWSER_MISSING = "actor:missing"
# 22 allowed subjects: 21 the Core holds plus the one it never held, so page 1 is full and page 2
# carries a real correlated absence.
BROWSER_SUBJECTS = tuple(sorted((*BROWSER_CORE_SUBJECTS, BROWSER_MISSING)))
# The allowed count the directory states, and the two page sizes the browser asserts.
BROWSER_ALLOWED = 22
BROWSER_PER_PAGE = 20
# 1 initial import revision + 22 decided drafts + 1 rollback revision + 1 trailing unapproved draft.
BROWSER_REVISIONS = 25
# The three named characters the real Core is seeded with for the revision/compare assertions.
BROWSER_ALPHA = "actor:alpha"
BROWSER_BETA = "actor:beta"
BROWSER_EPSILON = "actor:epsilon"
# The synthetic double's own character list: 21 it really holds plus the one it never holds, so the
# fault console's allowlist is the same 22 the real console declares and its second page carries the
# same correlated absence. `actor:alpha` needs a real multi-page history and `actor:beta` needs a
# revision holding all three body states at once; the extra tail is what makes page 1 full, and one
# of those names ends in `:b` so the double's own `one-forbidden` mode has a character to refuse.
BROWSER_PEER_HELD = (
    BROWSER_ALPHA,
    BROWSER_BETA,
    BROWSER_EPSILON,
    *BROWSER_EXTRA[:-1],
    # Sorts second, so the character `one-forbidden` refuses is on page 1 and the fault really is
    # the whole directory's - a refusal on page 2 would leave page 1 readable and prove nothing.
    "actor:b",
)
BROWSER_PEER_SUBJECTS = tuple(sorted((*BROWSER_PEER_HELD, BROWSER_MISSING)))
# The extension field `actor:alpha`'s last decided revision adds, and the one it drops.
BROWSER_EXTENSION = "extension_flag"
# The temporary certificate this process generated, once `serve()` has made it. A test that must
# call the peer's TLS control path reads it here rather than guessing the process's temp directory.
BROWSER_CA = []


def browser_roles():
    """The 21 characters the real Core is declared with, exactly as the deployment states them.

    Three shapes are deliberate. `actor:epsilon` is the **string shorthand** (no `version`), which
    is the one corrected wire domain this card exists for: the Core records `imported: null` and the
    candidate reads that as the real value it is. `actor:beta` is declared with an explicit
    `"tone": null` and no `address` at all, so one real revision carries a present-and-null field
    next to an absent one without any test having to write it. The tail characters are ordinary
    versioned entries; they exist so the directory has a second page.
    """
    roles = {name: {"version": 1, "persona": "角色 %s 的初始人格" % name} for name in BROWSER_EXTRA}
    roles[BROWSER_ALPHA] = {
        "version": 1,
        "persona": "甲：初始人格",
        "tone": "平静",
        "style": "简短",
        "address": "你",
    }
    # A null for a non-persona text field and no `address` key: the producer keeps both facts
    # distinct, and the page's `present` list is what tells them apart.
    roles[BROWSER_BETA] = {"version": 3, "persona": "乙：另一位角色", "tone": None}
    # Declared without a version: the Core records `imported: null`, never a fabricated number.
    roles[BROWSER_EPSILON] = "戊：部署项未声明版本"
    return roles


def browser_personas():
    """A directory wide enough to page, with one character per interesting state.

    This is the **synthetic double's** state, used by the `faulty` console only. It keeps a real
    multi-page history for `actor:alpha`, a character whose revision shows all three body states at
    once (`tone` explicitly null, `style` the empty string, `address` absent - states the real Core
    holds only partly, because its own `normalize` refuses a blank string), and a tail wide enough
    that page 1 is full. `actor:broken` is deliberately absent: the page fails the WHOLE directory
    when a character's answer is unusable, so a permanently broken character would make the directory
    unreadable rather than prove anything. That fault is injected through the control path instead.
    """
    state = Personas(BROWSER_PEER_HELD)
    for index in range(BROWSER_REVISIONS - 2):
        state.draft(
            BROWSER_ALPHA,
            {
                "persona": "甲的人格第 %d 版" % (index + 2),
                "tone": "平静" if index % 2 else "温和",
                "style": "简短",
                "address": "你",
            },
        )
    # A last revision that drops two fields and adds one this page does not interpret.
    final = state.draft(
        BROWSER_ALPHA,
        {
            "persona": "甲的人格最新一版",
            "tone": "正式",
            "style": "简短",
            BROWSER_EXTENSION: "on",
        },
    )
    for index in range(3):
        revision = state.subjects[BROWSER_ALPHA]["revisions"][index + 1]
        state.publish(BROWSER_ALPHA, revision, "第 %d 次合成发布" % (index + 1))
        state.approve(BROWSER_ALPHA, revision, reason="第 %d 次合成批准" % (index + 1))
    state.approve(BROWSER_ALPHA, final, decision="rejected", reason="合成驳回")
    state.rollback(BROWSER_ALPHA, state.subjects[BROWSER_ALPHA]["revisions"][1])
    # `actor:beta` shows all three body states in one revision: a present-and-null `tone`, a
    # present-and-empty `style`, and no `address` key at all. The comparison partner is published
    # first, so the three-state revision is the one the directory still points at when the browser
    # reads it.
    other = state.draft(BROWSER_BETA, {"persona": "乙：对照正文", "tone": "轻快", "style": "简洁"})
    state.publish(BROWSER_BETA, other, "乙的对照发布")
    three_state = state.draft(BROWSER_BETA, {"persona": "乙：三态正文", "tone": None, "style": ""})
    state.publish(BROWSER_BETA, three_state, "乙的合成发布")
    return state


def peer_app(double):
    """The peer's own surface: the one product path, plus a control path only tests may call."""
    from aiohttp import web

    app = web.Application()
    app.router.add_post("/internal/v1/persona/manage", double.handle)

    async def read_scenario(request):
        return web.json_response({"scenario": double.scenario})

    async def write_scenario(request):
        document = await request.json()
        name = document.get("scenario")
        if name not in {"ready", "unreadable", "one-forbidden"}:
            return web.json_response({"code": "invalid_input"}, status=400)
        double.scenario = name
        # `one-forbidden` is the double's own `mode`, so the control path has to set both the named
        # deployment state and the mode that implements it. Resetting to `ready` clears both, so a
        # previous fault never survives into the next browser scenario.
        if name == "one-forbidden":
            double.mode = name
        elif double.mode == "one-forbidden":
            double.mode = "normal"
        return web.json_response({"scenario": double.scenario})

    app.router.add_get("/control/scenario", read_scenario)
    app.router.add_post("/control/scenario", write_scenario)
    return app


def tls_material(directory):
    """Real certificate material, generated by the tool interpreter that carries cryptography."""
    tool = os.environ.get("TS013_TLS_PYTHON")
    if not tool:
        raise Unavailable(
            "TS013_TLS_PYTHON is not set: the browser fixture needs it to generate a real "
            "loopback certificate (cryptography is not an application dependency)"
        )
    script = str(Path(__file__).with_name("make_tls_fixture.py"))
    subprocess.run([tool, script, str(directory)], check=True)
    return Path(directory) / "localhost.pem", Path(directory) / "localhost-key.pem"


def browser_settings(directory, mode, base_url, ca):
    """One console per deployment state, each with its own origin and its own registered connection.

    The states differ only in what this deployment actually exposes: `core` is the real producer
    over HTTPS with the read action granted, `unconfigured` has no `web_personas` section at all,
    `forbidden` configures the page but an operator without `persona.read`, and `faulty` reads the
    synthetic double so a browser can reach the page's own fault handling.
    """
    import tempfile as _tempfile

    from web_fixtures import web_settings

    settings = web_settings(
        _tempfile.mkdtemp(dir=directory),
        origin="http://127.0.0.1:%d" % BROWSER_PORTS[mode],
        static=ROOT / "apps/web/dist",
    )
    settings["persona_connections"] = {
        BROWSER_CONNECTION: {
            "base_url": base_url,
            "token_env": BROWSER_TOKEN_ENV,
            "ca_file": str(ca),
        }
    }
    if mode == "unconfigured":
        # No section at all: the page states `personas_not_configured`, never an empty directory.
        return settings
    if mode != "forbidden":
        # Only the `forbidden` console is configured with an operator that lacks `persona.read`, so
        # its page is a real permission refusal rather than a directory with nothing in it. Every
        # other configured state carries the action the page needs, including the fault console.
        settings["principals"]["admin"]["actions"] += ["persona.read"]
    settings["web_personas"] = page_section(
        BROWSER_SUBJECTS if mode != BROWSER_FAULT_MODE else BROWSER_PEER_SUBJECTS,
        connection_id=BROWSER_CONNECTION,
    )
    return settings


async def seed_core(url, ca, token):
    """Seed the real Core through its own `manage` operations, and report what it really holds.

    Every fact the browser suite asserts about the positive chain is written here by the producer
    itself - `draft`/`approve`/`publish`/`rollback` with a `request_id`, an `operator`, the expected
    version and a reason - so nothing about the directory, the history, the revision bodies or the
    comparison is invented by a fixture. The interleaving is chosen to land on the exact history
    this card names:

    * `actor:alpha` ends with **25** revisions: the deployment import's initial one, 22 decided
      drafts, the one `rollback` itself writes, and one trailing unapproved draft;
    * **4** publications: the import seed, two explicit `publish` calls and the `rollback`;
    * **4** approvals: the two approvals those publishes required, one `reject`, and the approval
      row the producer's own `rollback` always writes with the restored revision;
    * **1** rollback row.

    The two revisions the compare assertion uses are drafted first and last: the first keeps
    `style` and `tone` so an older pair shares a field, the last drops `address` and adds
    `extension_flag`, so one comparison carries an `unchanged`, a `modified`, a `removed` field and
    an extra-field change at once.

    `actor:beta` is left published on its first drafted revision, which carries the deployment's
    explicit `tone: null` and has no `address` key; a second revision on the same character gives
    the comparison a field present on both sides.
    """
    alpha, beta = BROWSER_ALPHA, BROWSER_BETA
    seen = {"revisions": []}

    async def persona(subject):
        return (await call_core(url, ca, token, {"operation": "get", "subject": subject}))[
            "persona"
        ]

    async def draft(subject, content, tag, *, reason="合成起草"):
        state = await persona(subject)
        answer = await call_core(
            url,
            ca,
            token,
            {
                "operation": "draft",
                "subject": subject,
                "request_id": "browser-%s" % tag,
                "content": content,
                "operator": "operator:fixture",
                "expected": state["version"],
                "reason": reason,
                "note": "浏览器种子 %s" % tag,
            },
        )
        return answer["revision"]["revision_id"]

    async def move(operation, subject, revision_id, tag, reason):
        state = await persona(subject)
        return await call_core(
            url,
            ca,
            token,
            {
                "operation": operation,
                "subject": subject,
                "request_id": "browser-%s" % tag,
                "revision_id": revision_id,
                "operator": "operator:fixture",
                "expected": state["version"],
                "reason": reason,
            },
        )

    # 22 decided drafts, then the rollback and the trailing open draft make 25 revisions in all.
    drafts = []
    for number in range(2, 24):
        if number == 23:
            # The last revision drops `address` and adds an extension field this page names only.
            content = {
                "persona": "甲：末版正文，不再称呼",
                "tone": "正式",
                "style": "简短",
                BROWSER_EXTENSION: "on",
            }
        elif number == 2:
            # The earliest decidable revision keeps `style` and `tone`, so a comparison against the
            # last one has a field that really is unchanged on both sides.
            content = {
                "persona": "甲：第 2 版正文",
                "tone": "温和",
                "style": "简短",
                "address": "你",
            }
        else:
            content = {
                "persona": "甲：第 %d 版正文" % number,
                "tone": "温和" if number % 2 else "平静",
                "style": "简短" if number % 3 else "细致",
                "address": "你" if number % 4 else "您",
            }
        revision = await draft(alpha, content, "draft-%02d" % number)
        drafts.append(revision)
        # `approve` only ever decides the character's own pending draft, so each decision follows
        # the draft it decides. Two publish pairs and one rejection are all the approvals this
        # history may carry; the rollback below supplies the fourth.
        if number in (2, 3):
            rank = number - 1
            await move("approve", alpha, revision, "approve-%d" % rank, "第 %d 次合成批准" % rank)
            await move("publish", alpha, revision, "publish-%d" % rank, "第 %d 次合成发布" % rank)
        elif number == 4:
            await move("reject", alpha, revision, "reject-0", "合成驳回")
    seen["revisions"] = list(drafts)
    # The rollback replays the earliest published revision as a new one: it adds the 25th revision,
    # the fourth publication and the fourth approval row, all written by the producer itself.
    answer = await move("rollback", alpha, drafts[0], "rollback-0", "合成回退")
    seen["restored"] = answer["restored_revision"]
    # A fresh draft is left undecided, so the directory's "draft" word is the producer's own.
    seen["open_draft"] = await draft(
        alpha, {"persona": "甲：尚未批准的草稿", "tone": "低沉", "style": "简短"}, "draft-open"
    )
    # `actor:beta` is published on a revision whose body states two facts at once: `tone` is present
    # and explicitly null, and `address` is not there at all. A second revision on the same character
    # gives the comparison a field present on both sides, so `presence.left/right` reads false for
    # the absent `address` alone. The empty-string `style` state the card also names is NOT reachable
    # on this chain: the producer's own `normalize` refuses a blank string for any text field with
    # `invalid_input` (proved empirically), so that one state is carried by the synthetic double
    # instead - see `browser_personas`, where one revision holds all three at once.
    beta_other = await draft(
        beta, {"persona": "乙：对照正文", "tone": "轻快", "style": "简短"}, "beta-other"
    )
    await move("approve", beta, beta_other, "beta-approve-2", "乙的第二次合成批准")
    await move("publish", beta, beta_other, "beta-publish-2", "乙的第二次合成发布")
    beta_three = await draft(
        beta,
        {"persona": "乙：三态正文", "tone": None, "style": "简洁"},
        "beta-three",
    )
    await move("approve", beta, beta_three, "beta-approve", "乙的合成批准")
    await move("publish", beta, beta_three, "beta-publish", "乙的合成发布")
    seen["beta"] = {"three_state": beta_three, "other": beta_other, "published": beta_three}
    seen["alpha"] = {"first": drafts[0], "last": drafts[-1]}
    return seen


async def serve():
    """Four consoles and one peer in one process; every state is a real deployment setting.

    The `core` console reads the fixed real producer over real HTTPS; the `faulty` console reads the
    synthetic double. The two are started from the same process so the browser suite has one command
    to run, and the line printed below names the real Core URL and every console port so the suite
    reads the deployment it is about to drive from the fixture's own statement rather than a guess.
    """
    from aiohttp import web

    # `fixtures.py` reads the published contract path at import time, so state it first; the
    # browser fixture then reuses the same synthetic identities as the console fixture.
    os.environ.setdefault("TS012_CONTRACT_DIR", str(contracts_directory()))
    from fixtures import ENV
    from services.platform.server import create_app
    from services.platform.service import Platform

    if not (ROOT / "apps/web/dist/index.html").is_file():
        raise SystemExit("apps/web/dist is missing: run npm run build before the browser suite")
    os.environ.update(ENV)
    os.environ.update(PERSONA_ENV)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    # The real producer: the fixed companion commit, its own isolated database, its own roles.
    core_source = companion_source()
    assert core_source.is_dir() and core_source.name == "companion"

    with tempfile.TemporaryDirectory(prefix="ts025-browser-") as directory:
        root = Path(directory)
        certificate, key = tls_material(root)
        # The certificate is this process's own temporary loopback material, generated into the
        # directory above. A caller that must speak to the control path over TLS - which is a test
        # control, never a product route - reads the same path from here instead of guessing it.
        BROWSER_CA.append(certificate)
        core_config_path = root / "core.json"
        core_config_path.write_text(
            json.dumps(
                core_config(root, BROWSER_TOKEN_ENV, browser_roles()),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.environ["TIANSHU_COMPANION_CONFIG"] = str(core_config_path)
        from tianshu_companion.app import create_app as create_core

        core = await CoreServer.start(create_core(), certificate, key)
        try:
            seeded = await seed_core(core.url, certificate, PERSONA_TOKEN)
            double = Synthetic(browser_personas())
            import ssl

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            peer = web.AppRunner(peer_app(double), access_log=None)
            await peer.setup()
            await web.TCPSite(peer, "127.0.0.1", BROWSER_PEER_PORT, ssl_context=context).start()

            consoles = []
            for mode, port in BROWSER_PORTS.items():
                base_url = (
                    "https://127.0.0.1:%d" % BROWSER_PEER_PORT
                    if mode == BROWSER_FAULT_MODE
                    else core.url
                )
                settings = browser_settings(root, mode, base_url, certificate)
                settings["web"]["origin"] = "http://127.0.0.1:%d" % port
                runner = web.AppRunner(create_app(Platform(settings)), access_log=None)
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                consoles.append(runner)
            print(
                "personas fixture: core %s, peer %d, ca %s, consoles %s"
                % (
                    core.url,
                    BROWSER_PEER_PORT,
                    certificate,
                    ", ".join(f"{m}={p}" for m, p in BROWSER_PORTS.items()),
                ),
                flush=True,
            )
            print(
                "personas fixture: seeded %s"
                % json.dumps(
                    {
                        "subjects": len(BROWSER_CORE_SUBJECTS),
                        "allowed": len(BROWSER_SUBJECTS),
                        "alpha": seeded["alpha"],
                        "beta": seeded["beta"],
                        "restored": seeded["restored"],
                        "open_draft": seeded["open_draft"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            try:
                await asyncio.Event().wait()
            finally:
                for runner in consoles:
                    await runner.cleanup()
                await peer.cleanup()
        finally:
            await core.close()


if __name__ == "__main__":
    asyncio.run(serve())
