"""Load and verify the frozen `contracts/diagnostics/v1` package the deployment provides.

The package is an input, not a source dependency: the deployment hands over the directory, this
module checks it against the pinned digest and against its own per-file manifest, and nothing is
copied, rewritten or imported from another repository. That is what lets a platform build prove
which exact contract bytes it was reviewed against.

The published package registers its per-file digests over the bytes as they were published, so
verification here is over those same bytes. A copy whose line endings were rewritten is a
different artifact and is refused rather than quietly normalised into acceptance.
"""

import hashlib
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

# The reviewed digest of the frozen package's manifest, recorded by the coordinator. It is pinned
# here in the same shape as the product's other published-contract digests; a manifest that does
# not hash to this value stops assembly rather than being trusted.
MANIFEST_SHA256 = "5d89f7a21637fd57cea4a236e17f8d8c4917799497ff44f87ee68ea614d4805f"
CONTRACT_VERSION = "1.0.0"
CONTRACT_STATUS = "development_frozen_pending_joint_acceptance"
CONTRACT_FILES = ("README.md", "event.schema.json", "examples.json", "negative-examples.json")

# Deployment inputs. Every one is explicit: there is no development-workspace fallback, because a
# production readiness answer must never depend on where a checkout happened to sit.
CONTRACT_DIRECTORY_ENV = "TIANSHU_DIAGNOSTICS_CONTRACT_DIR"
LOG_DIRECTORY_ENV = "TIANSHU_LOG_DIR"
READY_TOKEN_ENV = "TIANSHU_DIAGNOSTICS_TOKEN"
MANIFEST_SHA256_ENV = "TIANSHU_DIAGNOSTICS_MANIFEST_SHA256"


class ContractProblem(Exception):
    """A frozen input that is missing, unreadable, tampered with or self-inconsistent."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


class ContractPackage:
    """A verified frozen package: its digests, its schema and its two example sets."""

    def __init__(self, directory, manifest_sha256, manifest, schema, examples, negative):
        self.directory = str(directory)
        self.manifest_sha256 = manifest_sha256
        self.version = manifest["version"]
        self.status = manifest["status"]
        self.files = dict(manifest["files"])
        self.schema = schema
        self.examples = examples
        self.negative_examples = negative
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def validate(self, record):
        """Whether one record satisfies the frozen schema exactly."""
        try:
            self._validator.validate(record)
        except (ValidationError, RecursionError):
            return False
        return True

    def check_examples(self):
        """The package must reject its own negative examples.

        Checking both sets at load time is what keeps the schema meaningful: a schema edited into
        acceptance would still hash consistently with its manifest, so the examples are the part
        that proves the frozen semantics were not silently weakened.
        """
        if not self.examples or not all(self.validate(item) for item in self.examples):
            raise ContractProblem("contract_examples_rejected")
        if not self.negative_examples:
            raise ContractProblem("contract_negative_examples_missing")
        if any(self.validate(item) for item in self.negative_examples):
            raise ContractProblem("contract_negative_examples_accepted")


def load_contract(directory, expected_manifest_sha256=MANIFEST_SHA256):
    """Read the package from `directory` and refuse anything that is not the reviewed artifact."""
    if not directory:
        raise ContractProblem("contract_directory_not_configured")
    root = Path(directory)
    if not root.is_absolute():
        raise ContractProblem("contract_directory_not_absolute")
    if not root.is_dir():
        raise ContractProblem("contract_directory_missing")
    return _load(root, expected_manifest_sha256 or MANIFEST_SHA256)


def _load(root, expected_manifest_sha256):
    try:
        raw = (root / "manifest.json").read_bytes()
    except OSError:
        raise ContractProblem("contract_manifest_unreadable") from None
    if _sha256(raw) != expected_manifest_sha256:
        raise ContractProblem("contract_manifest_hash_mismatch")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ContractProblem("contract_manifest_invalid") from None
    if not isinstance(manifest, dict):
        raise ContractProblem("contract_manifest_invalid")
    if manifest.get("version") != CONTRACT_VERSION or manifest.get("status") != CONTRACT_STATUS:
        raise ContractProblem("contract_manifest_version_mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(CONTRACT_FILES):
        raise ContractProblem("contract_manifest_files_mismatch")
    contents = {}
    for name in CONTRACT_FILES:
        try:
            contents[name] = (root / name).read_bytes()
        except OSError:
            raise ContractProblem("contract_file_unreadable") from None
        if _sha256(contents[name]) != files[name]:
            raise ContractProblem("contract_file_hash_mismatch")
    try:
        schema = json.loads(contents["event.schema.json"].decode("utf-8"))
        examples = json.loads(contents["examples.json"].decode("utf-8"))
        negative = json.loads(contents["negative-examples.json"].decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ContractProblem("contract_file_invalid") from None
    if (
        not isinstance(schema, dict)
        or not isinstance(examples, list)
        or not isinstance(negative, list)
    ):
        raise ContractProblem("contract_file_invalid")
    if schema.get("additionalProperties") is not False:
        # The closed-record guarantee is the whole point of the package: an open schema would let
        # a free-form key through, so it is checked here and not only relied on downstream.
        raise ContractProblem("contract_schema_not_closed")
    package = ContractPackage(root, expected_manifest_sha256, manifest, schema, examples, negative)
    package.check_examples()
    return package


def resolve_contract_directory(settings):
    """The deployment's explicit package location, from settings first and the environment second."""
    value = (settings or {}).get("diagnostics") or {}
    directory = value.get("contract_directory") or os.environ.get(CONTRACT_DIRECTORY_ENV)
    return directory or None


