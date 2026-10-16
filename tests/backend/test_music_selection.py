import hashlib
import os
import unittest

from music_fixtures import SyntheticNetease
import test_music_downloads as fixtures
from test_music_library import encrypted_ncm
from services.platform.media.music.providers import track, bridge_song
from services.platform.media.music.request_control import Deferred


class MusicSelectionTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(fixtures.MusicDownloadTests.setUpClass.__func__)
    asyncSetUp = fixtures.MusicDownloadTests.asyncSetUp
    fixture_url = fixtures.MusicDownloadTests.fixture_url
    value = fixtures.MusicDownloadTests.value
    subscribe = fixtures.MusicDownloadTests.subscribe
    queue = fixtures.MusicDownloadTests.queue
    run_job = fixtures.MusicDownloadTests.run_job
    file = fixtures.MusicDownloadTests.file

    def test_kugou_high_resolution_hash_and_fallbacks_survive_metadata_normalization(self):
        row = track(
            {
                "id": "base",
                "name": "Song",
                "artist": "Artist",
                "duration": 3,
                "extra": {
                    "hash": "base",
                    "res_hash": "hires",
                    "sq_hash": "lossless",
                    "hq_hash": "high",
                    "file_hash": "standard",
                    "privilege": "8",
                    "cookie": "must-not-survive",
                    "url": "https://example.invalid/private",
                },
            },
            "kugou",
        )
        prepared = bridge_song(row, "kugou")
        self.assertEqual(prepared["extra"]["hash"], "hires")
        self.assertEqual(prepared["extra"]["sq_hash"], "lossless")
        self.assertEqual(prepared["extra"]["hq_hash"], "high")
        self.assertEqual(row["provider_data"]["hash"], "base")
        self.assertNotIn("cookie", prepared["extra"])
        self.assertNotIn("url", prepared["extra"])

    def channels(self, choices):
        queried, full_transfers = [], []
        original = self.api.value["tracks"][0]
        for provider, (body, level) in choices.items():
            client = self.api if provider == "netease" else SyntheticNetease()

            async def search(query, cookie, provider=provider):
                return [{**original, "provider": provider, "source_id": provider + "101"}]

            async def download(*args, provider=provider, body=body, level=level):
                queried.append(provider)
                return {
                    "url": self.origin + "/file",
                    "level": level,
                    "size": len(body),
                    "md5": hashlib.md5(body).hexdigest(),
                }

            client.download, client.search = download, search
            client.lyrics = self.api.lyrics
            if provider != "netease":
                self.music.connectors[provider] = client
                self.library.save_account(
                    {"user_id": "456", "label": provider}, {"cookie": "fixture"}, provider
                )
        sample, fetch = self.music.downloads.transfer.sample, self.music.downloads.transfer.fetch

        async def sampled(*args, **kwargs):
            self.assertEqual(
                set(queried), set(choices), "All providers must be queried before any audio bytes"
            )
            self.file_bytes = choices[kwargs["provider"]][0]
            return await sample(*args, **kwargs)

        async def fetched(*args, **kwargs):
            if not kwargs.get("artwork"):
                full_transfers.append(kwargs["provider"])
                self.file_bytes = choices[kwargs["provider"]][0]
            return await fetch(*args, **kwargs)

        self.music.downloads.transfer.sample = sampled
        self.music.downloads.transfer.fetch = fetched
        return queried, full_transfers

    async def test_budget_blocked_channel_does_not_hide_later_highest_offer(self):
        queried, transfers = self.channels(
            {
                "netease": (self.audio["flac"], "lossless"),
                "kugou": (self.audio["mp3"], "standard"),
                "qq": (self.audio["hires"], "hires"),
            }
        )
        until = self.repo.clock() + 86400

        async def blocked(*args):
            queried.append("kugou")
            raise Deferred(until, "music_request_budget")

        self.music.connectors["kugou"].download = blocked
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(transfers, ["qq"])
        self.assertEqual(set(queried), {"netease", "qq", "kugou"})
        self.assertFalse(result["audio_routing"]["complete"])
        pending = result["audio_routing"]["deferred"]
        self.assertEqual([(r["provider"], r["retry_at"]) for r in pending], [("kugou", until)])
        self.assertEqual(self.repo.list("music_quality_check")[0]["pending_channels"], pending)
        self.assertNotIn(self.origin, str(result["audio_routing"]))

    async def test_all_channels_deferred_waits_without_terminal_failure_or_spending_retry(self):
        async def blocked(*args):
            raise Deferred(self.repo.clock() + 1800, "music_account_cooling")

        self.api.download = blocked
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "retry_wait")
        self.assertEqual(result["code"], "music_account_cooling")
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(self.audio_transfers, 0)
        self.assertFalse(result["audio_routing"]["complete"])

    async def test_probe_budget_recovers_with_one_upgrade_then_no_equal_redownload(self):
        _, transfers = self.channels(
            {"netease": (self.audio["flac"], "lossless"), "kugou": (self.audio["hires"], "hires")}
        )
        sample = self.music.downloads.transfer.sample

        async def blocked(*args, **kwargs):
            if kwargs["provider"] == "kugou":
                raise Deferred(self.repo.clock() + 3600, "music_request_budget")
            return await sample(*args, **kwargs)

        self.music.downloads.transfer.sample = blocked
        self.queue()
        first = await self.run_job()
        self.assertEqual(first["state"], "completed", first.get("code"))
        self.assertEqual(first["download_provider"], "netease")
        self.assertEqual(first["audio_routing"]["deferred"][0]["stage"], "probe")
        self.music.downloads.transfer.sample = sample
        self.queue()
        upgraded = await self.run_job()
        self.assertEqual(upgraded["actual_quality"], "hires")
        before = (self.file(upgraded).read_bytes(), self.file(upgraded).stat().st_mtime_ns)
        self.queue()
        reused = await self.run_job()
        self.assertTrue(reused["audio_reused"])
        self.assertEqual(transfers, ["netease", "kugou"])
        self.assertEqual(self.repo.job_count(), 1)
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)
        self.assertEqual(
            (self.file(reused).read_bytes(), self.file(reused).stat().st_mtime_ns), before
        )
        self.assertEqual(self.repo.list("music_quality_check")[0]["pending_channels"], [])

    async def test_download_cooldown_uses_another_compared_offer_without_failing_job(self):
        _, transfers = self.channels(
            {"netease": (self.audio["flac"], "lossless"), "kugou": (self.audio["hires"], "hires")}
        )
        acquire = self.music.downloads.acquire

        async def blocked(job, candidate, update):
            if candidate["provider"] == "kugou":
                raise Deferred(self.repo.clock() + 1800, "music_account_cooling")
            return await acquire(job, candidate, update)

        self.music.downloads.acquire = blocked
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(transfers, ["netease"])
        self.assertEqual(result["audio_routing"]["deferred"][0]["stage"], "download")
        self.assertEqual(result.get("attempts", 0), 0)

    async def test_metadata_cooldown_still_compares_other_providers(self):
        queried, transfers = self.channels(
            {
                "netease": (self.audio["flac"], "lossless"),
                "kugou": (self.audio["hires"], "hires"),
                "qq": (self.audio["mp3"], "standard"),
            }
        )

        async def blocked(*args):
            queried.append("netease")
            raise Deferred(self.repo.clock() + 300, "music_request_budget")

        self.api.enrich = blocked
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(transfers, ["kugou"])
        self.assertEqual(result["audio_routing"]["deferred"][0]["stage"], "metadata")

    async def test_later_provider_hires_beats_first_downloadable_source(self):
        queried, transfers = self.channels(
            {
                "netease": (self.audio["flac"], "lossless"),
                "kugou": (self.audio["mp3"], "observed"),
                "qq": (self.audio["hires"], "hires"),
            }
        )
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(result["download_provider"], "qq")
        self.assertEqual(transfers, ["qq"])
        self.assertEqual(len(queried), 3)
        self.assertEqual(result["audio_quality"]["level"], "hires")
        self.assertEqual(len(result["audio_routing"]["providers"]), 3)
        self.assertNotIn(self.origin, str(result["audio_routing"]))

    async def test_unknown_encrypted_candidate_is_validated_before_final_quality_choice(self):
        if not os.environ.get("TIANSHU_MUSICDECODE"):
            self.skipTest("Real decoder required")
        track = self.api.value["tracks"][0]
        encrypted = encrypted_ncm(
            self.audio["mp3"], track["title"], track["artists"], track["album"], self.image
        )
        _, transfers = self.channels(
            {
                "kugou": (encrypted, "observed"),
                "netease": (self.audio["flac"], "lossless"),
            }
        )
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(result["download_provider"], "netease")
        self.assertEqual(transfers, ["kugou", "netease"])
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)
        self.assertFalse(list(self.library_root.rglob("*.mp3")))
        candidates = result["audio_routing"]["candidates"]
        self.assertEqual(
            next(c for c in candidates if c["provider"] == "kugou")["probe"], "encrypted"
        )

    async def test_existing_low_quality_is_upgraded_then_same_quality_reuses_bytes(self):
        self.file_bytes, self.level = self.audio["mp3"], "standard"
        self.queue()
        first = await self.run_job()
        old = self.file(first)
        self.file_bytes, self.level = self.audio["hires"], "hires"
        self.queue()
        upgraded = await self.run_job()
        self.assertEqual(upgraded["state"], "completed", upgraded.get("code"))
        self.assertEqual(upgraded["actual_quality"], "hires")
        self.assertFalse(old.exists())
        path = self.file(upgraded)
        before = (path.stat().st_mtime_ns, path.read_bytes())
        _, transfers = self.channels({"netease": (self.audio["hires"], "hires")})
        self.queue()
        reused = await self.run_job()
        self.assertTrue(reused["audio_reused"])
        self.assertTrue(reused["audio_routing"]["selected"]["reused"])
        self.assertEqual(transfers, [])
        self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), before)

    async def test_legacy_lossless_label_reuses_actual_hires_without_full_download(self):
        self.file_bytes, self.level = self.audio["hires"], "hires"
        self.queue()
        first = await self.run_job()
        self.assertEqual(first["state"], "completed")
        path = self.file(first)
        before = (path.stat().st_mtime_ns, path.read_bytes())
        # Older archives recorded the provider's lossless tier even for 24-bit audio.
        artifact = first["audio_receipt"]
        self.repo.put(
            "music_library_file",
            artifact["recording_id"],
            {**artifact, "quality": {**artifact["quality"], "level": "lossless"}},
        )
        queried, transfers = self.channels({"netease": (self.audio["hires"], "lossless")})
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertTrue(queried)
        self.assertEqual(transfers, [], "A stale tier must not cause an equal audio download")
        self.assertTrue(result["audio_reused"])
        self.assertEqual(result["actual_quality"], "hires")
        self.assertEqual(self.file(result), path)
        self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), before)
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)

    async def test_quality_probe_is_bounded_when_server_ignores_range(self):
        self.file_bytes = b"x" * (2 * 1024**2)
        path = self.root / "probe.part"
        await self.music.downloads.transfer.sample(self.origin + "/file", path, provider="qq")
        self.assertEqual(path.stat().st_size, 1024**2)
        self.assertEqual(self.received[-1]["Range"], "bytes=0-1048575")
        self.assertNotIn("Cookie", self.received[-1])


if __name__ == "__main__":
    unittest.main()
