"""Immutable module-local payloads; SQL retains business state and exact digests.

FileStore fsyncs and seals bytes before a SQL reference can commit. No cache TTL
or background deletion owns these recovery files.
"""

import base64
import copy
from pathlib import Path

from tianshu_storage import FileStore, StorageError


class MediaPayloads:
    def __init__(self, database_path):
        self.books = FileStore(Path(database_path).with_suffix(".book-content") / "chapters")
        self.lyrics = FileStore(Path(database_path).with_suffix(".lyric-content"))

    def store(self, kind, value):
        result = copy.deepcopy(value)
        field = {"book_chapter": "content", "book_audio_job": "text"}.get(kind)
        if field and field in result:
            result[field + "_ref"] = self.books.put_text(result.pop(field)).as_dict()
        if kind == "music_lyrics":
            for item in (result.get("pending") or {}).get("writes", []):
                if "content" in item:
                    item["content_ref"] = self.lyrics.put_text(item.pop("content")).as_dict()
        return result

    def read(self, kind, value):
        if value is None:
            return None
        result = copy.deepcopy(value)
        field = {"book_chapter": "content", "book_audio_job": "text"}.get(kind)
        if field and field + "_ref" in result:
            result[field] = self.books.read_text(result.pop(field + "_ref"))
        if kind == "music_lyrics":
            for item in (result.get("pending") or {}).get("writes", []):
                if "content_ref" in item:
                    item["content"] = self.lyrics.read_text(item.pop("content_ref"))
        return result


class DeliveryPayloads:
    def __init__(self, database_path):
        self.files = FileStore(str(database_path) + ".payloads")

    def store(self, wire):
        result = copy.deepcopy(wire)
        for item in result.get("media", []):
            if "data" in item:
                encoded = item.pop("data")
                raw = base64.b64decode(encoded, validate=True)
                item["payload_ref"] = self.files.put_bytes(
                    raw, media_type=item["media_type"]
                ).as_dict()
                # Preserve every previously accepted wire spelling, including noncanonical
                # padding bits, without retaining a base64 body in the SQL request.
                if base64.b64encode(raw).decode("ascii") != encoded:
                    item["encoded_ref"] = self.files.put_text(encoded).as_dict()
        return result

    def read(self, wire):
        result = copy.deepcopy(wire)
        for item in result.get("media", []):
            if "payload_ref" in item:
                raw = self.files.read_bytes(item.pop("payload_ref"))
                encoded = base64.b64encode(raw).decode("ascii")
                if "encoded_ref" in item:
                    encoded = self.files.read_text(item.pop("encoded_ref"))
                    if base64.b64decode(encoded, validate=True) != raw:
                        raise StorageError("delivery_payload_encoding_mismatch")
                item["data"] = encoded
        return result
