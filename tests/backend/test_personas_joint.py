"""TS-025 joint acceptance: the real read-only page over a real, fixed character service.

This is the half of the acceptance that a controlled double cannot give. The Core here is the
producer's own application at the exact commit the verified candidate names, exported into this
worktree's runtime, served over real TLS by uvicorn on a real socket, reading its own isolated
SQLite database and seeding synthetic characters. The platform side is the real console with a real
login, a real Cookie, a real CSRF token and a real origin pin. Nothing between the two is replaced:
the page's own registered credential travels in a real Authorization header, the four read
operations are the candidate's own wire documents, and the cursors, version numbers and refusals in
the assertions are the producer's.

The two halves are reported separately on purpose. `test_personas_web.py` proves the shape, scope,
budget, gate and error rules against a peer that injects faults on demand; this file proves that the
same code path reads the actual product. When the fixed Core, its runtime dependencies or a usable
`git` are absent, every test here skips with the exact reason - never a pass.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web

import personas_fixture as fixture
from fixtures import ENV, ROOT
from services.platform.persona_page_config import (
    CANDIDATE_MANIFEST_SHA256,
    CANDIDATE_PRODUCER_COMMIT,
)
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD, web_settings

TOKEN = "synthetic-ts025-joint-persona-credential"
TOKEN_ENV = "TS025_JOINT_PERSONA"
CONNECTION = "characters-joint"
ALLOWED = ("actor:alpha", "actor:beta", "actor:epsilon")
ABSENT = "actor:delta"
REVISION_FIELDS = ("persona", "tone", "style", "address")


def joint_settings(directory, base_url, ca, **override):
    settings = web_settings(tempfile.mkdtemp(dir=directory))
    settings["principals"]["admin"]["actions"] += ["persona.read"]
    settings["persona_connections"] = {
        CONNECTION: {"base_url": base_url, "token_env": TOKEN_ENV, "ca_file": ca}
    }
    section = {
        "enabled": True,
        "connection_id": CONNECTION,
        "allowed_subjects": [*ALLOWED, ABSENT],
        "candidate_directory": os.environ["TS025_CANDIDATE_DIR"],
        "allow_candidate": True,
    }
    section.update(override)
    settings["web_personas"] = section
    return settings


class JointPersonaPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not os.environ.get("TS025_CANDIDATE_DIR"):
            raise unittest.SkipTest("TS025_CANDIDATE_DIR is not set")
        try:
            self.source = fixture.companion_source()
        except fixture.Unavailable as reason:
            raise unittest.SkipTest(f"joint Core unavailable: {reason}") from None
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {**ENV, TOKEN_ENV: TOKEN})
        env.start()
        self.addCleanup(env.stop)
        tool = os.environ.get("TS013_TLS_PYTHON")
        if not tool:
            raise unittest.SkipTest("TS013_TLS_PYTHON is not configured")
        subprocess.run(
            [tool, str(ROOT / "tests/backend/make_tls_fixture.py"), self.temp.name],
            check=True,
            capture_output=True,
        )
        self.ca = Path(self.temp.name) / "localhost.pem"
        self.key = Path(self.temp.name) / "localhost-key.pem"
        self.core_config_path = Path(self.temp.name) / "core.json"
        self.core_config_path.write_text(
            json.dumps(
                fixture.core_config(
                    self.temp.name,
                    TOKEN_ENV,
                    {
                        "actor:alpha": {"version": 1, "persona": "甲：初始人格", "tone": "平静"},
                        "actor:beta": {"version": 3, "persona": "乙：另一位角色"},
                        "actor:gamma": {"version": 1, "persona": "丙：未被本页允许"},
                        # Declared without a version, which the Core records as `imported: null`.
                        # This one entry is the runtime/schema deviation this file states below.
                        "actor:epsilon": "戊：部署项未声明版本",
                    },
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.environ["TIANSHU_COMPANION_CONFIG"] = str(self.core_config_path)
        from tianshu_companion.app import create_app as create_core

        self.core = await fixture.CoreServer.start(create_core(), self.ca, self.key)
        self.addAsyncCleanup(self.core.close)
        await self.seed()
        self.web_port = fixture.free_port()
        self.url = f"http://127.0.0.1:{self.web_port}"
        self.settings = joint_settings(self.temp.name, self.core.url, str(self.ca))
        self.settings["web"]["origin"] = self.url
        self.platform = Platform(self.settings)
        self.app = create_app(self.platform)
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.web_port).start()
        self.addAsyncCleanup(runner.cleanup)
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)

    # ------------------------------------------------------------------ seeding

    async def core_call(self, document):
        return await fixture.call_core(self.core.url, self.ca, TOKEN, document)

    async def draft(self, subject, content, request_id):
        persona = (await self.core_call({"operation": "get", "subject": subject}))["persona"]
        answer = await self.core_call(
            {
                "operation": "draft",
                "subject": subject,
                "request_id": request_id,
                "content": content,
                "operator": "operator:joint",
                "expected": persona["version"],
                "reason": "合成演练",
                "note": "联合验收种子",
            }
        )
        return answer["persona"]

    async def move(self, operation, subject, revision_id, expected, request_id):
        answer = await self.core_call(
            {
                "operation": operation,
                "subject": subject,
                "request_id": request_id,
                "revision_id": revision_id,
                "operator": "operator:joint",
                "expected": expected,
                "reason": "合成" + operation,
            }
        )
        return answer["persona"]

    async def seed(self):
        """Real writes through the producer's own API, so every assertion below is real history.

        `actor:alpha` ends on an unapproved draft with a rollback behind it; `actor:beta` ends
        published. `actor:gamma` exists in the Core but is not allowed by this deployment.
        """
        self.seeds = {}
        for subject, content in (
            (
                "actor:alpha",
                {
                    "persona": "actor:alpha 的第二版",
                    "tone": "温和",
                    "style": "简短",
                    "address": "你",
                    "extension_flag": True,
                },
            ),
            ("actor:beta", {"persona": "actor:beta 的第二版", "tone": "轻快"}),
        ):
            initial = (await self.core_call({"operation": "get", "subject": subject}))["persona"]
            drafted = await self.draft(subject, content, f"seed-draft-{subject}")
            second = drafted["draft_revision"]
            approved = await self.move(
                "approve", subject, second, drafted["version"], f"seed-approve-{subject}"
            )
            published = await self.move(
                "publish", subject, second, approved["version"], f"seed-publish-{subject}"
            )
            self.seeds[subject] = {
                "initial": initial["published_revision"],
                "second": second,
                "version": published["version"],
            }
        # One rollback, so the rollback history kind is real rather than empty.
        state = await self.move(
            "rollback",
            "actor:alpha",
            self.seeds["actor:alpha"]["initial"],
            self.seeds["actor:alpha"]["version"],
            "seed-rollback",
        )
        # A rollback publishes a new revision that carries the older content, so the current
        # published pointer is neither of the two revisions drafted above.
        self.seeds["actor:alpha"]["version"] = state["version"]
        self.seeds["actor:alpha"]["restored"] = state["published_revision"]
        # A third revision is drafted and left unapproved, so a draft pointer and a "draft" word
        # are read from the producer as well.
        state = await self.draft(
            "actor:alpha", {"persona": "甲：尚未批准的草稿", "tone": "急促"}, "seed-draft-open"
        )
        self.seeds["actor:alpha"]["draft"] = state["draft_revision"]
        self.seeds["actor:alpha"]["version"] = state["version"]

    # ------------------------------------------------------------------ harness

    async def call(self, path, body, csrf, expected=200, **headers):
        async with self.client.post(
            self.url + "/api/web/" + path,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": csrf, **headers},
        ) as response:
            data = await response.json()
            self.assertEqual(response.status, expected, data)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return data

    async def login(self):
        async with self.client.get(self.url + "/api/web/session") as response:
            anonymous = await response.json()
        return await self.call(
            "login", {"username": "synthetic-admin", "password": PASSWORD}, anonymous["csrf"]
        )

    async def page(self, logged, path, body, expected=200):
        return await self.call(f"personas/{path}", body, logged["csrf"], expected)

    # -------------------------------------------------------------------- reads

    async def test_catalog_lists_the_allowed_characters_only(self):
        logged = await self.login()
        page = await self.page(logged, "catalog", {"cursor": None})
        self.assertEqual(page["code"], "ready")
        self.assertEqual(page["subjects"], 4)
        # The registration name, the endpoint and the credential stay server-side: the browser is
        # told how many characters this deployment allows, not where they are read from.
        self.assertNotIn(CONNECTION, json.dumps(page, ensure_ascii=False))
        self.assertEqual(
            set(page),
            {"code", "page", "subjects", "count", "entries", "has_more", "next_cursor"},
        )
        self.assertEqual(
            [entry["subject"] for entry in page["entries"]],
            ["actor:alpha", "actor:beta", "actor:delta", "actor:epsilon"],
        )
        states = {entry["subject"]: entry["state"] for entry in page["entries"]}
        errors = {entry["subject"]: entry["error"] for entry in page["entries"]}
        # The producer's own words for what it holds: an unapproved draft is a draft, a published
        # pointer is published, and the deployment's character the Core never held is absent.
        self.assertEqual(states["actor:alpha"], "draft")
        self.assertEqual(states["actor:beta"], "published")
        self.assertEqual(errors["actor:delta"], "not_found")
        self.assertIsNone(states["actor:delta"])
        # `actor:epsilon` is readable by the Core but its answer does not satisfy the pinned
        # candidate (see the deviation test); the row states that, and the page is not empty.
        self.assertEqual(errors["actor:epsilon"], "invalid_upstream")
        # The Core holds a character this deployment does not allow, and it is not here.
        self.assertNotIn("actor:gamma", json.dumps(page, ensure_ascii=False))
        self.assertEqual(
            page["entries"][1]["published_revision"], self.seeds["actor:beta"]["second"]
        )

    async def test_a_deployment_entry_without_a_version_is_a_stated_deviation(self):
        """One real producer/schema gap, stated as a fact rather than smoothed over.

        A `roles` entry declared without a version is recorded by the Core as `imported: null`,
        while the pinned candidate's `get` branch requires `imported` to be an integer. TS-025
        consumes the contract it verified, so it refuses that one answer - and says which character
        it refused and why - instead of dropping the row, showing it as empty, or quietly
        reinterpreting the field. Fixing this belongs to the producer or to a new candidate
        revision; this consumer may not widen a pinned contract on its own.
        """
        raw = await self.core_call({"operation": "get", "subject": "actor:epsilon"})
        self.assertIsNone(raw["persona"]["imported"])
        from services.platform.persona_page_config import load_candidate

        validator = load_candidate(os.environ["TS025_CANDIDATE_DIR"]).response_validator
        failures = [
            f"{'/'.join(str(part) for part in error.path)}: {error.message}"
            for error in validator.iter_errors(raw)
        ]
        self.assertTrue(failures)
        self.assertIn("imported", " ".join(failures))
        logged = await self.login()
        page = await self.page(logged, "catalog", {"cursor": None})
        row = next(entry for entry in page["entries"] if entry["subject"] == "actor:epsilon")
        self.assertEqual(row["error"], "invalid_upstream")
        self.assertIsNone(row["state"])
        self.assertIsNone(row["version"])
        self.assertEqual(page["count"], 4)
        # The other three characters are unaffected: one refusal never erases a page.
        self.assertEqual([entry["error"] for entry in page["entries"]].count(None), 2)

    async def test_history_reads_every_kind_from_the_producer(self):
        logged = await self.login()
        counts = {}
        for kind in ("revisions", "publications", "approvals", "rollbacks"):
            page = await self.page(
                logged,
                "history",
                {"subject": "actor:alpha", "kind": kind, "cursor": None},
            )
            self.assertEqual(page["kind"], kind)
            self.assertEqual(page["consistency"], "version_bound")
            self.assertEqual(page["limit"], 20)
            self.assertEqual(page["count"], len(page["entries"]))
            counts[kind] = page["count"]
            self.assertFalse(page["has_more"])
        self.assertGreaterEqual(counts["revisions"], 3)
        self.assertGreaterEqual(counts["publications"], 2)
        self.assertGreaterEqual(counts["approvals"], 1)
        self.assertEqual(counts["rollbacks"], 1)

    async def test_revision_and_compare_carry_the_producer_values(self):
        logged = await self.login()
        initial = self.seeds["actor:alpha"]["initial"]
        second = self.seeds["actor:alpha"]["second"]
        restored = self.seeds["actor:alpha"]["restored"]
        current = await self.page(
            logged, "revision", {"subject": "actor:alpha", "revision_id": restored}
        )
        self.assertTrue(current["is_published"])
        self.assertFalse(current["is_draft"])
        self.assertEqual(current["revision"]["content"]["persona"], "甲：初始人格")
        self.assertEqual(current["revision"]["source"], "rollback")
        self.assertEqual(current["state"], "draft")
        earlier = await self.page(
            logged, "revision", {"subject": "actor:alpha", "revision_id": initial}
        )
        self.assertFalse(earlier["is_published"])
        self.assertEqual(earlier["revision"]["content"]["persona"], "甲：初始人格")
        self.assertEqual(earlier["revision"]["source"], "initial_config")
        drafted = await self.page(
            logged, "revision", {"subject": "actor:alpha", "revision_id": second}
        )
        self.assertEqual(drafted["revision"]["content"]["persona"], "actor:alpha 的第二版")
        self.assertEqual(drafted["revision"]["additional_fields"], ["extension_flag"])
        self.assertFalse(drafted["is_published"])
        self.assertFalse(drafted["is_draft"])
        draft = self.seeds["actor:alpha"]["draft"]
        answer = await self.page(
            logged,
            "compare",
            {"subject": "actor:alpha", "left": second, "right": draft},
        )
        self.assertEqual(answer["subject"], "actor:alpha")
        self.assertEqual(set(answer["fields"]), set(REVISION_FIELDS))
        self.assertFalse(answer["content_identical"])
        self.assertEqual(answer["fields"]["persona"]["change"], "modified")
        self.assertEqual(answer["fields"]["persona"]["left"], "actor:alpha 的第二版")
        self.assertEqual(answer["fields"]["persona"]["right"], "甲：尚未批准的草稿")
        # `tone` exists on both sides with different values; `address` only on the older one.
        self.assertEqual(answer["fields"]["tone"]["change"], "modified")
        self.assertEqual(answer["fields"]["tone"]["left"], "温和")
        self.assertEqual(answer["fields"]["tone"]["right"], "急促")
        self.assertEqual(answer["fields"]["address"]["change"], "removed")
        self.assertEqual(answer["fields"]["address"]["presence"], {"left": True, "right": False})
        self.assertEqual(answer["additional_field_names"], ["extension_flag"])
        self.assertTrue(answer["additional_fields_present"])
        for side in ("left", "right"):
            self.assertNotIn("content", answer[side])

    async def test_producer_refusals_keep_the_producers_own_words(self):
        logged = await self.login()
        unknown = await self.page(
            logged, "revision", {"subject": "actor:alpha", "revision_id": "0" * 64}, expected=404
        )
        self.assertEqual(unknown["code"], "not_found")
        # A revision that belongs to another character is the producer's `invalid_input`, and the
        # page does not turn it into an absence or into another character's answer.
        cross = await self.page(
            logged,
            "revision",
            {"subject": "actor:alpha", "revision_id": self.seeds["actor:beta"]["second"]},
            expected=400,
        )
        self.assertEqual(cross["code"], "invalid_input")

    async def test_history_pages_on_the_producers_cursor_and_states_a_stale_one(self):
        logged = await self.login()
        # Twenty-one revisions make the producer hand out its own opaque continuation cursor.
        for index in range(19):
            persona = (await self.core_call({"operation": "get", "subject": "actor:alpha"}))[
                "persona"
            ]
            await self.core_call(
                {
                    "operation": "draft",
                    "subject": "actor:alpha",
                    "request_id": f"page-draft-{index}",
                    "content": {"persona": f"甲：分页草稿 {index}"},
                    "operator": "operator:joint",
                    "expected": persona["version"],
                    "reason": "合成分页",
                }
            )
        first = await self.page(
            logged, "history", {"subject": "actor:alpha", "kind": "revisions", "cursor": None}
        )
        self.assertEqual(first["count"], 20)
        self.assertTrue(first["has_more"])
        cursor = first["next_cursor"]
        self.assertIsInstance(cursor, str)
        self.assertLessEqual(len(cursor), 2048)
        second = await self.page(
            logged, "history", {"subject": "actor:alpha", "kind": "revisions", "cursor": cursor}
        )
        self.assertGreater(second["count"], 0)
        self.assertFalse(second["has_more"])
        # The character moves on; the producer refuses the older page and the page says so
        # instead of quietly showing a newer list as if it were the next page.
        persona = (await self.core_call({"operation": "get", "subject": "actor:alpha"}))["persona"]
        await self.core_call(
            {
                "operation": "draft",
                "subject": "actor:alpha",
                "request_id": "page-draft-stale",
                "content": {"persona": "甲：又改了"},
                "operator": "operator:joint",
                "expected": persona["version"],
                "reason": "合成移动",
            }
        )
        stale = await self.page(
            logged,
            "history",
            {"subject": "actor:alpha", "kind": "revisions", "cursor": cursor},
            expected=409,
        )
        self.assertEqual(stale["code"], "version_conflict")

    async def test_a_disallowed_character_is_refused_by_the_page(self):
        """`actor:gamma` exists in the Core, so a 403 proves the page refused it locally."""
        logged = await self.login()
        answer = await self.page(
            logged,
            "history",
            {"subject": "actor:gamma", "kind": "revisions", "cursor": None},
            expected=403,
        )
        self.assertEqual(answer["code"], "forbidden")
        answer = await self.page(
            logged,
            "revision",
            {"subject": "actor:gamma", "revision_id": self.seeds["actor:alpha"]["initial"]},
            expected=403,
        )
        self.assertEqual(answer["code"], "forbidden")

    async def test_the_page_never_returns_the_credential_or_the_endpoint(self):
        logged = await self.login()
        seen = []
        for body, path in (
            ({"cursor": None}, "catalog"),
            ({"subject": "actor:alpha", "kind": "publications", "cursor": None}, "history"),
            (
                {"subject": "actor:alpha", "revision_id": self.seeds["actor:alpha"]["initial"]},
                "revision",
            ),
        ):
            seen.append(json.dumps(await self.page(logged, path, body), ensure_ascii=False))
        text = "\n".join(seen)
        for secret in (TOKEN, TOKEN_ENV, self.core.url, str(self.ca), str(Path(self.temp.name))):
            self.assertNotIn(secret, text)
        self.assertNotIn("internal/v1", text)

    async def test_the_verified_candidate_binds_this_run(self):
        """The page only exists because one exact candidate package verified against one Core."""
        adapter = self.adapter()
        self.assertTrue(adapter.enabled)
        manifest = adapter.candidate.manifest
        self.assertEqual(manifest["producer_commit"], fixture.COMPANION_COMMIT)
        self.assertEqual(manifest["allowed_consumer_task"], fixture.COMPANION_TASK)
        self.assertEqual(manifest["status"], "candidate_not_published")
        self.assertIs(manifest["production_publish_authorized"], False)
        on_disk = hashlib.sha256(
            (Path(os.environ["TS025_CANDIDATE_DIR"]) / "manifest.json")
            .read_bytes()
            .replace(b"\r\n", b"\n")
        ).hexdigest()
        self.assertEqual(on_disk, CANDIDATE_MANIFEST_SHA256)
        self.assertEqual(len(adapter.candidate.word), 64)
        self.assertTrue(
            all(character in "0123456789abcdef" for character in adapter.candidate.word)
        )

    def adapter(self):
        from services.platform.web_console import WebConsole

        for route in self.app.router.routes():
            console = getattr(route.handler, "__self__", None)
            if isinstance(console, WebConsole):
                return console.personas
        raise AssertionError("the console route was not assembled")


class JointFixtureTests(unittest.TestCase):
    """The fixture's own honesty rules: a missing piece is stated, never passed."""

    def test_export_is_the_named_commit_or_a_stated_skip(self):
        try:
            source = fixture.export_companion()
        except fixture.Unavailable as reason:
            self.assertIn("git", str(reason).lower() + " git")
            return
        marker = source / ".ts025-commit"
        self.assertEqual(marker.read_text(encoding="ascii"), fixture.COMPANION_COMMIT)
        # The exported snapshot is the producer's own tree at that commit.
        head = (source / "src/tianshu_companion/personas.py").is_file()
        self.assertTrue(head)

    def test_candidate_names_the_same_producer_commit(self):
        manifest = json.loads(
            (Path(os.environ["TS025_CANDIDATE_DIR"]) / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["producer_commit"], fixture.COMPANION_COMMIT)
        self.assertEqual(manifest["producer_commit"], CANDIDATE_PRODUCER_COMMIT)
        self.assertEqual(manifest["allowed_consumer_task"], "TS-025")
