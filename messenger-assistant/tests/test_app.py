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


if __name__ == "__main__":
    unittest.main()
