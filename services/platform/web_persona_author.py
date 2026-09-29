"""Persona authoring adapter: browser scope and four-field forms, no persona facts.

The Companion Personas use case owns revisions and the atomic approve/publish step. This
adapter only narrows a live console session to registered role targets and sends one
authenticated HTTPS operation per click. Neither content nor service credentials are kept
in a platform table.
"""

import asyncio
import re

import aiohttp

from .contracts import Fault, loads, require
from .persona_client import KNOWN_CODES, request_id


PROFILE_ID = re.compile(r"persona-profile:[0-9a-f]{32}\Z")
CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")
FIELDS = {"persona": 20000, "tone": 20000, "style": 20000, "address": 4000}


def _text(value, limit, required=False):
    require(isinstance(value, str) and len(value) <= limit, "invalid_input", 400)
    require(all(ord(char) >= 32 for char in value), "invalid_input", 400)
    require(not required or bool(value.strip()), "invalid_input", 400)
    return value


def _content(value):
    require(isinstance(value, dict) and set(value) == set(FIELDS), "invalid_input", 400)
    for key, limit in FIELDS.items():
        field = value[key]
        require(isinstance(field, str) and len(field) <= limit, "invalid_input", 400)
        require(all(ord(char) >= 32 or char in "\n\r\t" for char in field), "invalid_input", 400)
        require(key != "persona" or bool(field.strip()), "invalid_input", 400)
    return value


def _version(value):
    require(type(value) is int and value > 0, "invalid_input", 400)
    return value