def resolve_manifest_sha256(settings):
    """The reviewed manifest digest, overridable only by an explicit deployment pin."""
    value = (settings or {}).get("diagnostics") or {}
    pinned = value.get("expected_manifest_sha256") or os.environ.get(MANIFEST_SHA256_ENV)
    return pinned or MANIFEST_SHA256


def resolve_log_directory(settings):
    value = (settings or {}).get("diagnostics") or {}
    directory = value.get("log_directory") or os.environ.get(LOG_DIRECTORY_ENV)
    return directory or None


def resolve_log_directory_bytes(settings):
    value = (settings or {}).get("diagnostics") or {}
    return value.get("log_directory_bytes")


def resolve_ready_token_env(settings):
    """The variable the readiness probe reads, never a business or administrator credential."""
    value = (settings or {}).get("diagnostics") or {}
    return value.get("ready_token_env") or READY_TOKEN_ENV


def parse_diagnostics_settings(settings):
    """Validate the optional `diagnostics` section, without resolving any secret value."""
    section = (settings or {}).get("diagnostics")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ContractProblem("diagnostics_settings_invalid")
    allowed = {
        "contract_directory",
        "expected_manifest_sha256",
        "log_directory",
        "log_directory_bytes",
        "ready_token_env",
    }
    if set(section) - allowed:
        raise ContractProblem("diagnostics_settings_invalid")
    for key in ("contract_directory", "log_directory"):
        value = section.get(key)
        if value is not None and (not isinstance(value, str) or not Path(value).is_absolute()):
            raise ContractProblem("diagnostics_settings_invalid")
    size = section.get("log_directory_bytes")
    if size is not None and (type(size) is not int or size <= 0):
        raise ContractProblem("diagnostics_settings_invalid")
    token_env = section.get("ready_token_env")
    if token_env is not None and (
        not isinstance(token_env, str) or not token_env.isascii() or not token_env
    ):
        raise ContractProblem("diagnostics_settings_invalid")
    digest = section.get("expected_manifest_sha256")
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
        raise ContractProblem("diagnostics_settings_invalid")
    return section


def verify_or_none(settings):
    """Load the package when one is configured; a missing configuration is not an error here.

    Readiness is where an unconfigured contract becomes a red check, because a running service
    without one is a deployment fault worth reporting, while an explicit development run without
    one is simply a development run.
    """
    directory = resolve_contract_directory(settings)
    if directory is None:
        return None, None
    try:
        return load_contract(directory, resolve_manifest_sha256(settings)), None
    except ContractProblem as problem:
        return None, problem.code
