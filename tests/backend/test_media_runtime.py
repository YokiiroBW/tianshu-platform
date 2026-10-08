import base64
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp

from fixtures import ENV, start_http
from media_runtime_fixtures import (
    BV,
    OTHER_BV,
    QUALITY,
    PublicationFixture,
    SyntheticEngine,
    Upstream,
    settings,
    subscription,
)
from services.platform.contracts import Fault
from services.platform.media.accounts import Accounts
from services.platform.media.bilibili import Bilibili
from services.platform.media.config import source
from services.platform.media.subscriptions import job_payload
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD


class MediaRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(
            os.environ, {**ENV, "SYNTHETIC_MEDIA_PUBLISH": "synthetic-local-only"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.upstream = await Upstream().start()
        self.addAsyncCleanup(self.upstream.runner.cleanup)
        self.publisher = await PublicationFixture(Path(self.temp.name) / "staging").start()
        self.addAsyncCleanup(self.publisher.runner.cleanup)
        self.engine_config = {
            "python": sys.executable,
            "ffmpeg": os.environ.get("MEDIA_TEST_FFMPEG", "ffmpeg"),
            "ffprobe": os.environ.get("MEDIA_TEST_FFPROBE", "ffprobe"),
        }
        self.config = settings(self.temp.name, self.engine_config, self.publisher.url)
        self.platform = Platform(self.config)
        self.addCleanup(self.platform.close)
        self.media = self.platform.media
        self.media.connector = Bilibili(api=self.upstream.url, passport=self.upstream.url)
        self.media.engine = SyntheticEngine(self.media.config["engine"])

        async def manual_start():
            pass

        self.media.start = manual_start
        self.runner, self.url = await start_http(create_app(self.platform))
        self.addAsyncCleanup(self.runner.cleanup)
        self.config["web"]["origin"] = self.url
        self.client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.addAsyncCleanup(self.client.close)
        async with self.client.get(self.url + "/api/web/session") as response:
            session = await response.json()
        async with self.client.post(
            self.url + "/api/web/login",
            json={"username": "synthetic-admin", "password": PASSWORD},
            headers={"Origin": self.url, "X-CSRF-Token": session["csrf"]},
        ) as response:
            self.csrf = (await response.json())["csrf"]

    async def post(self, operation, body, expected=200):
        async with self.client.post(
            self.url + "/api/web/media/" + operation,
            json=body,
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            value = await response.json()
            self.assertEqual(response.status, expected, value)
            return value

    async def save(self, body=None):
        return (await self.post("subscriptions/save", body or subscription()))["subscription"]

    async def enqueue(self, cids=None):
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        return self.media.repo.enqueue(
            job_payload(
                resolved,
                cids or self.upstream.parts[BV],
                None,
                "synthetic-network-video",
                dict(QUALITY),
            )
        )

    async def run_job(self, identity):
        claimed = self.media.repo.claim("test-worker")
        self.assertEqual(claimed["job_id"], identity)
        await self.media.worker.run(claimed, "test-worker")
        return self.media.repo.job(identity)

    async def test_four_sources_links_and_anonymous_signing(self):
        links = {
            "favorite": ("https://space.bilibili.com/946974/favlist?fid=123", "123"),
            "uploader": ("https://space.bilibili.com/946974/video", "946974"),
            "collection": ("https://space.bilibili.com/946974/lists/456?type=season", "946974:456"),
            "series": ("https://space.bilibili.com/946974/lists/456?type=series", "946974:456"),
        }
        for kind, (url, identity) in links.items():
            value = source({"kind": kind, "id": url})
            self.assertEqual(value, {"kind": kind, "id": identity})
            self.assertEqual(await self.media.connector.scan(value, None), {BV})
        with self.assertRaises(Fault):
            source({"kind": "collection", "id": links["series"][0]})
        self.assertEqual(
            (await self.media.connector.resolve(BV))["video"]["parts"][0]["formats"][0][
                "quality_id"
            ],
            "80",
        )
        with self.assertRaises(Fault) as caught:
            await self.media.connector.account(None)
        self.assertEqual(caught.exception.code, "auth_required")

    async def test_membership_new_p_old_favorite_readd_and_cross_subscription_count(self):
        first = await self.save(subscription("future_only"))
        self.assertEqual(
            (await self.media.subscriptions.scan(first["subscription_id"]))["queued"], 0
        )
        self.upstream.parts[BV].append("444444444")
        changed = await self.media.subscriptions.scan(first["subscription_id"])
        self.assertEqual(changed["queued"], 1)
        jobs, _ = self.media.repo.jobs()
        self.assertEqual(jobs[0]["selected_cids"], ["444444444"])
        self.upstream.members.append(OTHER_BV)
        self.assertEqual(
            (await self.media.subscriptions.scan(first["subscription_id"]))["queued"], 1
        )
        self.upstream.members.remove(BV)
        await self.media.subscriptions.scan(first["subscription_id"])
        self.upstream.members.append(BV)
        self.assertEqual(
            (await self.media.subscriptions.scan(first["subscription_id"]))["queued"], 0
        )
        second = await self.save(subscription("history"))
        result = await self.media.subscriptions.scan(second["subscription_id"])
        self.assertEqual(result["queued"], 1)  # Full BV is new; OTHER_BV is already queued.
        self.assertEqual(result["subscription"]["counts"]["queued"], 1)
        third = await self.save(subscription("history"))
        self.assertEqual(
            (await self.media.subscriptions.scan(third["subscription_id"]))["queued"], 0
        )

    async def test_incomplete_scan_preserves_baseline_and_rule_revision_does_not_requeue(self):
        current = await self.save(subscription("future_only"))
        self.upstream.fail_page = True
        with self.assertRaises(Fault):
            await self.media.subscriptions.scan(current["subscription_id"])
        self.assertFalse(
            self.media.repo.get("subscription", current["subscription_id"])["baseline_ready"]
        )
        self.assertEqual(self.media.repo.members(current["subscription_id"]), set())
        self.upstream.fail_page = False
        await self.media.subscriptions.scan(current["subscription_id"])
        saved = self.media.repo.get("subscription", current["subscription_id"])
        body = subscription("future_only")
        body.update(
            subscription_id=saved["subscription_id"],
            expected_revision=saved["revision"],
            label="改名",
            rules={**body["rules"], "revision": 2},
        )
        await self.save(body)
        self.assertEqual(
            (await self.media.subscriptions.scan(saved["subscription_id"]))["queued"], 0
        )

    async def test_large_legal_unicode_policy_and_idempotency(self):
        body = subscription()
        body["rules"]["blacklist"] = [
            {
                "id": f"g{i}",
                "rules": [
                    {
                        "id": f"r{i}_{j}",
                        "field": "title",
                        "op": "contains",
                        "value": "汉" * 4096,
                        "case_sensitive": True,
                    }
                    for j in range(10)
                ],
            }
            for i in range(20)
        ]
        self.assertGreater(len(json.dumps(body, ensure_ascii=False).encode()), 256 * 1024)
        saved = await self.post("subscriptions/save", body)
        replay = await self.post("subscriptions/save", body)
        self.assertTrue(replay["replayed"])
        self.assertEqual(saved["subscription"], replay["subscription"])
        changed = {**body, "label": "冲突"}
        self.assertEqual(
            (await self.post("subscriptions/save", changed, 409))["code"], "idempotency_conflict"
        )

    async def test_jobs_keyset_pages_and_filter_bound_cursor(self):
        resolved = await self.media.connector.resolve(BV, with_formats=False)
        for index in range(205):
            payload = job_payload(
                resolved,
                [self.upstream.parts[BV][0]],
                None,
                "synthetic-network-video",
                {"mode": "exact", "quality_id": str(index + 1), "allow_fallback": False},
            )
            self.media.repo.enqueue(payload)
        result, all_ids, cursor = None, [], None
        while result is None or result["page"]["has_more"]:
            result = await self.post(
                "jobs/list", {"states": None, "page_size": 40, "cursor": cursor}
            )
            all_ids += [job["job_id"] for job in result["jobs"]]
            cursor = result["page"]["next_cursor"]
        self.assertEqual(len(all_ids), 205)
        self.assertEqual(len(set(all_ids)), 205)
        first = await self.post("jobs/list", {"states": None, "page_size": 40, "cursor": None})
        self.assertEqual(
            (
                await self.post(
                    "jobs/list",
                    {"states": ["failed"], "page_size": 40, "cursor": first["page"]["next_cursor"]},
                    409,
                )
            )["code"],
            "cursor_conflict",
        )

    async def test_partial_p_download_retry_reuses_verified_first_p_and_latest_three_editions(self):
        job = await self.enqueue()
        self.media.engine.fail_cid = self.upstream.parts[BV][1]
        failed = await self.run_job(job["job_id"])
        self.assertEqual(set(failed["downloads"]), {self.upstream.parts[BV][0]})
        self.media.repo.update_job(job["job_id"], {"state": "queued", "cancel_requested": False})
        self.media.engine.fail_cid = None
        done = await self.run_job(job["job_id"])
        self.assertEqual(done["state"], "published")
        self.assertEqual(self.media.engine.calls.count(self.upstream.parts[BV][0]), 1)
        operations = [done["asset_operation"]["operation_id"]]
        for cid in ("444444444", "555555555"):
            self.upstream.parts[BV].append(cid)
            added = await self.enqueue([cid])
            done = await self.run_job(added["job_id"])
            self.assertEqual(done["state"], "published")
            package = json.loads(base64.b64decode(done["package"]["manifest_base64"]))
            self.assertEqual(set(done["selected_cids"]), set(self.upstream.parts[BV]))
            self.assertEqual(done["package"]["base_operation_id"], operations[-1])
            self.assertEqual(
                len([entry for entry in package["files"] if entry["kind"] == "video"]),
                len(self.upstream.parts[BV]),
            )
            operations.append(done["asset_operation"]["operation_id"])
        self.assertEqual(
            self.media.repo.get("target_head", "synthetic-network-video:" + BV)["operation_id"],
            operations[-1],
        )

    async def test_source_p_reorder_uses_current_index_and_deleted_cid_fails(self):
        job = await self.enqueue([self.upstream.parts[BV][0]])
        self.upstream.parts[BV].reverse()
        done = await self.run_job(job["job_id"])
        self.assertEqual(done["downloads"][job["cid"]]["source_index"], 2)
        self.upstream.parts[BV].append("444444444")
        missing = await self.enqueue(["444444444"])
        self.upstream.parts[BV].remove("444444444")
        failed = await self.run_job(missing["job_id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["code"], "source_cid_removed")
        self.assertNotIn("444444444", self.media.engine.calls)

    async def test_bounded_publication_retry_and_manual_resume(self):
        job = await self.enqueue()
        self.publisher.fail = True
        delays = []
        for attempt in range(6):
            if attempt:
                self.media.repo.update_job(job["job_id"], {"state": "queued"})
            failed = await self.run_job(job["job_id"])
            delays.append(failed["retry_delay"])
        self.assertEqual(failed["attempts"], 6)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(delays[:5], [30, 60, 120, 240, 480])
        self.assertEqual(len(self.media.engine.calls), 2)
        self.publisher.fail = False
        await self.post(
            "jobs/control",
            {"job_id": job["job_id"], "action": "retry", "client_id": str(uuid.uuid4())},
        )
        done = await self.run_job(job["job_id"])
        self.assertEqual(done["state"], "published")
        self.assertEqual(done["attempts"], 0)

    async def test_metadata_refresh_cas_original_evidence_and_encrypted_account_restart(self):
        job = await self.enqueue()
        job = self.media.repo.update_job(
            job["job_id"], {"state": "waiting_metadata", "code": "metadata_incomplete"}
        )
        self.upstream.description = "来源更新后的完整简介"
        await self.post(
            "jobs/refresh_metadata",
            {
                "job_id": job["job_id"],
                "expected_revision": job["revision"] - 1,
                "client_id": str(uuid.uuid4()),
            },
            409,
        )
        fresh = (
            await self.post(
                "jobs/refresh_metadata",
                {
                    "job_id": job["job_id"],
                    "expected_revision": job["revision"],
                    "client_id": str(uuid.uuid4()),
                },
            )
        )["job"]
        self.assertEqual(fresh["state"], "queued")
        self.assertEqual(
            self.media.repo.list("snapshot_history")[0]["snapshot"]["description"], "完整合成简介"
        )
        cookie = "SESSDATA=synthetic-sensitive-only; DedeUserID=946974"
        account = self.media.accounts.save("测试", cookie, {"mid": "946974"})
        self.media.accounts.mark(account["account_id"], "auth_required", "expired")
        restarted = Accounts(self.media.repo)
        self.assertEqual(restarted.cookie(account["account_id"], True), cookie)
        self.assertNotIn(cookie.encode(), self.media.repo.database.read_bytes())

    async def test_expired_owner_cannot_mutate_current_generation(self):
        job = await self.enqueue()
        first = self.media.repo.claim("first", lease_seconds=-1)
        second = self.media.repo.claim("second")
        self.assertEqual(second["generation"], first["generation"] + 1)
        with self.assertRaises(Fault) as caught:
            self.media.repo.update_job(
                job["job_id"], {"state": "published"}, owner="first", generation=first["generation"]
            )
        self.assertEqual(caught.exception.code, "lease_conflict")

    async def test_subscription_one_p_grows_in_stable_layout_and_quality_upgrade_keeps_base(self):
        self.upstream.parts[BV] = ["111111111"]
        sub = await self.save()
        await self.media.subscriptions.scan(sub["subscription_id"])
        jobs, _ = self.media.repo.jobs()
        first = await self.run_job(jobs[0]["job_id"])
        self.assertEqual(first["package"]["layout"], "multipart")
        first_manifest = json.loads(base64.b64decode(first["package"]["manifest_base64"]))
        first_video = next(entry for entry in first_manifest["files"] if entry["kind"] == "video")
        self.upstream.parts[BV].append("222222222")
        self.assertEqual((await self.media.subscriptions.scan(sub["subscription_id"]))["queued"], 1)
        jobs, _ = self.media.repo.jobs(states={"queued"})
        second = await self.run_job(jobs[0]["job_id"])
        second_manifest = json.loads(base64.b64decode(second["package"]["manifest_base64"]))
        self.assertEqual(
            next(
                entry
                for entry in second_manifest["files"]
                if entry["kind"] == "video" and entry["cid"] == "111111111"
            ),
            first_video,
        )
        saved = self.media.repo.get("subscription", sub["subscription_id"])
        body = subscription()
        body.update(
            subscription_id=sub["subscription_id"],
            expected_revision=saved["revision"],
            quality={"mode": "exact", "quality_id": "80", "allow_fallback": False},
        )
        await self.save(body)
        self.assertEqual((await self.media.subscriptions.scan(sub["subscription_id"]))["queued"], 1)
        jobs, _ = self.media.repo.jobs(states={"queued"})
        upgraded = await self.run_job(jobs[0]["job_id"])
        self.assertEqual(upgraded["state"], "published")
        self.assertEqual(
            upgraded["package"]["base_operation_id"], second["asset_operation"]["operation_id"]
        )
        self.assertEqual(set(upgraded["package"]["replace_video_cids"]), {"111111111", "222222222"})
        self.assertEqual(self.media.engine.calls.count("111111111"), 2)

    async def test_mapping_changes_during_download_rejects_unproven_output(self):
        job = await self.enqueue(["111111111"])
        original_download = self.media.engine.download

        async def moving_download(*arguments):
            value = await original_download(*arguments)
            self.upstream.parts[BV].reverse()
            return value

        self.media.engine.download = moving_download
        failed = await self.run_job(job["job_id"])
        self.assertEqual(failed["code"], "source_identity_changed")
        self.assertEqual(failed["state"], "retry_wait")
        self.assertFalse(failed.get("downloads"))
        self.assertIsNone(failed.get("package"))

    async def test_account_check_preserves_credential_revision_and_peer_http_config(self):
        from services.platform.media.publication import validate_connection

        validate_connection({"base_url": "http://assetlibrary:7844", "token_env": "PUBLISH_TOKEN"})
        with self.assertRaises(Fault):
            validate_connection(
                {"base_url": "https://user:secret@peer.invalid", "token_env": "PUBLISH_TOKEN"}
            )
        cookie = "SESSDATA=synthetic-only; DedeUserID=946974"
        account = self.media.accounts.save("合成账号", cookie, {"mid": "946974"})
        checked = await self.post("accounts/check", {"account_id": account["account_id"]})
        self.assertEqual(checked["account"]["revision"], account["revision"])
        original = self.media.connector.account

        async def unavailable(value):
            raise Fault("source_unavailable", 503)

        self.media.connector.account = unavailable
        await self.post("accounts/check", {"account_id": account["account_id"]}, 503)
        self.assertEqual(self.media.repo.get("account", account["account_id"])["state"], "ready")
        self.media.connector.account = original

    async def test_task_centre_projects_the_media_owner_ledger(self):
        job = await self.enqueue()
        async with self.client.post(
            self.url + "/api/web/tasks/view",
            json={"source": "resources.download", "page_size": 1},
            headers={"Origin": self.url, "X-CSRF-Token": self.csrf},
        ) as response:
            view = await response.json()
            self.assertEqual(response.status, 200, view)
        self.assertEqual(view["items"][0]["task_id"], "media:" + job["job_id"])
        self.assertEqual(view["items"][0]["status"], "accepted")
        self.assertEqual(view["items"][0]["module"]["page"], "#/resources/2")
        self.assertEqual(
            next(item for item in view["sources"] if item["id"] == "resources.download")["records"],
            1,
        )
