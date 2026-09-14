"""Read the published 1.0.0 documents in place; never fetch remote schemas."""

import hashlib
import json
import math
import types
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

MANIFEST_SHA256 = "81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1"
SOURCE_SHA256 = "178d0ce66210bdfad4cfb85d8b5f0905b0b67f834e2a530efe5636ff0373633d"
NATIVE_SHA256 = "52711a71de56dbceebd1d5d96b2baf59a2d9551168029972d59111480f815141"
WEB_SHA256 = "e493a1b5d0f4cec8d55995553faf84042f4c33a59365d15423e57f4dc70a6c09"


class Fault(Exception):
    def __init__(self, code="invalid_input", status=400):
        self.code, self.status = code, status
        super().__init__(code)


def require(condition, code="forbidden", status=403):
    if not condition:
        raise Fault(code, status)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
        return number

    try:
        return json.loads(
            raw.decode("utf-8") if isinstance(raw, bytes) else raw,
            object_pairs_hook=pairs,
            parse_constant=finite,
            parse_float=finite,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise Fault() from None


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class Contracts:
    def __init__(self, directory, source_directory=None):
        root = Path(directory).resolve()

        def read(path, expected):
            raw = path.read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("published contract hash mismatch")
            return loads(raw)

        manifest = read(root / "manifest.json", MANIFEST_SHA256)
        resources = []
        for family in ("common", "model", "conversation", "identity-memory", "web"):
            schema = read(
                root / "schemas" / f"{family}.json",
                manifest["sha256"][f"text-dialogue/v1/schemas/{family}.json"],
            )
            resources.append((schema["$id"], Resource.from_contents(schema)))
        source = Path(source_directory) if source_directory else root.parents[1] / "source-sync/v1"
        source_manifest = read(source / "manifest.json", SOURCE_SHA256)
        rules_raw = None
        # Verify every published byte before loading even the pure relationship helpers.
        for path, expected in source_manifest["sha256"].items():
            raw = (source / path).read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("published source contract hash mismatch")
            if path.startswith("schemas/"):
                schema = loads(raw)
                resources.append((schema["$id"], Resource.from_contents(schema)))
            if path == "rules.py":
                rules_raw = raw
        for dependency in source_manifest["dependencies"]:
            dep_root = root.parents[1] / dependency["package"]
            dep = read(dep_root / "manifest.json", dependency["manifest_sha256"])
            if dependency["package"] == "profile-memory/v1":
                for path, expected in dep["sha256"].items():
                    raw = (dep_root / path).read_bytes().replace(b"\r\n", b"\n")
                    if hashlib.sha256(raw).hexdigest() != expected:
                        raise ValueError("published dependency hash mismatch")
                    if path.startswith("schemas/"):
                        schema = loads(raw)
                        resources.append((schema["$id"], Resource.from_contents(schema)))
        native = root.parents[1] / "model-protocol/v1"
        native_manifest = read(native / "manifest.json", NATIVE_SHA256)
        for path, expected in native_manifest["sha256"].items():
            raw = (native / path).read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("published model contract hash mismatch")
            if path.startswith("schemas/"):
                schema = loads(raw)
                resources.append((schema["$id"], Resource.from_contents(schema)))
        for dependency in native_manifest["dependencies"]:
            if (
                manifest["sha256"].get(dependency["package"] + "/" + dependency["path"])
                != (dependency["sha256"])
            ):
                raise ValueError("published model contract dependency mismatch")
        self.rules = types.ModuleType("published_source_sync_rules")
        exec(
            compile(rules_raw, str(source / "rules.py"), "exec"),
            self.rules.__dict__,
        )
        self.registry = Registry().with_resources(resources)

    def load_web(self, directory):
        root = Path(directory)
        raw = (root / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
        require(hashlib.sha256(raw).hexdigest() == WEB_SHA256, "dependency_unavailable", 503)
        manifest = loads(raw)
        for name, expected in manifest["sha256"].items():
            raw = (root / name).read_bytes().replace(b"\r\n", b"\n")
            require(hashlib.sha256(raw).hexdigest() == expected, "dependency_unavailable", 503)
            if name == "schema.json":
                schema = loads(raw)
                self.registry = self.registry.with_resource(
                    schema["$id"], Resource.from_contents(schema)
                )

    def check(self, name, document):
        family, kind = name.split("#")
        package, file = "text-dialogue/v1", family
        if family in {"sources", "shared", "workflow"}:
            package = "source-sync/v1"
        if family == "web-conversation":
            package = "web-conversation/v1"
        if family == "model-protocol":
            package, file = "model-protocol/v1", "model"
        try:
            Draft202012Validator(
                {"$ref": f"https://contracts.tianshu.invalid/{package}/{file}.json#/$defs/{kind}"},
                registry=self.registry,
                format_checker=FormatChecker(),
            ).validate(document)
        except (ValidationError, RecursionError):
            raise Fault() from None
