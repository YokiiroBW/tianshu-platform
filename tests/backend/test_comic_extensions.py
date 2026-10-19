import asyncio
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from services.platform.contracts import Fault
from services.platform.media.runtime import Media
from services.platform.media.comics.extension_stores import STORES, catalogue, select_packages
from services.platform.media.comics.extensions import DAY, Extensions
from services.platform.media.comics.suwayomi import Suwayomi


def archive(version=2, sources=("41",)):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("fixture.json", json.dumps({"version": version, "sources": sources}))
    return output.getvalue()


def candidate(pkg="fixture.comic", version=2, sources=("41",), warning="SAFE"):
    return {
        "package": pkg,
        "name": pkg,
        "store_id": "keiyoushi",
        "store_url": STORES[0]["url"],
        "version": f"1.6.{version}",
        "version_code": version,
        "lib": "1.6",
        "source_ids": list(sources),
        "languages": ["zh"],
        "content_warning": warning,
        "artifact_url": "https://github.com/fixture/comic.jar",
        "state": "queued",
        "code": "comic_extension_queued",
    }


class Engine:
    def __init__(self, installed_root):
        self.source_locks, self.records, self.calls = {}, {}, []
        self.root = installed_root
        self.lose_response = False

    def record(self, pkg, version, ids):
        filename = pkg + "-" + str(version) + ".jar"
        self.root.joinpath(filename).write_bytes(archive(version, ids))
        self.records[pkg] = {
            "pkgName": pkg,
            "name": pkg,
            "apkName": filename.removesuffix(".jar") + ".apk",
            "versionName": f"1.6.{version}",
            "versionCodeLong": str(version),
            "storeIndexUrl": STORES[0]["url"],
            "isInstalled": True,
            "contentWarning": "SAFE",
            "source": {"nodes": [{"id": i} for i in ids]},
        }

    async def close(self):
        pass

    async def client(self):
        return None

    async def extensions(self):
        return copy.deepcopy(list(self.records.values()))

    async def extension(self, pkg):
        return copy.deepcopy(self.records.get(pkg))

    async def extension_stores(self):
        return [{"indexUrl": s["url"]} for s in STORES]

    async def refresh_extensions(self):
        pass

    async def install_extension_file(self, path):
        self.calls.append(Path(path).name)
        with zipfile.ZipFile(path) as z:
            d = json.loads(z.read("fixture.json"))
        pkg = Path(path).name.rsplit("-", 1)[0]
        self.record(pkg, d["version"], d["sources"])
        if self.lose_response:
            self.lose_response = False
            raise Fault("comic_extension_install_unverified", 503)
        return self.records[pkg]


class ExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.now = 1800000000
        self.media = Media(
            {"database_path": str(root / "media.sqlite")},
            clock=lambda: self.now,
            local=asyncio.to_thread,
        )
        self.addAsyncCleanup(self.media.close)
        self.comics = self.media.comics
        self.comics.sources.engine_id = AsyncMock(return_value="fixture-engine")
        self.comics.sources.refresh = AsyncMock(return_value={})
        self.engine = Engine(root)
        self.comics.engine = self.engine
        self.manager = self.comics.extensions = Extensions(self.comics)
        self.manager.installed_root = root
        self.manager.save_policy(
            "fixture-engine", {"enabled": True, "state": "queued", "next_check_at": 0}
        )
        self.rows = [candidate()]

        async def store(client, descriptor):
            return (
                (copy.deepcopy(self.rows), "fixture-signing-key")
                if descriptor["id"] == "keiyoushi"
                else ([], "fixture-signing-key")
            )

        self.store_patch = patch(
            "services.platform.media.comics.extensions.read_store", side_effect=store
        )
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.fetch = AsyncMock(return_value=archive())
        self.fetch_patch = patch("services.platform.media.comics.extensions.fetch", self.fetch)
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)

    async def test_daily_maintenance_is_durable_idempotent_and_preserves_subscriptions(self):
        self.engine.record("fixture.comic", 1, ["41"])
        original = self.media.repo.put(
            "comic_subscription",
            "old",
            {"state": "active", "seen": ["old-version"], "interval_seconds": 21600},
        )
        await self.manager.run_once()
        row = (await self.manager.rows("fixture-engine"))[0]
        self.assertEqual(row["state"], "installed")
        self.assertEqual(row["backup"]["version_code"], 1)
        policy = await self.manager.policy()
        self.assertEqual(policy["next_check_at"], self.now + DAY)
        self.assertEqual(policy["revision"], 1)
        await self.manager.run_once()
        self.assertEqual(len(self.engine.calls), 1)
        self.now += DAY
        await self.manager.run_once()
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.media.repo.get("comic_subscription", "old"), original)

    async def test_response_loss_and_process_restart_reconcile_without_reinstall(self):
        self.engine.lose_response = True
        await self.manager.run_once()
        self.assertEqual((await self.manager.rows("fixture-engine"))[0]["state"], "verifying")
        self.now += 181
        fresh = Extensions(self.comics)
        fresh.installed_root = self.manager.installed_root
        await fresh.run_once()
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual((await fresh.rows("fixture-engine"))[0]["state"], "installed")

    async def test_failed_upgrade_restores_old_version_and_original_sources(self):
        self.engine.record("fixture.comic", 1, ["41"])
        self.rows = [candidate(sources=("42",))]
        self.fetch.return_value = archive(2, ["42"])
        await self.manager.run_once()
        row = (await self.manager.rows("fixture-engine"))[0]
        self.assertTrue(row["restored"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(self.engine.records["fixture.comic"]["versionName"], "1.6.1")
        self.assertEqual(self.engine.records["fixture.comic"]["source"]["nodes"], [{"id": "41"}])

    async def test_busy_source_is_deferred_without_fetch_or_uninstall(self):
        self.engine.record("fixture.comic", 1, ["41"])
        lock = self.engine.source_locks.setdefault("41", asyncio.Lock())
        await lock.acquire()
        try:
            await self.manager.run_once()
            self.assertEqual((await self.manager.rows("fixture-engine"))[0]["state"], "busy")
            self.fetch.assert_not_awaited()
            self.assertEqual(self.engine.calls, [])
        finally:
            lock.release()
        self.now += 31
        await self.manager.run_once()
        self.assertEqual((await self.manager.rows("fixture-engine"))[0]["state"], "installed")

    async def test_missing_backup_preserves_installed_plugin(self):
        self.engine.record("fixture.comic", 1, ["41"])
        self.manager.installed_root = self.manager.installed_root / "missing"
        await self.manager.run_once()
        row = (await self.manager.rows("fixture-engine"))[0]
        self.assertEqual(row["code"], "comic_extension_backup_unavailable")
        self.assertEqual(self.engine.calls, [])
        self.assertTrue(row["installed"])

    async def test_grouped_view_and_pause_preserve_plugin_records(self):
        self.rows = [candidate(), candidate("fixture.private", sources=("42",), warning="MIXED")]
        self.manager.save_policy("fixture-engine", {"enabled": False})
        await self.manager.run_once()
        self.assertEqual(self.engine.calls, [])
        await self.manager.prepare(await self.manager.policy())
        general = await self.manager.view({"library_kind": "general"})
        private = await self.manager.view({"library_kind": "private"})
        self.assertEqual([r["package"] for r in general["plugins"]], ["fixture.comic"])
        self.assertEqual([r["package"] for r in private["plugins"]], ["fixture.private"])

    async def test_pagination_includes_sources_after_first_500(self):
        engine = Suwayomi(AsyncMock())
        engine.query = AsyncMock(
            side_effect=[
                {"sources": {"nodes": [{"id": str(i)} for i in range(500)], "totalCount": 501}},
                {"sources": {"nodes": [{"id": "500"}], "totalCount": 501}},
            ]
        )
        rows, total = await engine.sources()
        self.assertEqual(len(rows), 500)
        self.assertEqual(total, 501)
        self.assertEqual(rows[-1]["id"], "500")
        self.assertEqual(engine.query.await_args_list[1].args[1], {"offset": 500})

    async def test_aggregate_search_does_not_silently_drop_sources_after_100(self):
        self.comics.sources.list = AsyncMock(
            return_value=[
                {
                    "source_id": str(i),
                    "source_key": "fixture:" + str(i),
                    "name": "fixture",
                    "enabled": True,
                    "language": "zh",
                    "content_warning": "SAFE",
                }
                for i in range(121)
            ]
        )
        self.engine.search = AsyncMock(return_value={"mangas": [], "hasNextPage": False})
        session = object()
        result = await self.comics.sources.search({"query": "fixture"}, session)
        await self.comics.sources.searches[result["search_id"]]["task"]
        completed = self.comics.sources.results(result["search_id"], session)
        self.assertEqual(completed["total"], 121)
        self.assertEqual(len(completed["sources"]), 121)
        self.assertEqual(self.engine.search.await_count, 121)

    def test_dedupe_retains_existing_origin_and_excludes_source_collisions(self):
        self.engine.record("fixture.comic", 1, ["41"])
        preferred = candidate()
        fork = {
            **preferred,
            "store_id": "yuzono",
            "store_url": STORES[1]["url"],
            "version_code": 20,
        }
        collision = candidate("fixture.duplicate")
        planned = select_packages([fork, preferred, collision], self.engine.records, {})
        self.assertEqual(planned[0]["store_id"], "keiyoushi")
        self.assertEqual(planned[0]["duplicates"], 1)
        self.assertEqual(planned[1]["state"], "source_conflict")

    def test_migration_placeholder_is_not_an_installable_extension(self):
        doc = {
            "extensionList": {
                "extensions": [
                    {
                        "packageName": "fixture.migration",
                        "versionCode": "1",
                        "versionName": "1.6.1",
                        "resources": {"jarUrl": "https://github.com/keiyoushi/extensions/"},
                        "sources": [{"id": "0"}],
                    }
                ]
            }
        }
        self.assertEqual(catalogue(STORES[1], doc)[0]["state"], "placeholder")
