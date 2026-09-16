"""Same-origin local household login. No browser authority reaches internal RPCs.

Sessions are process-local and die on restart. Dialogue is explicitly configured
and uses the published Core snapshot and source protocols, never simulated chat.
"""

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .auth import secret
from .contracts import Fault, digest, loads, require
from .home import Home
from .tasks import Tasks
from .transport import CoreFault
from .web_dialogue import WebDialogue
from .web_models import WebModels
from .web_sender import WebSender

COOKIE = "tianshu_session"
SESSION_TTL = 8 * 3600
LOGIN_TTL = 600
MODELS_PREFIX = "/api/web/models/"
HOME_PREFIX = "/api/web/home/"
TASKS_PREFIX = "/api/web/tasks/"


def password_hash(password, salt=None):
    """Fixed scrypt cost; salt and verifier only, never the password, are persisted."""
    require(isinstance(password, str) and 12 <= len(password) <= 256, "invalid_input", 400)
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt-v1$" + salt.hex() + "$" + key.hex()


class WebConsole:
    def __init__(self, platform):
        self.platform = platform
        self.config = platform.settings.get("web")
        self.sessions = {}
        self.failures = []
        self.login_lock = asyncio.Lock()
        self.clock = time.monotonic
        self.models = None
        if self.config is None:
            # Model management lives behind the console; without one it has no entry at all.
            return
        c = self.config
        require(
            isinstance(c, dict)
            and set(c) - {"dialogue_enabled"}
            == {
                "origin",
                "username",
                "password_hash",
                "principal",
                "input_entries",
                "static_directory",
            },
            "invalid_input",
            400,
        )
        require(type(c.get("dialogue_enabled", False)) is bool, "invalid_input", 400)
        url = urlsplit(c["origin"])
        require(
            url.scheme == ("http" if platform.auth.mode == "local_rehearsal" else "https")
            and url.hostname
            and not url.username
            and not url.password
            and not url.path
            and not url.query
            and not url.fragment,
            "invalid_input",
            400,
        )
        if platform.auth.mode == "local_rehearsal":
            require(url.hostname == "127.0.0.1", "invalid_input", 400)
        require(
            isinstance(c["username"], str) and 1 <= len(c["username"]) <= 128, "invalid_input", 400
        )
        version, salt, key = c["password_hash"].split("$")
        require(
            version == "scrypt-v1"
            and len(bytes.fromhex(salt)) == 16
            and len(bytes.fromhex(key)) == 32,
            "invalid_input",
            400,
        )
        self.salt = bytes.fromhex(salt)
        principal = platform.auth.principals.get(c["principal"])
        require(
            principal and principal["kind"] == "operator" and principal["service"] == "platform",
            "invalid_input",
            400,
        )
        require(
            {"source.register", "source.dispatch", "mapping.prepare", "origin.issue"}
            <= set(principal["actions"]),
            "invalid_input",
            400,
        )
        require(
            isinstance(c["input_entries"], list)
            and 1 <= len(c["input_entries"]) <= 32
            and len(set(c["input_entries"])) == len(c["input_entries"]),
            "invalid_input",
            400,
        )
        for entry_id in c["input_entries"]:
            entry = platform.sources.entries.get(entry_id)
            require(
                entry
                and entry["owner"] == c["principal"]
                and entry["account"] == principal["account"]
                and entry["audience"] == "self_private",
                "invalid_input",
                400,
            )
        self.static = Path(c["static_directory"]).resolve()
        require(
            Path(c["static_directory"]).is_absolute() and (self.static / "index.html").is_file(),
            "invalid_input",
            400,
        )
        self.dialogue = WebDialogue(platform)
        self.sender = WebSender(platform)
        self.models = WebModels(platform, self)
        # Device control carries its own unlock; it is never derived from model management.
        self.home = Home(platform, self)
        # The task centre only reads the ledgers the two modules above already own.
        self.tasks = Tasks(platform, self)

    def verify_password(self, password):
        """One fixed-cost verifier for the login form and the management unlock step."""
        require(isinstance(password, str) and 12 <= len(password) <= 256, "unauthorized", 401)
        candidate = password_hash(password, self.salt)
        return hmac.compare_digest(candidate, self.config["password_hash"])

    def authority(self):
        p = self.platform
        principal = p.auth.principals[self.config["principal"]]
        header = "Bearer " + (secret(principal["token_env"]) or "")
        with p.store.connect(write=True) as db:
            identity, _ = p.auth.authenticate(header, db, "source.register", operator=True)
            require(identity == self.config["principal"])
            fingerprint = digest(
                {
                    "policy": p.auth.policy_digest,
                    "effective": p.auth.effective_digest(),
                    "web": self.config,
                }
            )
            conversations = []
            for entry_id in self.config["input_entries"]:
                try:
                    entry = p.sources._entry(db, entry_id)
                    actors = []
                    for actor_entry in entry["actor_entries"]:
                        try:
                            actor = p.auth.entry(db, actor_entry)
                            p.auth.route(actor, "platform", "companion", "dialogue")
                            p.auth.route(actor, "companion", "memory", "dialogue")
                            actors.append(actor["actor_id"])
                        except Fault as exc:
                            if exc.status != 403:
                                raise
                    conversations.append(
                        {
                            "id": entry_id,
                            "label": entry["channel"]["channel_conversation_id"],
                            "actors": actors,
                        }
                    )
                except Fault as exc:
                    if exc.status != 403:
                        raise
        return fingerprint, conversations

    def session(self, request):
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        token = request.cookies.get(COOKIE, "")
        return token, self.sessions.get(digest(token))

    def issue(self, authenticated=False, fingerprint=None):
        require(len(self.sessions) < 128, "too_many_requests", 429)
        token = secrets.token_urlsafe(32)
        session = {
            "csrf": secrets.token_urlsafe(32),
            "authenticated": authenticated,
            "fingerprint": fingerprint,
            "expires": self.clock() + (SESSION_TTL if authenticated else LOGIN_TTL),
        }
        self.sessions[digest(token)] = session
        return token, session

    def set_cookie(self, response, token, authenticated):
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=self.platform.auth.mode == "service_https",
            samesite="Strict",
            path="/",
            max_age=SESSION_TTL if authenticated else LOGIN_TTL,
        )

    async def handle(self, request):
        try:
            response = await self.route(request)
        except web.HTTPRequestEntityTooLarge:
            response = web.json_response({"code": "budget_exceeded"}, status=413)
        except CoreFault as exc:
            response = web.json_response(exc.document, status=exc.status)
        except Fault as exc:
            response = web.json_response({"code": exc.code}, status=exc.status)
            if request.path == "/api/web/session" and exc.status in {401, 403}:
                self.sessions.pop(digest(request.cookies.get(COOKIE, "")), None)
                response.del_cookie(COOKIE, path="/")
        except (OSError, sqlite3.Error):
            response = web.json_response({"code": "dependency_unavailable"}, status=503)
        except (ValueError, TypeError, KeyError):
            response = web.json_response({"code": "invalid_input"}, status=400)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    async def route(self, request):
        require(self.config is not None, "web_not_configured", 503)
        require(request.host == urlsplit(self.config["origin"]).netloc, "forbidden", 403)
        require("Authorization" not in request.headers, "forbidden", 403)
        require(
            request.headers.get("Sec-Fetch-Site", "same-origin") in {"same-origin", "none"},
            "forbidden",
            403,
        )
        if not request.path.startswith("/api/web/"):
            require(request.method in {"GET", "HEAD"}, "not_found", 404)
            name = "index.html" if request.path == "/" else request.path.lstrip("/")
            target = (self.static / name).resolve()
            require(target.is_relative_to(self.static) and target.is_file(), "not_found", 404)
            return web.FileResponse(target)
        token, session = self.session(request)
        if request.path == "/api/web/session" and request.method == "GET":
            if session is None:
                token, session = self.issue()
            result = {"authenticated": False, "csrf": session["csrf"]}
            if session["authenticated"]:
                fingerprint, conversations = self.authority()
                require(session["fingerprint"] == fingerprint, "session_expired", 401)
                result.update(
                    authenticated=True,
                    username=self.config["username"],
                    conversations=conversations,
                    dialogue={
                        "available": self.dialogue.available(),
                        "code": "ready" if self.dialogue.available() else "core_web_not_connected",
                        "model": "not_configured"
                        if not self.dialogue.model_configured()
                        else "unverified",
                    },
                )
            response = web.json_response(result)
            # Never extend server-side absolute expiry by polling.
            self.set_cookie(response, token, session["authenticated"])
            return response
        require(request.method == "POST", "not_found", 404)
        require(request.headers.getall("Origin", []) == [self.config["origin"]], "forbidden", 403)
        require(session is not None, "session_expired", 401)
        csrf = request.headers.get("X-CSRF-Token", "")
        require(csrf.isascii() and hmac.compare_digest(csrf, session["csrf"]), "forbidden", 403)
        require(
            request.content_type == "application/json"
            and request.headers.get("Content-Encoding", "identity") == "identity",
            "invalid_input",
            400,
        )
        data = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.content.iter_chunked(4096):
                    data.extend(chunk)
                    require(len(data) <= 16384, "budget_exceeded", 413)
        except TimeoutError:
            raise Fault("timeout", 408) from None
        body = loads(bytes(data))
        require(isinstance(body, dict), "invalid_input", 400)
        if request.path == "/api/web/login":
            require(set(body) == {"username", "password"}, "invalid_input", 400)
            require(
                isinstance(body["username"], str)
                and len(body["username"]) <= 128
                and isinstance(body["password"], str)
                and 12 <= len(body["password"]) <= 256,
                "unauthorized",
                401,
            )
            async with self.login_lock:
                self.failures = [t for t in self.failures if t > self.clock() - 60]
                require(len(self.failures) < 5, "too_many_requests", 429)
                valid = await asyncio.to_thread(self.verify_password, body["password"])
                valid &= hmac.compare_digest(
                    body["username"].encode(), self.config["username"].encode()
                )
                if not valid:
                    self.failures.append(self.clock())
                    raise Fault("unauthorized", 401)
                fingerprint, _ = self.authority()
                require(
                    self.sessions.get(digest(token)) is session
                    and session["expires"] > self.clock(),
                    "session_expired",
                    401,
                )
                self.sessions.pop(digest(token), None)
                token, session = self.issue(True, fingerprint)
            response = web.json_response({"authenticated": True, "csrf": session["csrf"]})
            self.set_cookie(response, token, True)
            return response
        if request.path == "/api/web/logout":
            require(body == {}, "invalid_input", 400)
            self.sessions.pop(digest(token), None)
            response = web.json_response({"authenticated": False})
            response.del_cookie(
                COOKIE,
                path="/",
                samesite="Strict",
                secure=self.platform.auth.mode == "service_https",
                httponly=True,
            )
            return response
        require(session["authenticated"], "unauthorized", 401)
        fingerprint, _ = self.authority()
        require(session["fingerprint"] == fingerprint, "session_expired", 401)
        if request.path.startswith(MODELS_PREFIX):
            # Management authority is server-side session state, never a browser claim.
            result = await self.models.route(request.path, body, session)
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = self.authority()
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            return web.json_response(result)
        operation = {
            "/api/web/messages": self.dialogue.send,
            "/api/web/snapshot": self.dialogue.snapshot,
            "/api/web/cancel": self.dialogue.cancel,
        }.get(request.path)
        if operation:
            require(self.dialogue.available(), "core_web_not_connected", 503)
            result = await operation(body)
            # A revoked/expired login cannot receive results from an in-flight read.
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = self.authority()
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            return web.json_response(result)
        if request.path.startswith(HOME_PREFIX):
            result = await self.home.route(request.path, body, session)
            # The same re-check: a session revoked while HA was being asked gets no reading.
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = self.authority()
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            return web.json_response(result)
        if request.path.startswith(TASKS_PREFIX):
            result = self.tasks.route(request.path, body, session)
            # A read-only projection still belongs to the session that asked for it.
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = self.authority()
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            return web.json_response(result)
        raise Fault("not_found", 404)


if __name__ == "__main__":
    import getpass

    password = getpass.getpass("New local administrator password (12-256 characters): ")
    require(password == getpass.getpass("Confirm password: "), "invalid_input", 400)
    print(password_hash(password))
