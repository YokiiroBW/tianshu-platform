"""The optional, pinned participation namespace; original wires remain unchanged."""

from ..candidate_protocol import CandidateProtocol
from ..contracts import digest

PACKAGE = "observation-participation/candidate-v1"
MANIFEST = "347b60a1dde6967adf559597e5bd7dfaa948f5b268d158d04ea3e3ad78b7c826"
PREFIX = "/internal/v1/observation-participation/"
PATHS = {PREFIX + name for name in ("candidates", "decision", "status")}
WINDOW_SECONDS = 60


def protocol(contracts):
    return CandidateProtocol(contracts.root, PACKAGE, MANIFEST, response_limit=65536)


def candidate_ref(value):
    return {
        "candidate_id": value["candidate_id"],
        "actor_id": value["actor_id"],
        "candidate_digest": digest(value),
    }


def decision_ref(value):
    return {
        "decision_id": value["decision_id"],
        "revision": value["revision"],
        "decision_digest": digest(value),
    }


def registration_ref(value):
    return {"registration_id": value["registration_id"], "registration_digest": digest(value)}
