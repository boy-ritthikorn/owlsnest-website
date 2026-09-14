import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("OWL_ASSISTANT_ENV", "/tmp/nonexistent-owl-assistant-env")
import app


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_FILE = Path(self.temp.name) / "test.db"
        app.CFG = {
            "META_PAGE_TOKEN": "page-token",
            "META_APP_ID": "our-app",
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_MODEL": "gpt-5.6-luna",
        }
        app.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_webhook_message_is_ignored(self):
        first = app.record_incoming("person-1", "mid-1", "เปิดกี่โมง", "simulator")
        second = app.record_incoming("person-1", "mid-1", "เปิดกี่โมง", "simulator")
        self.assertIsInstance(first, int)
        self.assertIsNone(second)

    def test_learning_section_is_visible_when_empty(self):
        body = app.dashboard().decode("utf-8")
        self.assertIn("เรียนรู้จากคำตอบของทีม", body)
        self.assertIn("ยังไม่มีคำตอบจากทีมที่รอตรวจ", body)

    def test_team_echo_is_context_and_closes_pending_draft(self):
        incoming_id = app.record_incoming("person-1", "mid-in", "จองโต๊ะได้ไหม", "simulator")
        with app.db() as conn:
            conn.execute(
                """INSERT INTO drafts(message_id, reply, confidence, needs_human, reason, status, error, created_at, updated_at)
                   VALUES(?, 'ร่างเดิม', 'medium', 1, '', 'pending', '', 1, 1)""",
                (incoming_id,),
            )
        event = {
            "sender": {"id": "page-1"},
            "recipient": {"id": "person-1"},
            "message": {"mid": "mid-team", "is_echo": True, "text": "ได้ครับ รบกวนแจ้งวันและจำนวนคนครับ"},
        }
        app.process_event(event)
        with app.db() as conn:
            draft = conn.execute("SELECT status FROM drafts WHERE message_id=?", (incoming_id,)).fetchone()
            outgoing = conn.execute("SELECT direction, source FROM messages WHERE mid='mid-team'").fetchone()
            example = conn.execute("SELECT status FROM reply_examples").fetchone()
        self.assertEqual(draft["status"], "answered_elsewhere")
        self.assertEqual((outgoing["direction"], outgoing["source"]), ("out", "facebook_team"))
        self.assertEqual(example["status"], "pending")
        self.assertEqual(app.recent_history("person-1")[-1]["role"], "staff")

    def test_echo_from_this_app_is_not_learned(self):
        event = {
            "sender": {"id": "page-1"},
            "recipient": {"id": "person-1"},
            "message": {"mid": "mid-app", "is_echo": True, "app_id": "our-app", "text": "ตอบจากระบบ"},
        }
        app.process_event(event)
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reply_examples").fetchone()[0], 0)

    @patch("app.generate_reply")
    def test_team_reply_before_slow_draft_keeps_draft_closed(self, generate):
        generate.return_value = {
            "reply": "ร่างที่มาช้า", "confidence": "high", "needs_human": False, "reason": "",
        }
        incoming_id = app.record_incoming("person-1", "mid-in", "สอบถามครับ", "simulator")
        app.record_team_echo({
            "recipient": {"id": "person-1"},
            "message": {"mid": "mid-team", "is_echo": True, "text": "ทีมตอบแล้วครับ"},
        })
        app.create_draft(incoming_id, "person-1")
        with app.db() as conn:
            draft = conn.execute("SELECT status FROM drafts WHERE message_id=?", (incoming_id,)).fetchone()
        self.assertEqual(draft["status"], "answered_elsewhere")

    @patch("app.requests.post")
    def test_openai_structured_draft(self, post):
        response = Mock(ok=True)
        response.json.return_value = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "reply": "ร้านเปิดทุกวัน 17:00–24:00 น. ครับ",
                "confidence": "high",
                "needs_human": False,
                "reason": "ข้อมูลอยู่ในฐานความรู้"
            }, ensure_ascii=False)}]}]
        }
        post.return_value = response
        app.record_incoming("person-1", "mid-2", "วันนี้เปิดกี่โมง", "simulator")
        result = app.generate_reply("person-1")
        self.assertEqual(result["confidence"], "high")
        self.assertIn("17:00", result["reply"])
        sent = post.call_args.kwargs["json"]
        self.assertFalse(sent["store"])
        self.assertEqual(sent["text"]["format"]["type"], "json_schema")
        self.assertNotIn("person-1", sent["safety_identifier"])

    @patch("app.requests.post")
    def test_approved_team_reply_is_added_as_style_example(self, post):
        response = Mock(ok=True)
        response.json.return_value = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "reply": "ได้ครับ", "confidence": "medium", "needs_human": True, "reason": "รอตรวจ"
            }, ensure_ascii=False)}]}]
        }
        post.return_value = response
        app.record_incoming("old-customer", "old-in", "มีโต๊ะไหม", "simulator")
        message_id = app.record_team_echo({
            "recipient": {"id": "old-customer"},
            "message": {"mid": "old-out", "is_echo": True, "text": "ขอตรวจสอบโต๊ะให้สักครู่นะครับ"},
        })
        with app.db() as conn:
            conn.execute("UPDATE reply_examples SET status='approved' WHERE message_id=?", (message_id,))
        app.record_incoming("new-customer", "new-in", "ว่างไหม", "simulator")
        app.generate_reply("new-customer")
        payload = json.loads(post.call_args.kwargs["json"]["input"])
        self.assertEqual(payload["approved_staff_examples"][0]["customer"], "มีโต๊ะไหม")
        self.assertEqual(payload["approved_staff_examples"][0]["staff"], "ขอตรวจสอบโต๊ะให้สักครู่นะครับ")

    @patch("app.requests.post")
    def test_send_message_uses_response_type(self, post):
        response = Mock(ok=True)
        response.json.return_value = {"message_id": "out-1"}
        post.return_value = response
        self.assertEqual(app.send_message("person-1", "สวัสดีครับ"), "out-1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["messaging_type"], "RESPONSE")

    def test_signature_formula(self):
        body = b'{"object":"page"}'
        secret = "test-secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(signature, signature))

    def test_clinic_payload_keeps_only_ad_trace(self):
        event = {
            "sender": {"id": "clinic-customer"},
            "timestamp": 1789380000000,
            "message": {
                "mid": "clinic-mid-1",
                "text": "ข้อมูลการรักษาที่ห้ามส่งต่อ",
                "referral": {
                    "source": "ADS",
                    "type": "OPEN_THREAD",
                    "ad_id": "120000000000001",
                    "ref": "campaign-marker",
                },
            },
        }
        payload = app.clinic_attribution_payload(app.CLINIC_PAGE_ID, event)
        self.assertEqual(payload["ad_id"], "120000000000001")
        self.assertEqual(payload["psid"], "clinic-customer")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("ข้อมูลการรักษา", serialized)
        self.assertNotIn("text", payload)

    @patch("app.forward_clinic_attribution")
    @patch("app.process_event")
    def test_clinic_event_never_enters_owl_messages(self, process_event, forward):
        app.process_page_event(app.CLINIC_PAGE_ID, {"message": {"text": "private"}})
        forward.assert_called_once()
        process_event.assert_not_called()

    def test_admin_setup_token_is_single_use(self):
        env_file = Path(self.temp.name) / "runtime.env"
        note_file = Path(self.temp.name) / "credential.txt"
        expires = str(int(__import__("time").time()) + 300)
        env_file.write_text(f"ADMIN_USER='old'\nADMIN_PASSWORD='old-pass'\nSETUP_TOKEN='one-time'\nSETUP_EXPIRES='{expires}'\n", encoding="utf-8")
        old_env_file, old_note, old_cfg = app.ENV_FILE, app.CREDENTIAL_NOTE, dict(app.CFG)
        try:
            app.ENV_FILE, app.CREDENTIAL_NOTE = env_file, note_file
            app.CFG.update({"SETUP_TOKEN": "one-time", "SETUP_EXPIRES": expires})
            app.save_admin_password("one-time", "StrongPassword1!", "StrongPassword1!")
            self.assertFalse(app.valid_setup_token("one-time"))
            self.assertIn("Username: owladmin", note_file.read_text(encoding="utf-8"))
            self.assertEqual(oct(note_file.stat().st_mode & 0o777), "0o600")
        finally:
            app.ENV_FILE, app.CREDENTIAL_NOTE, app.CFG = old_env_file, old_note, old_cfg

    @patch("app.requests.get")
    def test_save_openai_key_keeps_it_out_of_database(self, get):
        response = Mock(ok=True)
        get.return_value = response
        env_file = Path(self.temp.name) / "runtime.env"
        env_file.write_text("OPENAI_API_KEY=''\nOPENAI_MODEL='gpt-5.6-luna'\n", encoding="utf-8")
        old_env_file = app.ENV_FILE
        try:
            app.ENV_FILE = env_file
            app.save_openai_key("sk-test-value-not-a-real-key")
            self.assertIn("sk-test-value", env_file.read_text(encoding="utf-8"))
            with app.db() as conn:
                values = " ".join(str(x) for row in conn.execute("SELECT * FROM messages") for x in row)
            self.assertNotIn("sk-test-value", values)
        finally:
            app.ENV_FILE = old_env_file


if __name__ == "__main__":
    unittest.main()
