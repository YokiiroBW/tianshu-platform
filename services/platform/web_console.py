"""Same-origin local household login. No browser authority reaches internal RPCs.

Sessions are process-local and die on restart. Dialogue is explicitly configured
and uses the published Core snapshot and source protocols, never simulated chat.
"""

import asyncio
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import diagnostics_config
from .auth import secret
from .contracts import Fault, digest, loads, require
from .diagnostics import AUTH_SUCCEEDED
from .home import Home
from .provider_management import ProviderManagement
from .role_runtime import is_placeholder_actor
from .tasks import Tasks
from .transport import CoreFault
from .web_access import WebAccess
from .web_access_settings import WebAccessSettings
from .web_account import WebAccount, password_hash, validate_hash, verify_password
from .web_assets import WebAssets
from .web_connections import view as connections_view
from .web_content import WebContent
from .web_dialogue import WebDialogue
from .web_external import WebExternal
from .web_life_management import WebLifeManagement
from .web_memory import WebMemory
from .web_memory_links import WebMemoryLinks
from .web_models import WebModels
from .web_people_life import WebPeopleLife
from .web_persona_author import WebPersonaAuthor
from .web_personas import WebPersonas
from .web_readers import WebReader
from .web_sender import WebSender
from .web_skills import WebSkills
from .web_weather import WebWeather

COOKIE = "tianshu_session"
SESSION_TTL = 8 * 3600
LOGIN_TTL = 600
ANONYMOUS_SESSION_LIMIT = 32
AUTHENTICATED_SESSION_LIMIT = 128
# The one fact the serving boundary cannot work out for itself: whether this request really was
# authenticated. A cookie header, an Authorization header or a path that reached the console are all
# things a stranger can produce, so none of them is evidence; only this console knows when a password
# was verified or when a live session it issued after one was presented. It sets this key at exactly
# those points and nowhere else, and the boundary writes `http.auth.succeeded` only when it is set.
CONSOLE_AUTH = web.RequestKey("console_authenticated", str)
MODELS_PREFIX = "/api/web/models/"
PROVIDERS_PREFIX = "/api/web/providers/"
HOME_PREFIX = "/api/web/home/"
TASKS_PREFIX = "/api/web/tasks/"
PERSONAS_PREFIX = "/api/web/personas/"
# The asset page is assembled here as one route; its scope, rules and read record belong to
# `services.platform.web_assets`.
ASSETS_PREFIX = "/api/web/assets/"
KNOWLEDGE_PREFIX = "/api/web/knowledge/"
LIFE_PREFIX = "/api/web/life/"
MEMORY_PREFIX = "/api/web/memory/"
EXTERNAL_PREFIX = "/api/web/external/"
BOTS_PREFIX = "/api/web/bots/"
BOT_ADAPTERS_PREFIX = "/api/web/bot-adapters/"
BOT_OBSERVATION_PREFIX = "/api/web/bot-observation/"
QQ_ADMIN_PREFIX = "/api/web/qq-admin/"
MEDIA_PREFIX = "/api/web/media/"


