"""Pinned shared DTO input, not a product-owned schema copy."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..contracts import Fault

SCHEMA_HASH = "e96397bac2b6ad8ff9d23c023d7d3c5ba0701734b27053a05b9d0f65a7ff8ee6"


class Contract:
    def __init__(self, path):
        raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw).hexdigest() != SCHEMA_HASH:
            raise Fault("invalid_input", 400)
        self.schema = json.loads(raw)

    def check(self, kind, value, *, upstream=False):
        try:
            Draft202012Validator({**self.schema, "$ref": "#/$defs/" + kind}).validate(value)
        except ValidationError:
            raise Fault(
                "invalid_upstream" if upstream else "invalid_input", 502 if upstream else 400
            ) from None
