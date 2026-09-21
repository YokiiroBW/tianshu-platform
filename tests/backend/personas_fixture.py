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
CANDIDATE_MANIFEST_SHA256 = "06a4e2c2be73952f9c369f4c1a540f7a6394a4f54b267e5487d23d64cd97e8fb"
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
    browser fixture does not need a second environment variable to state the same fact.
    """
    override = os.environ.get("TS025_CANDIDATE_DIR")
    if override:
        directory = Path(override).resolve()
    else:
        workspace, _ = _workspace()
        directory = workspace / "contracts/persona-management/candidate-v1"
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
        self.status = {"not_found": 404, "version_conflict": 409, "unavailable": 503}.get(code, 400)
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
            if request.headers.get("Authorization") != "Bearer " + self.token:
                return web.json_response(Refusal("unauthorized").wire(), status=401)
            document = loads(raw)
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
# `python tests/backend/personas_fixture.py` serves three consoles from one process, each with its
# own isolated SQLite files, plus one synthetic persona peer over real TLS. The browser therefore
# sees only same-origin answers, and each deployment state is a real deployment setting rather than
# a stubbed response in the page. Nothing here is a product credential, a real account or a device.

BROWSER_PEER_PORT = 4823
BROWSER_PORTS = {"ready": 4820, "unconfigured": 4821, "forbidden": 4822}
BROWSER_SUBJECTS = tuple(
    sorted(
        ("actor:alpha", "actor:beta", "actor:broken", "actor:missing")
        + tuple("actor:extra-%02d" % index for index in range(1, 19))
    )
)
BROWSER_REVISIONS = 25


def browser_personas():
    """A directory wide enough to page, with one character per interesting state."""
    state = Personas(tuple(name for name in BROWSER_SUBJECTS if name != "actor:missing"))
    for index in range(BROWSER_REVISIONS - 2):
        state.draft(
            "actor:alpha",
            {
                "persona": "甲的人格第 %d 版" % (index + 2),
                "tone": "平静" if index % 2 else "温和",
                "style": "简短",
                "address": "你",
            },
        )
    # A last revision that drops one field and adds one this page does not interpret.
    final = state.draft(
        "actor:alpha",
        {"persona": "甲的人格最新一版", "tone": "正式", "style": "简短", "extension_flag": "on"},
    )
    for index in range(3):
        revision = state.subjects["actor:alpha"]["revisions"][index + 1]
        state.publish("actor:alpha", revision, "第 %d 次合成发布" % (index + 1))
        state.approve("actor:alpha", revision, reason="第 %d 次合成批准" % (index + 1))
    state.approve("actor:alpha", final, decision="rejected", reason="合成驳回")
    state.rollback("actor:alpha", state.subjects["actor:alpha"]["revisions"][1])
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
        if name not in {"ready", "unreadable"}:
            return web.json_response({"code": "invalid_input"}, status=400)
        double.scenario = name
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
    """One console per deployment state: ready, never configured, and an operator without the read."""
    import tempfile as _tempfile

    from web_fixtures import web_settings

    settings = web_settings(
        _tempfile.mkdtemp(dir=directory),
        origin="http://127.0.0.1:%d" % BROWSER_PORTS.get(mode, BROWSER_PORTS["ready"]),
        static=ROOT / "apps/web/dist",
    )
    settings["persona_connections"] = {
        CONNECTION: {
            "base_url": base_url,
            "token_env": "TS025_PERSONA_ADMIN",
            "ca_file": str(ca),
        }
    }
    if mode == "unconfigured":
        return settings
    if mode == "ready":
        settings["principals"]["admin"]["actions"] += ["persona.read"]
    settings["web_personas"] = page_section(BROWSER_SUBJECTS)
    return settings


async def serve():
    """Three consoles and one peer in one process; every state is a real deployment setting."""
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

    with tempfile.TemporaryDirectory(prefix="ts025-browser-") as directory:
        root = Path(directory)
        certificate, key = tls_material(root)
        double = Synthetic(browser_personas())
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        peer = web.AppRunner(peer_app(double), access_log=None)
        await peer.setup()
        await web.TCPSite(peer, "127.0.0.1", BROWSER_PEER_PORT, ssl_context=context).start()

        consoles = []
        for mode, port in BROWSER_PORTS.items():
            settings = browser_settings(
                root, mode, "https://127.0.0.1:%d" % BROWSER_PEER_PORT, certificate
            )
            runner = web.AppRunner(create_app(Platform(settings)), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            consoles.append(runner)
        print(
            "personas fixture: peer on %d, consoles %s"
            % (BROWSER_PEER_PORT, ", ".join(f"{m}={p}" for m, p in BROWSER_PORTS.items())),
            flush=True,
        )
        try:
            await asyncio.Event().wait()
        finally:
            for runner in consoles:
                await runner.cleanup()
            await peer.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())
