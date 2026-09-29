"""Authority must still hold after an outbound slot becomes available."""

import asyncio
import unittest
from types import SimpleNamespace

from services.platform.contracts import Fault
from services.platform.web_persona_author import WebPersonaAuthor


class PersonaAuthorAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_edit_stops_apply_before_the_upstream_request(self):
        actions = {"persona.edit", "persona.apply"}
        console = SimpleNamespace(
            session_valid=lambda _session: True,
            persona_read_authorised=lambda: True,
            persona_action_authorised=lambda action: action in actions,
        )
        slots = asyncio.Semaphore(1)
        await slots.acquire()

        class Client:
            def __init__(self):
                self.slots = slots
                self.credential_reads = 0

            def credential(self):
                self.credential_reads += 1
                return "should-not-be-read"

        client = Client()
        personas = SimpleNamespace(
            rule={"authoring_enabled": True, "apply_subjects": ("actor:a",)},
            client=client,
            enabled=True,
        )
        author = WebPersonaAuthor(console, personas)
        pending = asyncio.create_task(
            author._call(
                {"operation": "apply_profile", "request_id": "interleaved-revoke"},
                object(),
                "persona.apply",
                "actor:a",
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(len(slots._waiters), 1)
        actions.remove("persona.edit")
        slots.release()
        with self.assertRaises(Fault) as refusal:
            await pending
        self.assertEqual(refusal.exception.code, "persona_write_required")
        self.assertEqual(client.credential_reads, 0)
