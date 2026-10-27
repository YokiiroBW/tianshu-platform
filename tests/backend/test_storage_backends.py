"""Real optional backends and immutable payload recovery, using synthetic data only."""

import asyncio
import base64
import contextlib
import io
import sys
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
import uuid
from unittest.mock import patch

from fixtures import ENV, start_http
from services.platform.server import create_app
from web_fixtures import home_settings
from services.platform import backends
from services.platform.bot_adapter_catalog import BotAdapterCatalog
from services.platform.external_catalog import ExternalCatalog
from services.platform.infrastructure.repository import Repository as Observations
from services.platform.infrastructure.control_repository import ControlRepository
from services.platform.media.repository import Repository
from services.platform.payload_files import DeliveryPayloads
from services.platform.provider_catalog import ProviderCatalog
from services.platform.service import Platform, validate_settings
from services.platform.contracts import Fault
from services.platform import runtime_health
from services.platform.storage import Store
from services.platform.web_account import WebAccount, password_hash
from services.platform.web_console import WebConsole
from services.platform.media.music.metadata_cache import MetadataCache

import test_platform as core_tests
import test_c4_delivery as delivery_tests
import test_books as book_tests
import test_book_audio as audio_tests
import test_comics as comic_tests
import test_music_request_control as music_budget_tests
import test_music_lyrics as lyric_tests
import test_home as home_tests
import test_web_models as model_tests
import test_infrastructure_control as infrastructure_tests
import test_bot_observation as observation_tests
import test_role_runtime as role_tests
from services.platform.role_runtime import RoleRuntime
from services.platform.bot_observation import BotObservation
from tianshu_storage import StorageOptions, StorageRuntime, StorageError

