import unittest
from datetime import datetime, timedelta
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

if __name__ == "__main__":
    unittest.main()