class WebConsole:
    def __init__(self, platform, access=None):
        self.access = access or WebAccess(platform.settings)
        self.access_settings = WebAccessSettings(self, self.access)
        self.platform = platform
        self.account = WebAccount(platform.settings)
        self.config = platform.settings.get("web")
        if self.config is not None and self.access.current is not None:
            self.config = {**self.config, "origin": self.access.current["origin"]}
        if self.config is not None:
            self.input_entries = list(self.config["input_entries"])
        self.sessions = {}
        self.failures = []
        self.login_lock = asyncio.Lock()
        self.clock = time.monotonic
        self.models = None
        if self.config is None:
            # Model management lives behind the console; without one it has no entry at all.
            return
        c = self.config
        account_create = self.account.config is not None and self.account.config["mode"] == "create"
        credential_keys = set() if account_create else {"username", "password_hash"}
        require(
            isinstance(c, dict)
            and set(c) - {"dialogue_enabled"}
            == credential_keys
            | {
                "origin",
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
            (
                self.access.current is not None
                or url.scheme == ("http" if platform.auth.mode == "local_rehearsal" else "https")
            )
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
        if not account_create:
            require(
                isinstance(c["username"], str) and 1 <= len(c["username"]) <= 128,
                "invalid_input",
                400,
            )
            validate_hash(c["password_hash"])
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
        self.dialogue.input_entries = self.input_entries
        self.sender.input_entries = self.input_entries
        self.models = WebModels(platform, self)
        self.providers = ProviderManagement(platform, self)
        self.external = WebExternal(platform, self)
        # Device control carries its own unlock; it is never derived from model management.
        self.home = Home(platform, self)
        # The task centre only reads the ledgers the two modules above already own.
        self.tasks = Tasks(platform, self)
        # The asset page is a read-only window: it owns its own scope, rules and read record, and
        # this console only assembles its route below.
        self.assets = WebAssets(platform, self)
        platform.assets.external = self.external if self.external.catalog else None
        # The persona page is the same shape with a different peer: one registered read-only
        # window, one route prefix, and the session protection is the one above. It is given the
        # finished reader, the frozen rule and three narrow callbacks - never this console.
        self.personas = WebPersonas(
            configured=platform.settings.get("web_personas") is not None,
            rule=platform.personas,
            reader=platform.persona_reader(),
            authorised=self.persona_read_authorised,
            session_valid=self.session_valid,
            authority=lambda: self.authority()[0],
            run_local=platform.local_work.run,
        )
        self.persona_author = WebPersonaAuthor(self, self.personas)
        self.knowledge = WebReader("knowledge", platform, self)
        self.life = WebReader("life", platform, self)
        self.life_management = WebLifeManagement(platform, self)
        self.skills = WebSkills(self.life_management)
        self.content = WebContent(platform, self)
        self.memory_links = WebMemoryLinks(platform, self)
        self.people_life = WebPeopleLife(platform, self)
        self.weather = WebWeather(platform, self)
        self.memory = WebMemory(platform, self)
        self.role_runtime = platform.role_runtime
        self.qq_admin = platform.qq_admin
        from .media.web import WebMedia

        self.media = WebMedia(platform.media, self)
        if self.role_runtime.config is not None:
            self.role_runtime.console = self

    def verify_password(self, password):
        """One fixed-cost verifier for the login form and the management unlock step."""
        credential = self.credential()
        require(credential is not None, "setup_required", 409)
        return verify_password(password, credential["password_hash"])

    def credential(self):
        account = self.account.read()
        if account is not None:
            return {**account, "source": "account"}
        if self.account.config is not None and self.account.config["mode"] == "create":
            return None
        return {
            "username": self.config["username"],
            "password_hash": self.config["password_hash"],
            "version": 0,
            "source": "deployment",
        }

    def onboarding(self, authenticated):
        credential = self.credential()
        source = credential["source"] if credential else "setup"
        state = "create_admin" if credential is None else "sign_in"
        if authenticated:
            state = "claim_admin" if source == "deployment" else "ready"
        return {
            "state": state,
            "credential_source": source,
            "setup_token_required": credential is None,
        }

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
                    "web": {**p.settings["web"], "origin": self.config["origin"]},
                    "credential": self.credential(),
                }
            )
            conversations = []
            for entry_id in self.input_entries:
                try:
                    entry = p.sources._entry(db, entry_id)
                    actors = []
                    for actor_entry in entry["actor_entries"]:
                        try:
                            actor = p.auth.entry(db, actor_entry)
                            role = p.role_runtime.get(actor["actor_id"])
                            if is_placeholder_actor(actor["actor_id"], role):
                                continue
                            if role is not None and (
                                not p.role_runtime.active(actor["actor_id"])
                                or "dialogue" not in role["capabilities"]
                            ):
                                continue
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

    def session_valid(self, session):
        """The whole authority of one in-flight read: the same live session, pinned to the same
        deployment. A logout, an expiry, a revoked action, a rotated credential or a changed
        persona configuration all fail this one check."""
        if session is None or session["expires"] <= self.clock():
            return False
        if not any(item is session for item in tuple(self.sessions.values())):
            return False
        fingerprint, _ = self.authority()
        return session["fingerprint"] == fingerprint and self.session_live(session)

    def session_live(self, session):
        return (
            session is not None
            and session["expires"] > self.clock()
            and any(item is session for item in tuple(self.sessions.values()))
        )

    def persona_read_authorised(self):
        """May the current operator read personas? Asked again at every outbound step of a read.

        The deployment's local operator identity must be exactly that, and it must carry the
        explicit persona read action. Login, config.publish and device.control grant nothing. The
        answer is computed from live configuration, so revoking the action ends reads that are
        already in flight instead of only the next one.
        """
        principal = self.platform.auth.principals.get(self.config["principal"], {})
        return (
            principal.get("kind") == "operator"
            and principal.get("service") == "platform"
            and "persona.read" in set(principal.get("actions", []))
        )

    def persona_action_authorised(self, action):
        principal = self.platform.auth.principals.get(self.config["principal"], {})
        return (
            principal.get("kind") == "operator"
            and principal.get("service") == "platform"
            and action in {"persona.create", "persona.edit", "persona.apply"}
            and action in set(principal.get("actions", []))
        )

    def issue(self, authenticated=False, fingerprint=None):
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        matching = [(k, v) for k, v in self.sessions.items() if v["authenticated"] == authenticated]
        if authenticated:
            require(len(matching) < AUTHENTICATED_SESSION_LIMIT, "too_many_requests", 429)
        elif len(matching) >= ANONYMOUS_SESSION_LIMIT:
            candidates = [
                (k, v) for k, v in matching if v is not getattr(self, "authenticating", None)
            ]
            oldest = min(candidates, key=lambda pair: pair[1]["expires"])[0]
            self.sessions.pop(oldest)
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
            secure=urlsplit(self.config["origin"]).scheme == "https",
            samesite="Strict",
            path="/",
            max_age=SESSION_TTL if authenticated else LOGIN_TTL,
        )

    async def configure_account(self, request, body, token, session):
        require(self.account.config is not None, "setup_closed", 409)
        claiming = request.path == "/api/web/account/claim"
        expected = (
            {"username", "password", "current_password"}
            if claiming
            else {"username", "password", "setup_token"}
        )
        require(
            set(body) <= expected and {"username", "password"} <= set(body), "invalid_input", 400
        )
        if claiming:
            require(set(body) == expected, "invalid_input", 400)
            require(session["authenticated"], "unauthorized", 401)
        async with self.login_lock:
            self.failures = [t for t in self.failures if t > self.clock() - 60]
            require(len(self.failures) < 5, "too_many_requests", 429)
            self.authenticating = session
            try:
                credential = self.credential()
                if claiming:
                    require(
                        credential is not None and credential["source"] == "deployment",
                        "setup_closed",
                        409,
                    )
                    valid = await asyncio.to_thread(self.verify_password, body["current_password"])
                    if not valid:
                        self.failures.append(self.clock())
                        raise Fault("unauthorized", 401)
                else:
                    try:
                        self.account.check_setup_token(
                            body.get("setup_token"),
                            (
                                *self.platform.other_credentials,
                                diagnostics_config.resolve_ready_token_env(self.platform.settings),
                            ),
                        )
                    except Fault as exc:
                        if exc.status == 401:
                            self.failures.append(self.clock())
                        raise
                verifier = await asyncio.to_thread(password_hash, body["password"])
                fingerprint, _ = await self.platform.local_work.run(self.authority)
                require(self.session_live(session), "session_expired", 401)
                require(credential == self.credential(), "session_expired", 401)
                if claiming:
                    require(session["fingerprint"] == fingerprint, "session_expired", 401)
                await asyncio.to_thread(self.account.create, body["username"], verifier)
                # The transaction committed. Never revive the deployment password even if the
                # connection is lost now; a retry signs in with the newly chosen credentials.
                fingerprint, _ = await self.platform.local_work.run(self.authority)
                self.sessions.clear()
                token, session = self.issue(True, fingerprint)
            finally:
                self.authenticating = None
        request[CONSOLE_AUTH] = AUTH_SUCCEEDED
        response = web.json_response({"authenticated": True, "csrf": session["csrf"]})
        self.set_cookie(response, token, True)
        return response

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
        if request.path.startswith(PROVIDERS_PREFIX) and response.status >= 400:
            # The provider page has a frozen error envelope; other web APIs retain theirs.
            try:
                code = json.loads(response.body)["code"]
            except (ValueError, KeyError, TypeError):
                code = "dependency_unavailable"
            unknown = code in {"timed_out", "connection_failed", "result_unknown"} or (
                request.path == "/api/web/providers/test"
                and code in {"upstream_invalid", "upstream_rejected", "dependency_unavailable"}
            )
            response = web.json_response(
                {
                    "schema_version": 1,
                    "request_id": "request:" + secrets.token_hex(16),
                    "code": code,
                    "execution_state": "unknown" if unknown else "not_started",
                    "retryable": response.status == 503 and not unknown,
                },
                status=response.status,
            )
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
            if self.account.config is not None:
                result["onboarding"] = self.onboarding(session["authenticated"])
            if session["authenticated"]:
                fingerprint, conversations = await self.platform.local_work.run(self.authority)
                require(self.session_live(session), "session_expired", 401)
                require(session["fingerprint"] == fingerprint, "session_expired", 401)
                # A live session that was created by a verified password and still belongs to this
                # deployment: presenting it is authentication, and this is the only place a page
                # request can demonstrate that.
                request[CONSOLE_AUTH] = AUTH_SUCCEEDED
                result.update(
                    authenticated=True,
                    username=self.credential()["username"],
                    conversations=conversations,
                    dialogue={
                        "available": self.dialogue.available(),
                        "code": "ready" if self.dialogue.available() else "core_web_not_connected",
                        "actor_models": {
                            actor: await self.platform.local_work.run(
                                self.dialogue.model_configured, actor
                            )
                            for conversation in conversations
                            for actor in conversation["actors"]
                        },
                        "model": "not_configured"
                        if not await self.platform.local_work.run(self.dialogue.model_configured)
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
        if request.path.startswith("/api/web/content/upload/"):
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            request[CONSOLE_AUTH] = AUTH_SUCCEEDED
            result = await self.content.upload(request, session)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
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
                    require(
                        len(data)
                        <= (
                            6 * 1024 * 1024
                            if request.path.startswith(MEDIA_PREFIX)
                            else 1048576
                            if request.path.startswith(
                                (LIFE_PREFIX + "runtime/", "/api/web/content/")
                            )
                            else 262144
                            if request.path.startswith(PERSONAS_PREFIX)
                            else 16384
                        ),
                        "budget_exceeded",
                        413,
                    )
        except TimeoutError:
            raise Fault("timeout", 408) from None
        body = loads(bytes(data))
        require(isinstance(body, dict), "invalid_input", 400)
        if request.path in {"/api/web/setup", "/api/web/account/claim"}:
            return await self.configure_account(request, body, token, session)
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
                self.authenticating = session
                try:
                    credential = self.credential()
                    require(credential is not None, "setup_required", 409)
                    valid = await asyncio.to_thread(self.verify_password, body["password"])
                    valid &= hmac.compare_digest(
                        body["username"].encode(), credential["username"].encode()
                    )
                    if not valid:
                        self.failures.append(self.clock())
                        raise Fault("unauthorized", 401)
                    fingerprint, _ = await self.platform.local_work.run(self.authority)
                    require(credential == self.credential(), "session_expired", 401)
                    require(self.session_live(session), "session_expired", 401)
                    require(
                        self.sessions.get(digest(token)) is session
                        and session["expires"] > self.clock(),
                        "session_expired",
                        401,
                    )
                    self.sessions.pop(digest(token), None)
                    token, session = self.issue(True, fingerprint)
                finally:
                    self.authenticating = None
            # The operator's own password was just verified against this deployment's hash: a real
            # authentication happened in this request, whatever else the answer says.
            request[CONSOLE_AUTH] = AUTH_SUCCEEDED
            response = web.json_response({"authenticated": True, "csrf": session["csrf"]})
            self.set_cookie(response, token, True)
            return response
        if request.path == "/api/web/logout":
            require(body == {}, "invalid_input", 400)
            if session["authenticated"]:
                # Ending a session that was authenticated is itself an authenticated request; an
                # unauthenticated visitor logging out has proved nothing about identity.
                request[CONSOLE_AUTH] = AUTH_SUCCEEDED
            self.sessions.pop(digest(token), None)
            self.knowledge.forget_session(session)
            response = web.json_response({"authenticated": False})
            response.del_cookie(
                COOKIE,
                path="/",
                samesite="Strict",
                secure=urlsplit(self.config["origin"]).scheme == "https",
                httponly=True,
            )
            return response
        require(session["authenticated"], "unauthorized", 401)
        fingerprint, _ = await self.platform.local_work.run(self.authority)
        require(self.session_live(session), "session_expired", 401)
        require(session["fingerprint"] == fingerprint, "session_expired", 401)
        # The session is live, authenticated and still pinned to this deployment's current policy:
        # that, and only that, is what makes this an authenticated request rather than one that
        # merely carried a cookie. A later refusal in this same request answers 401/403 and is
        # recorded as a refusal, which the boundary gives precedence to.
        request[CONSOLE_AUTH] = AUTH_SUCCEEDED
        if request.path.startswith("/api/web/access/"):
            result = await self.access_settings.route(request.path, body, session)
            fingerprint, _ = await self.platform.local_work.run(self.authority)
            require(
                self.session_live(session) and session["fingerprint"] == fingerprint,
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith(MODELS_PREFIX):
            # Management authority is server-side session state, never a browser claim.
            result = await self.models.route(request.path, body, session)
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = await self.platform.local_work.run(self.authority)
            require(self.session_live(session), "session_expired", 401)
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            return web.json_response(result)
        if request.path.startswith(PROVIDERS_PREFIX):
            try:
                result = await self.providers.route(request.path, body, session)
            except Fault:
                await self.platform.local_work.run(self.providers._gate, session)
                raise
            _, current = self.session(request)
            require(current is session and self.session_live(session), "session_expired", 401)
            await self.platform.local_work.run(self.providers._gate, session)
            return web.json_response(result)
        if request.path.startswith(EXTERNAL_PREFIX):
            result = await self.external.route(request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith(MEDIA_PREFIX):
            result = await self.media.route(request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith(BOT_ADAPTERS_PREFIX):
            result = await self.platform.bot_adapters.route(self, request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith(BOT_OBSERVATION_PREFIX):
            result = await self.platform.bot_observation.route(self, request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith(QQ_ADMIN_PREFIX):
            result = await self.qq_admin.route(self, request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith(BOTS_PREFIX):
            result = await self.platform.bots.web_route(self, request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith("/api/web/relationships/"):
            result = await self.platform.relationships.route(
                self, request.path[len("/api/web/relationships/") :], body, session
            )
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith("/api/web/roles/"):
            result = await self.role_runtime.route(self, request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
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
            fingerprint, _ = await self.platform.local_work.run(self.authority)
            require(self.session_live(session), "session_expired", 401)
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            return web.json_response(result)
        if request.path.startswith(HOME_PREFIX):
            await self.platform.local_work.run(self.external.sync)
            external_revision = self.external.revision
            result = await self.home.route(request.path, body, session)
            await self.platform.local_work.run(self.external.sync)
            # The same re-check: a session revoked while HA was being asked gets no reading.
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = await self.platform.local_work.run(self.authority)
            require(self.session_live(session), "session_expired", 401)
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            require(self.external.revision == external_revision, "external_revision_changed", 409)
            return web.json_response(result)
        if request.path.startswith(TASKS_PREFIX):
            result = await self.platform.local_work.run(
                self.tasks.route, request.path, body, session
            )
            # A read-only projection still belongs to the session that asked for it.
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = await self.platform.local_work.run(self.authority)
            require(self.session_live(session), "session_expired", 401)
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            return web.json_response(result)
        if request.path.startswith(ASSETS_PREFIX):
            await self.platform.local_work.run(self.external.sync)
            external_revision = self.external.revision
            # The asset page reads through the registered asset identity; the browser session is
            # only the local operator asking, and it is re-checked once the peer has answered.
            result = await self.assets.route(request.path, body, session)
            await self.platform.local_work.run(self.external.sync)
            _, current = self.session(request)
            require(current is session, "session_expired", 401)
            fingerprint, _ = await self.platform.local_work.run(self.authority)
            require(self.session_live(session), "session_expired", 401)
            require(session["fingerprint"] == fingerprint, "session_expired", 401)
            require(self.external.revision == external_revision, "external_revision_changed", 409)
            return web.json_response(result)
        if request.path.startswith(PERSONAS_PREFIX):
            if request.path[len(PERSONAS_PREFIX) :] in self.persona_author.routes:
                result = await self.persona_author.route(request.path, body, session)
                require(
                    await self.platform.local_work.run(self.session_valid, session),
                    "session_expired",
                    401,
                )
                return web.json_response(result)
            # The persona page reads through its own registered character-service credential and
            # proves this session, this action and this subject allowlist at the start of every
            # outbound step and again before answering. This console makes the same proof once more
            # on its own account, immediately before the body leaves here.
            result = await self.personas.route(request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            require(self.persona_read_authorised(), "persona_read_required", 403)
            return web.json_response(result)
        if request.path.startswith("/api/web/weather/"):
            result = await self.weather.route(request.path, body, session)
            require(self.session_valid(session), "session_expired", 401)
            return web.json_response(result)
        if request.path == LIFE_PREFIX + "retry":
            result = await self.life_management.retry(body, session)
            return web.json_response(result)
        if request.path.startswith(LIFE_PREFIX + "image-backend/"):
            name = request.path[len(LIFE_PREFIX + "image-backend/") :]
            result = await self.life_management.image_backend(name, body, session)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if request.path.startswith("/api/web/skills/"):
            name = request.path[len("/api/web/skills/") :]
            result = await self.skills.route(name, body, session)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if request.path.startswith("/api/web/content/"):
            name = request.path[len("/api/web/content/") :]
            result = await self.content.route(name, body, session)
            if name == "original":
                data, headers = result
                return web.Response(body=data, headers=headers)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if request.path.startswith("/api/web/memory-links/"):
            result = await self.memory_links.route(
                request.path[len("/api/web/memory-links/") :], body, session
            )
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if request.path.startswith("/api/web/people-life/"):
            result = await self.people_life.route(
                request.path[len("/api/web/people-life/") :], body, session
            )
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if request.path.startswith(LIFE_PREFIX + "runtime/"):
            name = request.path[len(LIFE_PREFIX + "runtime/") :]
            require(name in {"read", "manage", "media", "content"}, "not_found", 404)
            result = await self.life_management.runtime(name, body, session)
            if name == "media":
                data, content_type, checksum = result
                return web.Response(
                    body=data,
                    content_type=content_type,
                    headers={"Cache-Control": "no-store", "X-Content-SHA256": checksum},
                )
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if request.path.startswith(KNOWLEDGE_PREFIX) or request.path.startswith(LIFE_PREFIX):
            reader = self.knowledge if request.path.startswith(KNOWLEDGE_PREFIX) else self.life
            result = await reader.route(request.path, body, session)
            if reader is self.life and request.path == LIFE_PREFIX + "state":
                result["can_retry"] = self.life_management.available()
            if reader is self.life and request.path == LIFE_PREFIX + "actors":
                roles = await self.platform.local_work.run(self.role_runtime.directory)
                names = {row["actor_id"]: row["name"] for row in roles}
                managed = {row["actor_id"]: row for row in roles}

                def project(page):
                    return [
                        {**item, "label": names[item["actor_id"]]}
                        if item["actor_id"] in names
                        else item
                        for item in page["items"]
                        if not is_placeholder_actor(item["actor_id"], managed.get(item["actor_id"]))
                    ]

                items = project(result)
                # Only one reserved bootstrap ID exists. If it occupied the whole
                # first page, advance once rather than hide the following real role.
                if result["items"] and not items and result["next_after_actor_id"] is not None:
                    result = await reader.route(
                        request.path,
                        {"limit": body["limit"], "after_actor_id": result["next_after_actor_id"]},
                        session,
                    )
                    items = project(result)
                result["items"] = items
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path.startswith(MEMORY_PREFIX):
            result = await self.memory.route(request.path, body, session)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        if request.path == "/api/web/connections/view":
            await self.platform.local_work.run(self.external.sync)
            result = connections_view(self, body)
            require(
                await self.platform.local_work.run(self.session_valid, session),
                "session_expired",
                401,
            )
            return web.json_response(result)
        raise Fault("not_found", 404)


if __name__ == "__main__":
    import getpass

    password = getpass.getpass("New local administrator password (12-256 characters): ")
    require(password == getpass.getpass("Confirm password: "), "invalid_input", 400)
    print(password_hash(password))
