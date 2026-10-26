"""Pinned local candidate contracts, separate from published user protocols."""

import hashlib

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from .contracts import Fault, canonical, loads, require


class CandidateProtocol:
    def __init__(self, root, package, manifest, *, registry=None, response_limit=1024 * 1024):
        self.root = root / package
        self.manifest = manifest
        self.response_limit = response_limit
        self.schema = None
        self.registry = registry or Registry()

    def check(self, kind, value):
        if self.schema is None:
            try:
                raw = (self.root / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
                require(
                    hashlib.sha256(raw).hexdigest() == self.manifest, "dependency_unavailable", 503
                )
                for name, expected in loads(raw)["sha256"].items():
                    data = (self.root / name).read_bytes().replace(b"\r\n", b"\n")
                    require(
                        hashlib.sha256(data).hexdigest() == expected, "dependency_unavailable", 503
                    )
                schema = loads((self.root / "schema.json").read_bytes())
                self.registry = self.registry.with_resource(
                    schema["$id"], Resource.from_contents(schema)
                )
                self.schema = schema
            except (OSError, ValueError, KeyError):
                raise Fault("dependency_unavailable", 503) from None
        try:
            Draft202012Validator(
                {"$ref": self.schema["$id"] + "#/$defs/" + kind},
                registry=self.registry,
                format_checker=FormatChecker(),
            ).validate(value)
        except (ValidationError, RecursionError):
            raise Fault("invalid_input", 400) from None

    def response(self, kind, value):
        self.check(kind, value)
        require(len(canonical(value).encode()) <= self.response_limit, "capacity_exceeded", 413)
        return value
