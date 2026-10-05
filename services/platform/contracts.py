"""Read the published 1.0.0 documents in place; never fetch remote schemas."""

import hashlib
import json
import math
import types
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

MANIFEST_SHA256 = "90697e6ecbb587d3db8c8e4682f7f8f43a8b1a98f70d8835c8282b03828d2d3a"
SOURCE_SHA256 = "6d5c417c2e407aaf055151c1a3639d3b4e2ce4ad7da999f95273b6fcbf487370"
NATIVE_SHA256 = "832abdbfbbb49d71bffc0aabdd816f4de92d892680f26cc5f05402e397d34262"
WEB_SHA256 = "3ccb44c13f5969ce4cd279c51d14f58d95c9cd4e75ca85cd8d6af286b3db0417"
LIFE_SHA256 = "7be7507d58f897a739b269de3c096ba948c92b25266888a91fa50130d342c551"
RUNTIME_PACKAGES = {
    "life-runtime/v2": "85aff438f91cb96876e259204b5a198e2fedaead56db64a2265aa520a59ea7e3",
    "image-backend/v1": "62e71dd2f42fb5b1376c439c362a555619da41cb7f7ae26d07acc1182b80ffbd",
    "bot-delivery/v2": "edec27d83b8b9427656096d45818d9db3665d8d7bd82cc796805e21ba35b757b",
    "memory-context/v1": "d44a23ac674d5fd3f8e9325c80884dc1426305055b873be1da239f126056e570",
    "knowledge-content/v1": "75d210454102af5af505ec1f72d69cc7dfd344550f7cf90e70125c082fdf4cd7",
}


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
        self.root = root.parents[1]
        self.loaded_packages = set()

    def load_runtime(self, package):
        """Load each formally published extension once; no candidate or remote schemas."""
        if package in self.loaded_packages:
            return
        root = self.root / package
        manifest_raw = (root / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
        require(
            hashlib.sha256(manifest_raw).hexdigest() == RUNTIME_PACKAGES[package],
            "dependency_unavailable",
            503,
        )
        manifest = loads(manifest_raw)
        for dependency, expected in manifest.get("dependencies", {}).items():
            dependency = {"text-dialogue": "text-dialogue/v1", "source-sync": "source-sync/v1"}.get(dependency, dependency)
            raw = (self.root / dependency / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
            require(hashlib.sha256(raw).hexdigest() == expected, "dependency_unavailable", 503)
            if dependency in RUNTIME_PACKAGES:
                self.load_runtime(dependency)
        for name, expected in manifest["sha256"].items():
            raw = (root / name).read_bytes().replace(b"\r\n", b"\n")
            require(hashlib.sha256(raw).hexdigest() == expected, "dependency_unavailable", 503)
            if name.startswith("schemas/") or name == "schema.json":
                schema = loads(raw)
                self.registry = self.registry.with_resource(
                    schema["$id"], Resource.from_contents(schema)
                )
            elif name == "dependencies/platform-credential.json":
                self.image_credential_schema = loads(raw)
        self.loaded_packages.add(package)

    def check_image_credential(self, kind, document):
        self.load_runtime("life-runtime/v2")
        try:
            Draft202012Validator(
                self.image_credential_schema[kind], format_checker=FormatChecker()
            ).validate(document)
        except ValidationError:
            raise Fault() from None

    def load_life(self, directory):
        root = Path(directory)
        raw = (root / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
        require(hashlib.sha256(raw).hexdigest() == LIFE_SHA256, "dependency_unavailable", 503)
        manifest = loads(raw)
        for name, expected in manifest["sha256"].items():
            raw = (root / name).read_bytes().replace(b"\r\n", b"\n")
            require(hashlib.sha256(raw).hexdigest() == expected, "dependency_unavailable", 503)
            if name == "schemas/life.json":
                schema = loads(raw)
                self.registry = self.registry.with_resource(
                    schema["$id"], Resource.from_contents(schema)
                )

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
        if family == "life-read":
            package, file = "life-read/v1", "life"
        if family in {"life-runtime", "bot-delivery", "memory-context", "knowledge-content", "image-backend"}:
            package, file = {
                "life-runtime": ("life-runtime/v2", "life"),
                "bot-delivery": ("bot-delivery/v2", "delivery"),
                "memory-context": ("memory-context/v1", "schema"),
                "knowledge-content": ("knowledge-content/v1", "schema"),
                "image-backend": ("image-backend/v1", "image-backend"),
            }[family]
            self.load_runtime(package)
        try:
            Draft202012Validator(
                {"$ref": f"https://contracts.tianshu.invalid/{package}/{file}.json#/$defs/{kind}"},
                registry=self.registry,
                format_checker=FormatChecker(),
            ).validate(document)
        except (ValidationError, RecursionError):
            raise Fault() from None