PG = os.environ.get("TS231_TEST_POSTGRES")
REDIS = os.environ.get("TS231_TEST_REDIS")


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "media.sqlite"

    def test_sqlite_chapter_audio_and_lyrics_are_files_with_identical_reads(self):
        repo = Repository(self.path)
        values = {
            "book_chapter": {"content": "第一个不可变正文\n第二行"},
            "book_audio_job": {"text": "听书准备时的旧正文", "state": "unknown", "lease": 123},
            "music_lyrics": {
                "pending": {"writes": [{"content": "[00:01]歌词", "sha256": "unchanged"}]}
            },
        }
        for kind, value in values.items():
            saved = repo.put(kind, kind, value)
            self.assertEqual(repo.get(kind, kind), saved)
            with repo.transaction() as db:
                raw = json.loads(
                    db.execute("SELECT body FROM documents WHERE kind=?", (kind,)).fetchone()[0]
                )
            self.assertNotIn("content", raw)
            self.assertNotIn("text", raw)
            if kind == "music_lyrics":
                self.assertNotIn("content", raw["pending"]["writes"][0])
        reopened = Repository(self.path)
        self.assertEqual(
            reopened.get("book_audio_job", "book_audio_job")["text"],
            values["book_audio_job"]["text"],
        )

    def test_existing_inline_sqlite_keeps_revision_and_unknown_state(self):
        repo = Repository(self.path)
        prior = {"text": "旧输入", "state": "unknown", "revision": 17, "lease": 567}
        with repo.transaction() as db:
            db.execute(
                "INSERT INTO documents VALUES(?,?,?,?)",
                ("book_audio_job", "old", 17, json.dumps(prior)),
            )
        reopened = Repository(self.path)
        self.assertEqual(reopened.get("book_audio_job", "old"), prior)
        with reopened.transaction() as db:
            raw = json.loads(db.execute("SELECT body FROM documents WHERE id='old'").fetchone()[0])
            self.assertNotIn("text", raw)
            self.assertEqual(raw["revision"], 17)
            reference = raw["text_ref"]
        (reopened.payloads.books.root / reference["relative_path"]).write_bytes(b"tampered")
        with self.assertRaises(sqlite3.Error):
            reopened.get("book_audio_job", "old")

    def test_delivery_binary_is_sealed_and_wire_is_exact(self):
        files = DeliveryPayloads(Path(self.folder.name) / "bots.sqlite")
        content = b"synthetic-image-bytes"
        wire = {
            "text": "hello",
            "media": [
                {
                    "media_type": "image/png",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "data": base64.b64encode(content).decode(),
                }
            ],
        }
        stored = files.store(wire)
        self.assertNotIn("data", stored["media"][0])
        self.assertEqual(files.read(stored), wire)
        self.assertEqual(files.store(stored), stored)

    def test_noncanonical_but_valid_base64_keeps_the_exact_wire_spelling(self):
        files = DeliveryPayloads(self.path)
        for data in ("YR==", "YWJ="):
            with self.subTest(data=data):
                wire = {"media": [{"media_type": "image/png", "data": data}]}
                stored = files.store(wire)
                self.assertNotIn("data", stored["media"][0])
                self.assertIn("encoded_ref", stored["media"][0])
                self.assertEqual(files.read(stored), wire)


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class MigrationTests(unittest.TestCase):
    def test_all_owned_schemas_import_seals_ciphertexts_and_history_then_restart(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {**ENV, "TS017_HA_TOKEN": "synthetic-storage-home"}),
        ):
            root = Path(folder)
            settings = home_settings(folder, "http://127.0.0.1:41001")
            settings["web_account"] = {"mode": "claim"}
            platform = Platform(settings)
            console = WebConsole(platform)
            account = console.account.create(
                "storage-owner", password_hash("synthetic-storage-password")
            )
            media = platform.media.repo
            media.put("book_chapter", "chapter", {"content": "不能丢失的下载正文"})
            media.put("book_audio_job", "audio", {"text": "必须固定的听书输入", "state": "unknown"})
            media.put(
                "music_lyrics", "lyrics", {"pending": {"writes": [{"content": "[00:01]恢复歌词"}]}}
            )
            historical = Path(str(platform.store.path) + ".home-controls.external.7.sqlite")
            with (
                closing(sqlite3.connect(console.home.ledger_path)) as source,
                closing(sqlite3.connect(historical)) as target,
            ):
                source.backup(target)
            provider_dir, external_dir, adapter_dir = (
                root / "providers",
                root / "external",
                root / "adapters",
            )
            providers = ProviderCatalog(provider_dir, create=True)
            provider = providers.save(
                client_id="provider:create",
                name="Synthetic",
                base_url="https://example.invalid/v1",
                model_id="synthetic",
                api_key="synthetic-secret",
            )
            external = ExternalCatalog(external_dir, create=True)
            external.save(
                "weather",
                {"host": "example.invalid", "locations": {}},
                {"action": "replace", "value": "synthetic-weather"},
                {"action": "clear"},
                0,
                str(uuid.uuid4()),
            )
            adapters = BotAdapterCatalog(adapter_dir)
            adapters.put({"id": "adapter:one", "revision": 3, "token": "synthetic-private"})
            infra_path = str(platform.store.path) + ".infrastructure.sqlite"
            observations = Observations(infra_path)
            observations.record(
                "node", "docker.inventory", 3, {"status": "ok", "data": {"containers": []}}
            )
            controls = ControlRepository(infra_path)
            document = {
                "operation_id": "op:one",
                "client_id": "op:create",
                "node_id": "node",
                "connection_revision": 3,
                "kind": "action",
                "stack_id": None,
                "container_id": "container",
                "state": "queued",
            }
            controls.accept(document, {"action": "stop"}, "container", 1000)
            controls.claim("old-owner", 1000, 10)
            controls.sending("op:one", "old-owner", 1001)
            platform.close()
            before = {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*.key")
            }
            settings["storage_backends"] = {
                "postgresql_url": PG,
                "namespace": "ts231_" + uuid.uuid4().hex,
            }
            for iteration in range(2):
                runtime = backends.start(settings)
                self.addCleanup(runtime.close)
                store = Store(settings["database_path"], runtime=runtime)
                migrated = Repository(str(store.path) + ".media.sqlite", runtime=runtime)
                provider_owner = ProviderCatalog(provider_dir, runtime=runtime)
                external_owner = ExternalCatalog(external_dir, runtime=runtime)
                adapter_owner = BotAdapterCatalog(adapter_dir, runtime=runtime)
                observation_owner = Observations(infra_path, runtime=runtime)
                control_owner = ControlRepository(infra_path, runtime=runtime)
                suffixes = {
                    "web_inputs": ".web-inputs.sqlite",
                    "web_replies": ".web-replies.sqlite",
                    "web_models": ".web-models.sqlite",
                    "home": ".home-controls.sqlite",
                    "home_7": ".home-controls.external.7.sqlite",
                    "bots": ".bots.sqlite",
                    "qq_admin": ".qq-admin.sqlite",
                    "web_account": ".web-account.sqlite",
                }
                for name, suffix in suffixes.items():
                    backends.database(runtime, name, str(store.path) + suffix)
                runtime.finalize()
                self.assertEqual(WebAccount(settings, runtime=runtime).read(), account)
                self.assertEqual(
                    provider_owner.view()["providers"][0]["provider_id"], provider["provider_id"]
                )
                self.assertEqual(adapter_owner.get("adapter:one")["token"], "synthetic-private")
                self.assertEqual(
                    external_owner.snapshot()[1]["weather"]["credential"], "synthetic-weather"
                )
                self.assertEqual(
                    observation_owner.read("node", "docker.inventory", 3)["sample"]["status"], "ok"
                )
                recovered = control_owner.claim("new-owner", 2000 + iteration * 1000, 10)
                self.assertEqual(recovered["sent"], 1)
                self.assertEqual(recovered["document"]["stage"], "reconciling")
                self.assertEqual(
                    migrated.get("book_chapter", "chapter")["content"], "不能丢失的下载正文"
                )
                self.assertEqual(migrated.get("book_audio_job", "audio")["state"], "unknown")
                with migrated.transaction() as db:
                    raw = json.loads(
                        db.execute("SELECT body FROM documents WHERE id='chapter'").fetchone()[0]
                    )
                    self.assertIn("content_ref", raw)
                    self.assertNotIn("content", raw)
                self.assertEqual(migrated.dashboard_details()["database"]["engine"], "PostgreSQL")
                self.assertEqual(
                    before,
                    {
                        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in root.rglob("*.key")
                    },
                )
                runtime.close()


