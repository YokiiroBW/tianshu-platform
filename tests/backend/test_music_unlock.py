"""Real synthetic encrypted audio through acquisition, tagging, shared storage and fallback."""

import asyncio
import copy
import hashlib
import json
import os
import re
import struct
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.flac import FLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

import test_music_downloads as fixtures
from test_music_library import encrypted_ncm
from services.platform.media.music.decoding import KGM_MAGIC


def encrypted_qmc(audio):
    # Fixture encoder uses the pinned upstream's published static table.
    source = (
        Path(__file__).resolve().parents[2]
        / "tools/musicdecode/third_party/unlock_cli/algo/qmc/cipher_static.go"
    )
    table = source.read_text().split("var staticCipherBox")[1]
    table = "\n".join(line.split("//")[0] for line in table.splitlines())
    mask = bytes(int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{2})", table))
    assert len(mask) == 256
    result = bytearray(audio)
    for index, value in enumerate(audio):
        offset = index % 0x7FFF if index > 0x7FFF else index
        result[index] = value ^ mask[(offset * offset + 27) & 255]
    return bytes(result)


def key_required_kgm():
    return KGM_MAGIC + struct.pack("<III", 80, 5, 1) + bytes(52) + b"synthetic"


class MusicUnlockTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MusicDownloadTests.asyncSetUp
    fixture_url = fixtures.MusicDownloadTests.fixture_url
    value = fixtures.MusicDownloadTests.value
    subscribe = fixtures.MusicDownloadTests.subscribe
    queue = fixtures.MusicDownloadTests.queue
    run_job = fixtures.MusicDownloadTests.run_job
    file = fixtures.MusicDownloadTests.file

    @classmethod
    def setUpClass(cls):
        fixtures.MusicDownloadTests.setUpClass.__func__(cls)
        if not os.environ.get("TIANSHU_MUSICDECODE"):
            raise unittest.SkipTest("Real pinned decoder executable required")
        for extension, codec in (("ogg", "libvorbis"), ("opus", "libopus")):
            target = Path(cls.fixture.name) / ("tone." + extension)
            subprocess.run(
                [
                    cls.ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=3",
                    "-c:a",
                    codec,
                    str(target),
                ],
                check=True,
            )
            cls.audio[extension] = target.read_bytes()

    def ncm(self):
        track = self.api.value["tracks"][0]
        return encrypted_ncm(
            self.audio["flac"], track["title"], track["artists"], track["album"], self.image
        )

    async def test_download_detects_ncm_without_suffix_and_import_reuses_one_file(self):
        original = self.file_bytes = self.ncm()
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(result["audio_unlock"]["state"], "decoded")
        self.assertEqual(result["audio_unlock"]["format"], "ncm")
        self.assertEqual(
            result["audio_unlock"]["input_sha256"], hashlib.sha256(original).hexdigest()
        )
        self.assertEqual(result["audio_receipt"]["unlock"], result["audio_unlock"])
        sidecar = json.loads(self.file(result, "metadata").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["unlock"], result["audio_unlock"])
        self.assertEqual(sidecar["provider"], "netease")
        self.assertEqual(FLAC(self.file(result))["title"], [self.api.value["tracks"][0]["title"]])
        directory = self.library_root / ".incoming"
        directory.mkdir(exist_ok=True)
        source = directory / "same.ncm"
        source.write_bytes(original)
        # Queue only this original; unrelated playlist jobs are not part of this check.
        for job in self.repo.jobs()[0]:
            if job["state"] == "queued":
                self.repo.update_job(
                    job["job_id"], {"state": "cancelled", "code": "user_cancelled"}
                )
        from services.platform.media.music.imports import stamp

        self.music.imports.enqueue(item={"path": ".incoming/same.ncm", **stamp(source)})
        imported = await self.run_job()
        self.assertTrue(imported["audio_reused"])
        self.assertEqual(self.file(imported), self.file(result))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)
        self.assertEqual(self.api.download_calls, 1)

    async def test_qmc_download_with_format_hint_preserves_ogg_and_tags(self):
        self.file_bytes = encrypted_qmc(self.audio["ogg"])
        self.level = "standard"
        original = self.api.download

        async def offered(*args):
            return {**await original(*args), "format": "qmcogg"}

        self.api.download = offered
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(result["audio_unlock"]["format"], "qmcogg")
        self.assertEqual(result["audio_quality"]["codec"], "vorbis")
        self.assertFalse(result["audio_quality"]["lossless"])
        audio = OggVorbis(self.file(result))
        self.assertEqual(audio["title"], [self.api.value["tracks"][0]["title"]])
        self.assertTrue(audio["metadata_block_picture"])
        self.assertTrue(audio["lyrics"])

    async def test_plain_opus_bypasses_decoder_despite_encrypted_filename_hint(self):
        self.file_bytes, self.level = self.audio["opus"], "standard"
        original = self.api.download

        async def offered(*args):
            return {**await original(*args), "format": "ncm"}

        self.api.download = offered
        self.queue()
        with patch(
            "services.platform.media.music.decoding.tools.invoke",
            side_effect=AssertionError("Plain audio must bypass decoder"),
        ):
            result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(result["audio_unlock"], {"state": "not_needed"})
        self.assertEqual(result["audio_quality"]["codec"], "opus")
        self.assertEqual(
            OggOpus(self.file(result))["artist"], self.api.value["tracks"][0]["artists"]
        )

    async def test_missing_client_key_has_durable_reason_without_publishing(self):
        self.file_bytes = key_required_kgm()
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["code"], "music_decode_key_required")
        self.assertEqual(result["audio_unlock"]["state"], "key_required")
        self.assertEqual(result["audio_attempts"][0]["code"], "music_decode_key_required")
        self.assertFalse(list(self.library_root.rglob("*.flac")))
        retained = list((self.root / "staging").rglob("unlock-input.kgm"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), self.file_bytes)
        self.assertNotIn(self.origin, json.dumps(result["audio_attempts"]))

    async def test_unlock_failure_falls_back_to_another_entitled_source(self):
        encrypted = key_required_kgm()
        plain = self.audio["flac"]
        self.file_bytes = encrypted
        original = copy.deepcopy(self.api.value["tracks"][0])
        client = fixtures.SyntheticNetease()

        async def search(query, cookie):
            return [{**original, "source_id": "qq-alternative", "provider": "qq"}]

        async def download(*args):
            return {
                "url": self.origin + "/file",
                "level": "lossless",
                "size": len(plain),
                "md5": hashlib.md5(plain).hexdigest(),
            }

        client.search, client.download = search, download
        client.lyrics = self.api.lyrics
        self.music.connectors["qq"] = client
        self.library.save_account({"user_id": "456", "label": "QQ"}, {"cookie": "fixture"}, "qq")
        fetch = self.music.downloads.transfer.fetch

        async def transfer(*args, **kwargs):
            self.file_bytes = encrypted if kwargs.get("provider") == "netease" else plain
            return await fetch(*args, **kwargs)

        self.music.downloads.transfer.fetch = transfer
        self.queue()
        result = await self.run_job()
        self.assertEqual(result["state"], "completed", result.get("code"))
        self.assertEqual(result["download_provider"], "qq")
        self.assertEqual(result["audio_unlock"]["state"], "not_needed")
        self.assertEqual(result["audio_attempts"][0]["unlock"]["state"], "key_required")
        self.assertEqual(len(list(self.library_root.rglob("*.flac"))), 1)

    async def test_cancellation_during_unlock_never_publishes(self):
        self.file_bytes = self.ncm()
        entered = asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.queue()
        with patch("services.platform.media.music.decoding.tools.invoke", waiting):
            running = asyncio.create_task(self.run_job())
            await entered.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertFalse(list(self.library_root.rglob("*.flac")))
        self.assertEqual(len(list((self.root / "staging").rglob("unlock-input.ncm"))), 1)
        current = next(job for job in self.repo.jobs()[0] if job.get("audio_unlock"))
        self.assertEqual(current["audio_unlock"]["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
