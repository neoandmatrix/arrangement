"""Synthetic WebSocket integration tests: no model calls, real numbers or external requests."""

import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app_api
import app_calls
import channels
import prompts
import store
import workflow
from price_fixture import checked_quote
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, InterruptionFrame


class AppAudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "audio.db"
        self.env = patch.dict(os.environ, {"APP_KEY": "synthetic-test-key"})
        self.env.start()
        self.family = "F12345678"
        self.other_family = "F87654321"
        store.save_participant(self.family, None)
        store.save_participant(self.other_family, None)
        self.event_id = store.save_event({**prompts.SAMPLE_NAAMKARAN, "family_id": self.family})
        for vendor_id, price in (("K01", 2400), ("K03", 2600)):
            store.save_quote(self.event_id, vendor_id, None, checked_quote({"available": True, "price_amount": price,
                "price_unit": "total", "transport_charge": 0, "advance_amount": 500}, "cake"))
        workflow.book("cake", "K01")
        @asynccontextmanager
        async def lifespan(app):
            yield
            await app_calls.close()
        app = FastAPI(lifespan=lifespan)
        app.include_router(app_api.router)
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        app_calls._presence.clear()
        channels._sockets.clear()
        channels._calls.clear()
        self.env.stop()
        store.DB_PATH = self.original_db
        self.tmp.cleanup()

    def headers(self, participant=None):
        return {"X-App-Key": "synthetic-test-key", "X-Participant-ID": participant or self.family}

    def presence(self, participant):
        return self.client.websocket_connect(f"/app/presence/ws?participant_id={participant}", headers={"X-App-Key": "synthetic-test-key"})

    def audio(self, ticket):
        return self.client.websocket_connect(ticket["audio_path"], headers={"Authorization": f"Bearer {ticket['audio_token']}"})

    def test_direct_policy_only_active_primary_and_own_family(self):
        self.assertTrue(app_calls.direct_allowed(self.family, "K01", self.event_id))
        self.assertTrue(app_calls.direct_allowed("K01", self.family, self.event_id))
        self.assertFalse(app_calls.direct_allowed(self.other_family, "K01", self.event_id))
        self.assertFalse(app_calls.direct_allowed(self.family, "K03", self.event_id))
        self.assertFalse(app_calls.direct_allowed("K01", "K03", self.event_id))

    def test_bidirectional_direct_pcm_without_recording(self):
        with self.presence("K01") as presence:
            response = self.client.post("/app/direct-calls", headers=self.headers(), json={"target_id": "K01"})
            self.assertEqual(response.status_code, 200, response.text)
            caller_ticket = response.json()
            ring = presence.receive_json()
            self.assertEqual(ring["kind"], "direct")
            self.assertEqual(ring["caller_id"], self.family)
            with self.audio(caller_ticket) as caller:
                self.assertEqual(caller.receive_json()["type"], "ringing")
                response = self.client.post(f"/app/calls/{ring['call_id']}/connect", headers=self.headers("K01"))
                with self.audio(response.json()) as vendor:
                    self.assertEqual(caller.receive_json()["type"], "connected")
                    self.assertEqual(vendor.receive_json()["type"], "connected")
                    caller.send_bytes(b"\x01\x00" * 320)
                    self.assertEqual(vendor.receive_bytes(), b"\x01\x00" * 320)
                    vendor.send_bytes(b"\x02\x00" * 320)
                    self.assertEqual(caller.receive_bytes(), b"\x02\x00" * 320)
                    vendor.send_json({"type": "hangup"})
                    self.assertEqual(caller.receive_json()["type"], "ended")
        self.assertEqual(store.calls(self.event_id), [])
        self.assertFalse(app_calls.busy(self.family))

    def test_vendor_can_ring_family_and_family_can_decline(self):
        with self.presence(self.family) as presence:
            response = self.client.post("/app/direct-calls", headers=self.headers("K01"), json={"target_id": self.family})
            self.assertEqual(response.status_code, 200)
            ring = presence.receive_json()
            response = self.client.post(f"/app/calls/{ring['call_id']}/decline", headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(app_calls.busy("K01"))

    def test_wrong_phone_cannot_connect_or_end_direct_call(self):
        with self.presence("K01") as presence:
            ticket = self.client.post("/app/direct-calls", headers=self.headers(), json={"target_id": "K01"}).json()
            presence.receive_json()
            for route in (f"/app/calls/{ticket['call_id']}/connect", f"/app/sessions/{ticket['call_id']}/end"):
                self.assertEqual(self.client.post(route, headers=self.headers(self.other_family)).status_code, 403)
            self.client.post(f"/app/sessions/{ticket['call_id']}/end", headers=self.headers())

    def test_offline_target_and_unbooked_vendor_are_not_called(self):
        self.assertEqual(self.client.post("/app/direct-calls", headers=self.headers(), json={"target_id": "K01"}).status_code, 409)
        self.assertFalse(app_calls.busy(self.family))
        self.assertEqual(self.client.post("/app/direct-calls", headers=self.headers(), json={"target_id": "K03"}).status_code, 403)

    def test_family_event_and_update_decisions_are_scoped(self):
        self.assertIsNone(self.client.get("/app/events/current", headers=self.headers(self.other_family)).json()["event"])
        self.assertEqual(self.client.post("/app/events/current/vendors/K01/approve", headers=self.headers(self.other_family)).status_code, 403)
        update_id = workflow.record_vendor_update(self.event_id, "K01", {"kind": "price", "price_unit": "total", "price_amount": 2800, "notes": "Rate"}, "Base price is 2800 rupees in total")
        self.assertEqual(self.client.post(f"/app/updates/{update_id}/approve", headers=self.headers(self.other_family)).status_code, 403)
        self.assertEqual(self.client.post(f"/app/updates/{update_id}/approve", headers=self.headers()).status_code, 200)

    def test_assistant_busy_lease_and_abandoned_ticket_cleanup(self):
        response = self.client.post("/app/assistant-calls", headers=self.headers(), json={"mode": "form"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(channels.CALL_LOCK.locked())
        self.assertEqual(self.client.post("/app/assistant-calls", headers=self.headers(self.other_family), json={"mode": "form"}).status_code, 409)
        ticket = response.json()
        self.client.post(f"/app/sessions/{ticket['call_id']}/end", headers=self.headers())
        self.assertFalse(channels.CALL_LOCK.locked())
        with self.assertRaises(WebSocketDisconnect):
            with self.audio(ticket):
                pass

    def test_form_call_emits_draft_without_saving_event(self):
        async def fake_conversation(transport, cfg, **kwargs):
            self.assertEqual(cfg.mode, "form")
            self.assertEqual(kwargs, {"sample_rate_in": 16000, "sample_rate_out": 24000})
            await cfg.on_result({"type": "form", "draft": {"family_name": "Demo", "guests": 60}, "complete": True})
        with patch("app_calls.run_conversation", fake_conversation):
            ticket = self.client.post("/app/assistant-calls", headers=self.headers(), json={"mode": "form", "language": "kn-IN"}).json()
            with self.audio(ticket) as audio:
                self.assertEqual(audio.receive_json()["type"], "connected")
                self.assertEqual(audio.receive_json()["draft"]["guests"], 60)
                self.assertEqual(audio.receive_json()["type"], "ended")
        self.assertEqual(store.current_event()[0], self.event_id)
        self.assertFalse(channels.CALL_LOCK.locked())

    def test_audio_ticket_rejects_wrong_token(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/app/audio/no-such-call?token=wrong"):
                pass

    def test_vendor_update_only_for_vendor_with_enquiry(self):
        self.assertEqual(self.client.post("/app/assistant-calls", headers=self.headers("K02"), json={"mode": "vendor_update"}).status_code, 400)
        self.assertEqual(self.client.post("/app/assistant-calls", headers=self.headers("K01"), json={"mode": "form"}).status_code, 403)

    def test_submit_requires_pin_and_binds_family_without_external_calls(self):
        body = dict(prompts.SAMPLE_NAAMKARAN)
        body["date"] = (date.today() + timedelta(days=20)).isoformat()
        self.assertEqual(self.client.post("/app/events", headers=self.headers(), json=body).status_code, 422)
        self.assertEqual(store.current_event()[0], self.event_id)
        body["location"] = {"lat": 12.9352, "lon": 77.6245}
        async def no_external_enquiries(event_id, brief):
            pass
        with patch("enquiries.run_event", no_external_enquiries):
            response = self.client.post("/app/events", headers=self.headers(), json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(store.current_event()[1]["family_id"], self.family)

    def test_invalid_draft_is_rejected_before_acquiring_gnani_lease(self):
        response = self.client.post("/app/assistant-calls", headers=self.headers(), json={"mode": "form", "draft": []})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(channels.CALL_LOCK.locked())

    def test_legacy_answer_decline_and_jobs_cannot_impersonate_another_vendor(self):
        pc = channels.PendingCall("APsynthetic", "K01", {"mode": "vendor_call", "vendor_id": "K01"}, {})
        channels._calls[pc.call_id] = pc
        for route in (f"/app/calls/{pc.call_id}/answer", f"/app/calls/{pc.call_id}/decline", f"/app/calls/{pc.call_id}/connect"):
            self.assertEqual(self.client.post(route, headers=self.headers("K03"), json={}).status_code, 403)
        self.assertFalse(pc.answered.is_set())
        self.assertFalse(pc.declined.is_set())
        self.assertEqual(self.client.get("/app/vendor/K01/jobs", headers=self.headers("K03")).status_code, 403)
        self.assertEqual(self.client.post("/app/vendor/register", headers=self.headers("K03"), json={"vendor_id": "K01"}).status_code, 403)

    def test_incoming_ai_call_uses_pcm_and_finishes_existing_call_queue(self):
        pc = channels.PendingCall("APsynthetic", "K01", {"mode": "vendor_call", "vendor_id": "K01", "event_id": self.event_id}, {})
        channels._calls[pc.call_id] = pc
        async def fake_conversation(transport, cfg, **kwargs):
            self.assertEqual(cfg.call_id, pc.call_id)
            self.assertEqual(cfg.event_id, self.event_id)
            self.assertTrue(pc.answered.is_set())
        with patch("app_calls.run_conversation", fake_conversation):
            response = self.client.post(f"/app/calls/{pc.call_id}/connect", headers=self.headers("K01"))
            self.assertEqual(response.status_code, 200)
            with self.audio(response.json()) as audio:
                self.assertEqual(audio.receive_json()["type"], "connected")
                self.assertEqual(audio.receive_json()["type"], "ended")
        self.assertTrue(pc.finished.is_set())
        self.assertEqual(store.enquiry(self.event_id, "K01")["status"], "in_call")

    def test_direct_call_is_revoked_if_booking_changes_before_answer(self):
        with self.presence("K01") as presence:
            ticket = self.client.post("/app/direct-calls", headers=self.headers(), json={"target_id": "K01"}).json()
            presence.receive_json()
            store.set_booking(self.event_id, "cake", "primary", "K03", "active")
            with self.assertRaises(WebSocketDisconnect):
                with self.audio(ticket):
                    pass
            self.assertFalse(app_calls.busy(self.family))

    def test_price_reconfirmation_is_scoped_to_the_family(self):
        with patch("enquiries.reconfirm", new_callable=AsyncMock, return_value="answered") as recheck:
            response = self.client.post("/app/events/current/vendors/K01/reconfirm", headers=self.headers(self.other_family))
            self.assertEqual(response.status_code, 403)
            recheck.assert_not_called()
            response = self.client.post("/app/events/current/vendors/K01/reconfirm", headers=self.headers())
            self.assertEqual(response.status_code, 200)
            recheck.assert_called_once()

    def test_quote_correction_approval_cannot_bypass_server_price_guards(self):
        current = store.latest_quotes(self.event_id)["K01"]
        result = workflow.record_quote(self.event_id, "K01", "synthetic-recheck", {**current, "transport_charge": 500000})
        response = self.client.post(f"/app/updates/{result['update_id']}/approve", headers=self.headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["transport_charge"], 0)


class PCMSerializerTest(unittest.IsolatedAsyncioTestCase):
    async def test_binary_pcm_and_interruptions(self):
        serializer = app_calls.PCMSerializer()
        frame = await serializer.deserialize(b"\x00\x00" * 320)
        self.assertIsInstance(frame, InputAudioRawFrame)
        self.assertEqual(frame.sample_rate, 16000)
        self.assertEqual(await serializer.serialize(OutputAudioRawFrame(b"\x01\x00", 24000, 1)), b"\x01\x00")
        self.assertEqual(json.loads(await serializer.serialize(InterruptionFrame())), {"type": "clear"})

    async def test_bad_pcm_packets_are_ignored(self):
        serializer = app_calls.PCMSerializer()
        for data in (b"", b"\x00", b"\x00" * 6402, "invalid JSON", "[]"):
            self.assertIsNone(await serializer.deserialize(data))