class PostgreSQLMixin:
    def setUp(self):
        self.backend_runtimes = []
        environment = patch.dict(
            os.environ,
            {"TIANSHU_POSTGRES_URL": PG, "TIANSHU_STORAGE_NAMESPACE": "ts231_" + uuid.uuid4().hex},
        )
        environment.start()
        self.addCleanup(environment.stop)
        original = backends.start
        original_readonly, original_inspect = backends.readonly, backends.inspect
        installation_seed = os.environ["TIANSHU_STORAGE_NAMESPACE"]

        def scoped(operation, settings):
            anchor = str(Path(settings["database_path"]).resolve())
            namespace = installation_seed + "_" + hashlib.sha256(anchor.encode()).hexdigest()[:12]
            with patch.dict(os.environ, {"TIANSHU_STORAGE_NAMESPACE": namespace}):
                return operation(settings)

        def capture(settings):
            runtime = scoped(original, settings)
            self.backend_runtimes.append(runtime)
            return runtime

        readonly_patch = patch.object(
            backends, "readonly", lambda settings: scoped(original_readonly, settings)
        )
        readonly_patch.start()
        self.addCleanup(readonly_patch.stop)
        inspect_patch = patch.object(
            backends, "inspect", lambda settings: scoped(original_inspect, settings)
        )
        inspect_patch.start()
        self.addCleanup(inspect_patch.stop)
        capture_patch = patch.object(backends, "start", capture)
        capture_patch.start()
        self.addCleanup(capture_patch.stop)
        self.addCleanup(lambda: [runtime.close() for runtime in self.backend_runtimes])
        # Existing suites inspect/hold their owner's ledger directly. Route only those
        # explicit fixture connections to the very same PG handles as the application.
        original_connect = sqlite3.connect

        def fixture_connect(path, *args, **kwargs):
            for runtime in reversed(self.backend_runtimes):
                for handle in runtime.handles.values():
                    if str(path) == handle.path:
                        return handle.connect(*args, **kwargs)
            return original_connect(path, *args, **kwargs)

        self.fixture_connections = patch.object(sqlite3, "connect", fixture_connect)
        self.fixture_connections.start()
        self.addCleanup(self.fixture_connections.stop)
        super().setUp()


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLPlatformTests(PostgreSQLMixin, core_tests.PlatformTests):
    """The existing authority/revocation/CAS/publication regression on actual PG."""


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLDeliveryTests(PostgreSQLMixin, delivery_tests.DeliveryHTTPTests):
    @unittest.skip("Legacy SQLite in-place version fixture is exercised by the SQLite suite")
    async def test_migrate_existing_queue_preserves_nonempty_receipts_once(self):
        pass


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLHomeTests(PostgreSQLMixin, home_tests.HomeTests):
    """Real HA protocol fixture, including claims and unknown-send reconciliation."""

    async def start(self, config):
        # A restart must not block the HA fixture's shared event loop while the
        # PG startup checks run; that would expire a deliberately held HTTP call.
        self.config = config
        if self._testMethodName in {
            "test_a_cancelled_server_task_never_leads_to_a_second_command",
            "test_a_transmitted_command_without_its_receipt_stays_unconfirmed",
            "test_two_instances_sharing_the_ledger_still_send_one_command",
        }:
            # These tests intentionally hold a real HTTP request across fresh pool/schema
            # startup. Preserve that scenario within a declared deadline that includes PG
            # startup; the separate 2-second timeout tests retain their original settings.
            config["home"]["timeout_seconds"] = 10
        self.platform = await asyncio.to_thread(Platform, config)
        self.app = create_app(self.platform)
        self.runner, self.url = await start_http(self.app)
        self.addAsyncCleanup(self.runner.cleanup)
        config["web"]["origin"] = self.url
        self.console = next(
            handler.__self__
            for handler in (route.handler for route in self.app.router.routes())
            if isinstance(getattr(handler, "__self__", None), WebConsole)
        )
        return self.url


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLWebModelsTests(PostgreSQLMixin, model_tests.WebModelsTests):
    """Native publication claims, cross-ledger reconciliation and concurrent writers."""


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLInfrastructureTests(
    PostgreSQLMixin, infrastructure_tests.InfrastructureControlTests
):
    """Native durable scheduler; only the node protocol is synthetic."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The import source is SQLite; subsequent edits target the current PG owner.
        self.catalog = self.platform.infrastructure.catalog


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLHealthTests(unittest.TestCase):
    def test_pending_binding_refuses_old_sqlite_preflight_and_resumes_only_at_startup(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, ENV):
            settings = home_settings(folder, "http://127.0.0.1:41001")
            platform = Platform(settings)
            platform.media.repo.put("book_chapter", "pending", {"content": "不可回退到旧库"})
            platform.close()
            settings["storage_backends"] = {
                "postgresql_url": PG,
                "namespace": "ts231_" + uuid.uuid4().hex,
            }
            pending = backends.start(settings)
            Store(settings["database_path"], runtime=pending)
            pending.close()
            before = {
                str(p.relative_to(folder)): p.read_bytes()
                for p in Path(folder).rglob("*")
                if p.is_file()
            }
            readonly = backends.readonly(settings)
            self.assertEqual(readonly.backend, "postgresql")
            self.assertEqual(readonly.probe(), "failed")
            report = runtime_health.preflight(settings, validate=validate_settings)
            self.assertEqual(report["status"], "not_ready")
            self.assertEqual(report["checks"]["store"], "failed")
            self.assertTrue(report["requires_initialization"])
            self.assertEqual(
                before,
                {
                    str(p.relative_to(folder)): p.read_bytes()
                    for p in Path(folder).rglob("*")
                    if p.is_file()
                },
            )
            restarted = Platform(settings)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.storage_runtime.backend, "postgresql")
            self.assertEqual(restarted.storage_runtime.status()["state"], "active")
            self.assertEqual(
                restarted.media.repo.get("book_chapter", "pending")["content"], "不可回退到旧库"
            )

    def test_missing_historical_tables_refuse_restart_and_do_not_recreate_empty_ledgers(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, ENV):
            settings = home_settings(folder, "http://127.0.0.1:41001")
            settings["storage_backends"] = {
                "postgresql_url": PG,
                "namespace": "ts231_" + uuid.uuid4().hex,
            }
            platform = Platform(settings)
            self.addCleanup(platform.close)
            provider_dir = Path(folder) / "providers"
            ProviderCatalog(provider_dir, create=True, runtime=platform.storage_runtime)
            platform.storage_runtime.finalize()
            # Drop one representative authority, media, sender and encrypted catalogue table.
            for name, table in (
                ("authority", "origins"),
                ("media", "jobs"),
                ("bots", "replies"),
                ("providers", "providers"),
            ):
                with self.subTest(owner=name):
                    handle = platform.storage_runtime.handles[name]
                    with closing(handle.connect()) as connection, connection:
                        connection.native("DROP TABLE " + table + " CASCADE")
                    self.assertEqual(platform.storage_runtime.probe(), "failed")
                    restarted = backends.start(settings)
                    try:
                        with self.assertRaises(StorageError):
                            backends.database(restarted, name, handle.path)
                    finally:
                        restarted.close()
                    # Reconstructing an owner within the current runtime must also refuse.
                    if name == "providers":
                        with self.assertRaises(Fault):
                            ProviderCatalog(provider_dir, runtime=platform.storage_runtime)
                    with closing(handle.connect()) as connection:
                        self.assertIsNone(
                            connection.native(
                                "SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tablename=?",
                                (table,),
                            ).fetchone()
                        )

    def test_readonly_preflight_and_account_seal_are_checked_in_current_backend(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, ENV):
            settings = home_settings(folder, "http://127.0.0.1:41001")
            settings["web_account"] = {"mode": "claim"}
            settings["storage_backends"] = {
                "postgresql_url": PG,
                "namespace": "ts231_" + uuid.uuid4().hex,
            }
            before = {
                str(p.relative_to(folder)): p.read_bytes()
                for p in Path(folder).rglob("*")
                if p.is_file()
            }
            report = runtime_health.preflight(settings, validate=validate_settings)
            self.assertTrue(report["requires_initialization"])
            self.assertEqual(
                before,
                {
                    str(p.relative_to(folder)): p.read_bytes()
                    for p in Path(folder).rglob("*")
                    if p.is_file()
                },
            )
            platform = Platform(settings)
            self.addCleanup(platform.close)
            console = WebConsole(platform)
            console.account.create("owner", password_hash("synthetic-storage-password"))
            sealed = {
                str(p.relative_to(folder)): p.read_bytes()
                for p in Path(folder).rglob("*")
                if p.is_file()
            }
            report = runtime_health.preflight(settings, validate=validate_settings)
            self.assertEqual(report["checks"]["store"], "ok", report)
            self.assertEqual(report["checks"]["sidecars"], "ok", report)
            self.assertEqual(
                sealed,
                {
                    str(p.relative_to(folder)): p.read_bytes()
                    for p in Path(folder).rglob("*")
                    if p.is_file()
                },
            )
            self.assertEqual(platform.storage_runtime.backend, "postgresql")
            self.assertEqual(runtime_health.Probe(platform.health)._account_state(), "ok")
            from services.platform.__main__ import main

            bootstrap_directory = Path(folder) / "never-create-provider-key"
            settings["provider_self_service"] = {"directory": str(bootstrap_directory)}
            input_path = Path(folder) / "bootstrap-test.json"
            input_path.write_text(json.dumps(settings), encoding="utf-8")
            with (
                patch.object(
                    sys, "argv", ["platform", "--settings", str(input_path), "providers-init"]
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 1)
            self.assertFalse(bootstrap_directory.exists())
            console.account.seal.unlink()
            self.assertEqual(runtime_health.Probe(platform.health)._account_state(), "failed")
            # Losing the old SQLite file and its seal must never reactivate the deployment hash.
            console.account.path.unlink(missing_ok=True)
            without_account = {k: v for k, v in settings.items() if k != "web_account"}
            with self.assertRaises(Fault):
                WebAccount(without_account, runtime=platform.storage_runtime)


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLObservationTests(observation_tests.ObservationTests):
    def setUp(self):
        super().setUp()
        runtime = backends.start(
            {
                "database_path": self.platform.store.path,
                "storage_backends": {
                    "postgresql_url": PG,
                    "namespace": "ts231_" + uuid.uuid4().hex,
                },
            }
        )
        self.addCleanup(runtime.close)
        self.platform.storage_runtime = runtime
        self.platform.bot_adapters.catalog = BotAdapterCatalog(
            Path(self.temp.name) / "adapters",
            runtime=runtime,
        )
        self.manager = BotObservation(self.platform)
        runtime.finalize()

    def test_reply_generation_ddl_is_atomic_and_preserves_history(self):
        with closing(self.manager._db()) as db:
            with db:
                db.native("BEGIN")
                db.native("DROP TABLE reply_connections")
                db.native(
                    "CREATE TABLE reply_connections(connection_id TEXT PRIMARY KEY,"
                    "observation_id TEXT NOT NULL,conversation TEXT NOT NULL,actor_id TEXT NOT NULL,"
                    "UNIQUE(observation_id,conversation,actor_id),"
                    "UNIQUE(observation_id,connection_id))"
                )
                db.execute(
                    "INSERT INTO reply_connections VALUES(?,?,?,?)",
                    ("bot:old", "obs:synthetic", "group:20002", "actor:a"),
                )
                db.execute("INSERT INTO reply_authors VALUES(?,?)", ("bot:old", "30003"))

            def rows():
                return [
                    tuple(row)
                    for row in db.execute("SELECT * FROM reply_connections ORDER BY connection_id")
                ]

            def constraints():
                return [
                    (kind, list(columns))
                    for kind, columns in db.native(
                        "SELECT c.contype,array_agg(a.attname ORDER BY k.ord) FROM pg_constraint c "
                        "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(num,ord) "
                        "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num "
                        "WHERE c.conrelid='reply_connections'::regclass GROUP BY c.conname,c.contype "
                        "ORDER BY c.conname"
                    )
                ]

            original, original_constraints = rows(), constraints()
            authors = [
                tuple(row)
                for row in db.execute("SELECT * FROM reply_authors ORDER BY connection_id,author")
            ]

            class FailedDDL:
                @property
                def in_transaction(self):
                    return db.in_transaction

                def native(self, statement, *args):
                    cursor = db.native(statement, *args)
                    if statement.startswith("ALTER TABLE reply_connections DROP CONSTRAINT "):
                        raise RuntimeError("synthetic_after_drop_before_commit")
                    return cursor

                def __enter__(self):
                    db.__enter__()
                    return self

                def __exit__(self, *args):
                    return db.__exit__(*args)

            with self.assertRaisesRegex(RuntimeError, "synthetic_after_drop_before_commit"):
                self.manager._migrate_reply_generations(FailedDDL())
            self.assertEqual(rows(), original)
            self.assertEqual(constraints(), original_constraints)

            self.manager._migrate_reply_generations(db)
            expected = [
                value
                for value in original_constraints
                if value != ("u", ["observation_id", "conversation", "actor_id"])
            ]
            self.assertEqual(constraints(), expected)
            self.assertIn(("p", ["connection_id"]), expected)
            self.assertEqual(rows(), original)
            self.assertEqual(
                [
                    tuple(row)
                    for row in db.execute(
                        "SELECT * FROM reply_authors ORDER BY connection_id,author"
                    )
                ],
                authors,
            )
            with db:
                db.execute(
                    "INSERT INTO reply_connections VALUES(?,?,?,?)",
                    ("bot:new", "obs:synthetic", "group:20002", "actor:a"),
                )
            self.assertEqual(len(rows()), 2)
            self.assertIn(original[0], rows())
            with self.assertRaises(sqlite3.IntegrityError):
                with db:
                    db.execute(
                        "INSERT INTO reply_connections VALUES(?,?,?,?)",
                        ("bot:new", "obs:other", "group:30003", "actor:b"),
                    )
            self.assertEqual(len(rows()), 2)
            before_reentry = rows(), constraints()
            self.manager._migrate_reply_generations(db)
            self.assertEqual((rows(), constraints()), before_reentry)


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires a disposable PostgreSQL database")
class PostgreSQLRoleTests(unittest.TestCase):
    def scenario(self, function):
        with tempfile.TemporaryDirectory() as folder:
            runtime = backends.start(
                {
                    "database_path": str(Path(folder) / "platform.sqlite"),
                    "storage_backends": {
                        "postgresql_url": PG,
                        "namespace": "ts231_" + uuid.uuid4().hex,
                    },
                }
            )
            self.addCleanup(runtime.close)
            original = RoleRuntime.__init__

            def initialize(owner, platform):
                platform.storage_runtime = runtime
                original(owner, platform)

            with patch.object(RoleRuntime, "__init__", initialize):
                function(Path(folder))
            runtime.finalize()
            self.assertEqual(runtime.probe(), "ok")
            runtime.close()

    def test_role_apply_replay_restart_and_disable(self):
        self.scenario(role_tests.test_two_roles_apply_replay_restart_and_disable)

    def test_lost_peer_receipt_replays_stage_without_second_effect(self):
        self.scenario(role_tests.test_lost_peer_receipts_replay_exact_stage_without_second_effect)

    def test_cancel_after_provider_revocation_fences_old_intent(self):
        self.scenario(
            role_tests.test_cancel_pending_after_provider_revocation_reboots_and_fences_old_intent
        )


class PostgreSQLMediaMixin:
    def setUp(self):
        self.backend_folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.backend_folder.cleanup)
        self.media_runtimes = {}
        prefix = "ts231_" + uuid.uuid4().hex
        original = Repository.__init__

        def initialize(repo, path, *, clock=None, runtime=None):
            if runtime is None:
                identity = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]
                if identity not in self.media_runtimes:
                    core = StorageRuntime(
                        StorageOptions.from_settings(
                            {
                                "storage_backends": {
                                    "postgresql_url": PG,
                                    "namespace": prefix + identity,
                                }
                            }
                        ),
                        service="platform",
                        state_directory=Path(self.backend_folder.name) / identity,
                    )
                    core.start()
                    self.media_runtimes[identity] = backends.PlatformRuntime(core)
                runtime = self.media_runtimes[identity]
            kwargs = {} if clock is None else {"clock": clock}
            original(repo, path, runtime=runtime, **kwargs)
            runtime.finalize()

        constructor = patch.object(Repository, "__init__", initialize)
        constructor.start()
        self.addCleanup(constructor.stop)
        self.addCleanup(lambda: [runtime.close() for runtime in self.media_runtimes.values()])
        super().setUp()


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires disposable PostgreSQL")
class PostgreSQLBooksTests(PostgreSQLMediaMixin, book_tests.BookTests):
    pass


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires disposable PostgreSQL")
class PostgreSQLAudioTests(PostgreSQLMediaMixin, audio_tests.BookAudioTests):
    pass


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires disposable PostgreSQL")
class PostgreSQLComicsTests(PostgreSQLMediaMixin, comic_tests.ComicTests):
    pass


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires disposable PostgreSQL")
class PostgreSQLMusicBudgetTests(PostgreSQLMediaMixin, music_budget_tests.MusicRequestControlTests):
    pass


@unittest.skipUnless(PG, "TS231_TEST_POSTGRES requires disposable PostgreSQL")
class PostgreSQLLyricsTests(PostgreSQLMediaMixin, lyric_tests.MusicLyricsTests):
    pass


@unittest.skipUnless(REDIS, "TS231_TEST_REDIS requires disposable Redis")
class RedisMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_hits_account_revision_strong_refresh_and_loss_fall_back(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = backends.start(
                {
                    "database_path": str(Path(folder) / "state.sqlite"),
                    "storage_backends": {
                        "redis_url": REDIS,
                        "namespace": "ts231_" + uuid.uuid4().hex,
                    },
                }
            )
            self.addCleanup(runtime.close)
            first = MetadataCache(shared=runtime.cache, scope="netease")
            second = MetadataCache(shared=runtime.cache, scope="netease")
            calls = []

            async def fetch():
                calls.append(1)
                return {"title": "synthetic", "sequence": len(calls)}

            cookie = {"_music_account_id": "one", "_music_account_revision": 1}
            a = await first.read("search", cookie, "query", fetch)
            self.assertEqual(await second.read("search", cookie, "query", fetch), a)
            self.assertEqual(len(calls), 1)
            await second.read("search", {**cookie, "_music_account_revision": 2}, "query", fetch)
            await first.read("search", cookie, "query", fetch, fresh=True)
            self.assertEqual(len(calls), 3)
            self.assertEqual(runtime.status()["backend"], "sqlite")
