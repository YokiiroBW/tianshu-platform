"""Speech configuration; credentials are sealed by the existing media account store."""

import re
from urllib.parse import urlsplit

from ....contracts import digest, require

FISH_MODELS = ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3")
EDGE_VOICES = (
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声"),
    ("zh-CN-YunxiNeural", "云希 · 男声"),
    ("zh-CN-YunjianNeural", "云健 · 男声"),
    ("zh-CN-YunyangNeural", "云扬 · 男声"),
    ("zh-TW-HsiaoChenNeural", "晓臻 · 女声"),
)


def defaults(provider):
    return {
        "provider": provider,
        "endpoint": "https://api.fish.audio" if provider == "fish" else "",
        "model": "s2.1-pro" if provider == "fish" else "edge" if provider == "edge" else "",
        "voice": "zh-CN-XiaoxiaoNeural" if provider == "edge" else "",
        "speed": 1.0,
        "pitch": 0,
        "daily_characters": 200000 if provider == "edge" else 20000,
        "revision": 0,
    }


def profile(config):
    return {
        **{k: config[k] for k in ("provider", "endpoint", "model", "voice", "speed", "pitch")},
        "format": "mp3",
        "segment_version": 1,
    }


def profile_id(config):
    return digest(profile(config))[:24]


class AudioConfig:
    def __init__(self, books):
        self.books, self.repo = books, books.repo

    def get(self, provider):
        require(provider in {"edge", "fish", "local"}, "invalid_input", 400)
        return self.repo.get("book_audio_config", provider) or defaults(provider)

    @staticmethod
    def public(config):
        return {
            **{k: v for k, v in config.items() if k != "secret"},
            "has_key": bool(config.get("secret")),
            "configured": config["provider"] == "edge"
            or bool(
                config["endpoint"]
                and config["model"]
                and config["voice"]
                and (config["provider"] == "local" or config.get("secret"))
            ),
        }

    def save(self, body, db):
        provider = body.get("provider")
        require(provider in {"edge", "fish", "local"}, "invalid_input", 400)
        old = self.repo._document(db, "book_audio_config", provider) or defaults(provider)
        value = {**old}
        for key in ("endpoint", "model", "voice", "speed", "pitch", "daily_characters"):
            if key in body:
                value[key] = body[key]
        for key in ("endpoint", "model", "voice"):
            require(isinstance(value[key], str) and len(value[key]) <= 1024, "invalid_input", 400)
            value[key] = value[key].strip().rstrip("/") if key == "endpoint" else value[key].strip()
        require(
            type(value["speed"]) in (float, int) and 0.5 <= value["speed"] <= 2,
            "invalid_input",
            400,
        )
        require(type(value["pitch"]) is int and -50 <= value["pitch"] <= 50, "invalid_input", 400)
        require(
            type(value["daily_characters"]) is int and 0 <= value["daily_characters"] <= 10000000,
            "invalid_input",
            400,
        )
        if provider == "edge":
            require(
                re.fullmatch(r"[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural", value["voice"]),
                "book_audio_voice_invalid",
                400,
            )
            value.update(endpoint="", model="edge")
        elif provider == "fish":
            require(
                value["endpoint"] == "https://api.fish.audio" and value["model"] in FISH_MODELS,
                "book_audio_model_invalid",
                400,
            )
            require(
                not value["voice"] or re.fullmatch(r"[a-fA-F0-9]{32}", value["voice"]),
                "book_audio_voice_invalid",
                400,
            )
            value["pitch"] = 0
        else:
            endpoint = urlsplit(value["endpoint"])
            require(
                not value["endpoint"]
                or endpoint.scheme in {"http", "https"}
                and endpoint.hostname
                and not endpoint.username
                and not endpoint.password
                and not endpoint.query
                and not endpoint.fragment,
                "book_audio_endpoint_invalid",
                400,
            )
            value["pitch"] = 0
        if old["endpoint"] != value["endpoint"]:
            value.pop("secret", None)
        key = body.get("api_key")
        require(
            key is None
            or isinstance(key, str)
            and len(key) <= 4096
            and not any(ord(c) < 32 for c in key),
            "invalid_input",
            400,
        )
        if key:
            value["secret"] = self.books.media.accounts.seal("book-audio:" + provider, key)
        if body.get("clear_key") is True:
            value.pop("secret", None)
        result = self.repo.put(
            "book_audio_config", provider, value, body.get("expected_revision"), db=db
        )
        return {"config": self.public(result)}

    def credentials(self, selected):
        current = self.get(selected["provider"])
        require(current["endpoint"] == selected["endpoint"], "book_audio_endpoint_changed", 409)
        require(self.public(current)["configured"], "book_audio_not_configured", 409)
        return (
            self.books.media.accounts.unseal(
                "book-audio:" + selected["provider"], current["secret"]
            )
            if current.get("secret")
            else ""
        )
