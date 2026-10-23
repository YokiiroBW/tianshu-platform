"""Episode proof issuance reuses real source/identity bindings; no Memory target fiction."""

import copy
import unittest

from fixtures import bearer
import test_c4_proofs_credentials as legacy

from services.platform.contracts import canonical
from services.platform.memory_proofs import ISSUE_V2, VERIFY, operation_digest


class EpisodeProofHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = legacy.ProofCredentialHTTPTests.asyncSetUp
    post = legacy.ProofCredentialHTTPTests.post
    source_and_proposal = legacy.ProofCredentialHTTPTests.source_and_proposal

    def episode_request(self):
        physical, request = self.source_and_proposal()
        request["schema_version"] = 2
        proposal = request["proposal"]
        proposal["kind"] = "forget_episode"
        proposal["target"] = dict(episode_ref="episode:memory-owned-target", episode_version=1)
        del proposal["units"]
        return physical, request

    def verification(self, request, issued):
        return dict(
            schema_version=1,
            request_id="verify:episode",
            origin=request["origin"],
            proof_ref=issued["proof_ref"],
            purpose="revision",
            operation_digest=issued["operation_digest"],
        )

    async def test_v2_issue_replays_one_proof_and_old_verify_binds_complete_target(self):
        physical, request = self.episode_request()
        status, issued = await self.post(ISSUE_V2, request)
        self.assertEqual(200, status, issued)
        self.assertEqual(operation_digest(request["proposal"]), issued["operation_digest"])
        self.assertNotIn(physical["parts"][0]["text"], canonical(issued))
        self.assertEqual((200, issued), await self.post(ISSUE_V2, request))
        status, result = await self.post(VERIFY, self.verification(request, issued), "MEMORY")
        self.assertEqual(200, status, result)
        self.assertEqual([physical["author"]], result["accounts"])
        self.assertEqual([request["proposal"]["scope"]], result["scopes"])
        for target in (
            dict(episode_ref="episode:other", episode_version=1),
            dict(episode_ref="episode:memory-owned-target", episode_version=2),
        ):
            changed = copy.deepcopy(request)
            changed["proposal"]["target"] = target
            self.assertEqual(409, (await self.post(ISSUE_V2, changed))[0])
            verification = self.verification(request, issued)
            verification["operation_digest"] = operation_digest(changed["proposal"])
            self.assertEqual(403, (await self.post(VERIFY, verification, "MEMORY"))[0])
        with self.p.store.connect() as db:
            self.assertEqual(
                1, db.execute("SELECT count(*) FROM memory_operation_proofs").fetchone()[0]
            )

    async def test_forged_source_scope_plain_origin_and_wrong_service_still_reject(self):
        _, request = self.episode_request()
        forged = copy.deepcopy(request)
        forged["source"]["parts"][0]["text"] = "model-invented request"
        foreign = copy.deepcopy(request)
        foreign["proposal"]["scope"]["person_id"] = "person:other"
        for changed in (forged, foreign):
            self.assertEqual(403, (await self.post(ISSUE_V2, changed))[0])
        plain = self.p.origins.issue(bearer("CONNECTOR"), "actor-a")
        changed = copy.deepcopy(request)
        changed["origin"] = changed["proposal"]["command"]["origin"] = dict(
            assertion_ref=plain["assertion_ref"]
        )
        self.assertEqual(403, (await self.post(ISSUE_V2, changed))[0])
        self.assertEqual(403, (await self.post(ISSUE_V2, request, "MEMORY"))[0])
        self.p.origins.revoke(bearer("ADMIN"), "entry", "actor-a")
        self.assertEqual(403, (await self.post(ISSUE_V2, request))[0])

    async def test_source_withdrawal_or_expiry_invalidates_existing_episode_proof(self):
        physical, request = self.episode_request()
        status, issued = await self.post(ISSUE_V2, request)
        self.assertEqual(200, status, issued)
        verification = self.verification(request, issued)
        edited = copy.deepcopy(physical)
        edited["message_key"]["revision"] = 2
        edited.update(kind="retract", parts=[])
        self.p.sources.register_input(bearer("CONNECTOR"), "input-entry", edited)
        self.assertEqual(409, (await self.post(VERIFY, verification, "MEMORY"))[0])
        self.now += 31
        self.assertEqual(403, (await self.post(VERIFY, verification, "MEMORY"))[0])