class WebPersonaAuthor:
    routes = {"profiles", "view", "create", "save", "apply"}

    def __init__(self, console, personas):
        self.console = console
        self.personas = personas
        self.rule = personas.rule
        self.client = personas.client

    def _guard(self, session, action, target=None):
        require(self.console.session_valid(session), "session_expired", 401)
        require(self.personas.enabled, "personas_disabled", 503)
        require(self.rule.get("authoring_enabled", False), "personas_disabled", 503)
        require(self.console.persona_read_authorised(), "persona_read_required", 403)
        if action:
            require(self.console.persona_action_authorised(action), "persona_write_required", 403)
            if action == "persona.apply":
                require(
                    self.console.persona_action_authorised("persona.edit"),
                    "persona_write_required",
                    403,
                )
        if target is not None:
            require(target in self.rule["apply_subjects"], "forbidden", 403)

    async def _call(self, document, session, action, target=None):
        self._guard(session, action, target)
        require(self.client is not None, "dependency_unavailable", 503)
        client = self.client
        try:
            async with asyncio.timeout(10):
                async with client.slots:
                    self._guard(session, action, target)
                    token = client.credential()
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=client.timeout), trust_env=False
                    ) as http:
                        async with http.post(
                            client.base_url + "/internal/v1/persona/manage",
                            json=document,
                            headers={"Authorization": "Bearer " + token},
                            ssl=client._tls(),
                            allow_redirects=False,
                        ) as response:
                            require(
                                response.content_type == "application/json", "invalid_upstream", 502
                            )
                            raw = await response.content.read(1024 * 1024 + 1)
                            require(len(raw) <= 1024 * 1024, "invalid_upstream", 502)
                            try:
                                answer = loads(raw)
                            except (ValueError, UnicodeError, TypeError):
                                raise Fault("invalid_upstream", 502) from None
                            require(isinstance(answer, dict), "invalid_upstream", 502)
                            if response.status != 200:
                                code = answer.get("code")
                                require(
                                    answer.get("request_id") == document["request_id"]
                                    and KNOWN_CODES.get(code) == response.status,
                                    "invalid_upstream",
                                    502,
                                )
                                raise Fault(code, response.status)
            self._guard(session, action, target)
            require(
                answer.get("operation") == document["operation"]
                and answer.get("schema_version") == 1,
                "invalid_upstream",
                502,
            )
            return answer
        except TimeoutError:
            raise Fault("timeout", 503) from None
        except (aiohttp.ClientError, OSError):
            raise Fault("dependency_unavailable", 503) from None

    def _item(self, answer, wanted=None):
        item = answer.get("item")
        require(isinstance(item, dict), "invalid_upstream", 502)
        identifier = item.get("id")
        require(isinstance(identifier, str), "invalid_upstream", 502)
        require(item.get("kind") in {"profile", "role"}, "invalid_upstream", 502)
        if wanted is not None:
            require(identifier == wanted, "invalid_upstream", 502)
        require(type(item.get("version")) is int and item["version"] > 0, "invalid_upstream", 502)
        result = {
            key: item.get(key)
            for key in (
                "id",
                "kind",
                "name",
                "description",
                "version",
                "state",
                "published_revision",
                "draft_revision",
                "updated_at",
                "last_applied_target",
                "last_applied_profile_revision",
                "last_applied_target_revision",
            )
        }
        if "content" in item:
            content = item["content"]
            require(isinstance(content, dict), "invalid_upstream", 502)
            result["content"] = {field: content.get(field, "") for field in FIELDS}
            result["additional_fields"] = sorted(set(content) - set(FIELDS))
        return result

    def _profile_status(self, item, role_map=None):
        if item["kind"] != "profile":
            return item
        last_target = item.pop("last_applied_target")
        target_revision = item.pop("last_applied_target_revision")
        visible_target = last_target if last_target in self.rule["apply_subjects"] else None
        if last_target is None:
            state = "never"
        elif visible_target is None or role_map is None or visible_target not in role_map:
            state = "unknown"
        else:
            state = (
                "current"
                if target_revision is not None
                and role_map[visible_target]["published_revision"] == target_revision
                else "previous"
            )
        item["applied_state"] = state
        item["applied_target"] = visible_target
        return item

    async def route(self, path, body, session):
        name = path.rsplit("/", 1)[-1]
        require(name in self.routes and isinstance(body, dict), "not_found", 404)
        action = {"create": "persona.create", "save": "persona.edit", "apply": "persona.apply"}.get(
            name
        )
        target = None
        if name == "profiles":
            require(body == {}, "invalid_input", 400)
            answer = await self._call(
                {"operation": "author_catalog", "request_id": request_id()}, session, None
            )
            profiles = answer.get("profiles")
            require(isinstance(profiles, list) and len(profiles) <= 256, "invalid_upstream", 502)
            roles = answer.get("roles")
            require(isinstance(roles, list) and len(roles) <= 256, "invalid_upstream", 502)
            role_map = {}
            for row in roles:
                item = self._item({"item": row})
                require(
                    item["kind"] == "role" and item["id"] not in role_map, "invalid_upstream", 502
                )
                role_map[item["id"]] = item
            require(set(self.rule["apply_subjects"]) <= set(role_map), "invalid_upstream", 502)
            return {
                "profiles": [
                    self._profile_status(self._item({"item": item}), role_map) for item in profiles
                ],
                "targets": [role_map[subject] for subject in self.rule["apply_subjects"]],
                "permissions": {
                    action: self.console.persona_action_authorised("persona." + action)
                    for action in ("create", "edit", "apply")
                },
            }
        if name == "view":
            require(set(body) == {"id"}, "invalid_input", 400)
            identifier = _text(body["id"], 128, True)
            if not PROFILE_ID.fullmatch(identifier):
                target = identifier
            self._guard(session, None, target)
            answer = await self._call(
                {"operation": "author_view", "subject": identifier, "request_id": request_id()},
                session,
                None,
                target,
            )
            return {"item": self._profile_status(self._item(answer, identifier))}
        if name == "create":
            require(
                set(body)
                in (
                    {"name", "description", "content", "client_id"},
                    {"name", "description", "content", "client_id", "copy_from", "copy_expected"},
                ),
                "invalid_input",
                400,
            )
        elif name == "save":
            require(
                set(body) == {"id", "name", "description", "content", "expected", "client_id"},
                "invalid_input",
                400,
            )
        else:
            require(
                set(body)
                == {
                    "id",
                    "name",
                    "description",
                    "content",
                    "expected",
                    "target",
                    "target_expected",
                    "client_id",
                },
                "invalid_input",
                400,
            )
        require(
            isinstance(body["client_id"], str) and CLIENT_ID.fullmatch(body["client_id"]),
            "invalid_input",
            400,
        )
        name_text = _text(body["name"], 80, True)
        description = _text(body["description"], 400)
        content = _content(body["content"])
        identifier = body.get("id")
        if identifier is not None:
            identifier = _text(identifier, 128, True)
        if name == "apply":
            target = _text(body["target"], 128, True)
            _version(body["target_expected"])
            self._guard(session, "persona.edit", target)
        elif identifier is not None and not PROFILE_ID.fullmatch(identifier):
            target = identifier
        if name != "create":
            _version(body["expected"])
        profile = identifier is not None and PROFILE_ID.fullmatch(identifier)
        operation = (
            "create_profile"
            if name == "create"
            else "save_profile"
            if name == "save" and profile
            else "save_role"
            if name == "save"
            else "apply_profile"
            if profile
            else "apply_role"
        )
        document = {
            "operation": operation,
            "request_id": body["client_id"],
            "operator": self.console.config["principal"],
            "reason": "Web console save and apply" if name == "apply" else "Web console save draft",
            "name": name_text,
            "description": description,
            "content": content,
        }
        if name == "create" and "copy_from" in body:
            source = _text(body["copy_from"], 128, True)
            _version(body["copy_expected"])
            if not PROFILE_ID.fullmatch(source):
                self._guard(session, None, source)
                target = source
            document.update(source=source, source_expected=body["copy_expected"])
        if identifier is not None:
            document.update(subject=identifier, expected=body["expected"])
        if name == "apply" and profile:
            document.update(target=target, target_expected=body["target_expected"])
        elif name == "apply":
            require(target == identifier, "forbidden", 403)
        answer = await self._call(document, session, action, target)
        if name == "apply" and profile:
            result = answer.get("item")
            require(isinstance(result, dict), "invalid_upstream", 502)
            return {
                "item": self._profile_status(
                    self._item({"item": result.get("profile")}, identifier),
                    {target: self._item({"item": result.get("target")}, target)},
                ),
                "target": self._item({"item": result.get("target")}, target),
            }
        return {"item": self._profile_status(self._item(answer, identifier))}
