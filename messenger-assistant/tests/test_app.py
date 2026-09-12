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
