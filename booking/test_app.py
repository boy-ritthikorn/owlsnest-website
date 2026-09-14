import unittest
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
from booking import app

class BookingValidationTests(unittest.TestCase):
    def good(self):
        tomorrow = (datetime.now(ZoneInfo("Asia/Bangkok")).date() + timedelta(days=1)).isoformat()
        return {"date": tomorrow, "time": "19:00", "party_size": 4,
                "name": "ทดสอบ ลูกค้า", "phone": "090-123-4567",
                "preference": "ให้ร้านจัดให้", "contact_channel": "phone", "note": ""}
    def test_valid_booking(self):
        values = app.validate_booking(self.good())
        self.assertEqual(values[2], 4)
        self.assertEqual(values[5], "0901234567")
    def test_invalid_time(self):
        body = self.good(); body["time"] = "16:30"
        with self.assertRaisesRegex(ValueError, "17:00"):
            app.validate_booking(body)
    def test_party_limit(self):
        body = self.good(); body["party_size"] = 31
        with self.assertRaisesRegex(ValueError, "1–30"):
            app.validate_booking(body)
    def test_bad_phone(self):
        body = self.good(); body["phone"] = "123"
        with self.assertRaisesRegex(ValueError, "เบอร์โทร"):
            app.validate_booking(body)

    def test_real_thai_booking_is_not_spam(self):
        body = self.good(); body["form_ts"] = 100
        values = app.validate_booking(body)
        self.assertEqual(app.spam_reasons(body, values, now=110), [])

    def test_honeypot_is_quarantined(self):
        body = self.good(); body["website"] = "https://spam.example"
        values = app.validate_booking(body)
        self.assertEqual(app.spam_reasons(body, values), ["honeypot"])

    def test_too_fast_is_quarantined(self):
        body = self.good(); body["form_ts"] = 100
        values = app.validate_booking(body)
        self.assertEqual(app.spam_reasons(body, values, now=101), ["too_fast"])

    def test_two_weak_signals_are_quarantined(self):
        body = self.good(); body.update({"name": "Cheap SEO", "phone": "+1 202 555 0182",
                                        "note": "visit https://spam.example"})
        values = app.validate_booking(body)
        self.assertEqual(app.spam_reasons(body, values, now=100),
                         ["phone_format", "link", "no_thai"])

    def test_one_weak_signal_does_not_block_customer(self):
        body = self.good(); body.update({"name": "John", "note": "Birthday"})
        values = app.validate_booking(body)
        self.assertEqual(app.spam_reasons(body, values, now=100), [])

    def test_line_config_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "line.json"
            with patch.object(app, "LINE_CONFIG", target):
                app.save_line_config({"channel_secret": "secret"})
                self.assertEqual(json.loads(target.read_text())["channel_secret"], "secret")
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_line_alert_skips_until_target_is_ready(self):
        with patch.object(app, "line_config", return_value={}):
            self.assertEqual(app.send_line_alert({}), "not_configured")

if __name__ == "__main__":
    unittest.main()
