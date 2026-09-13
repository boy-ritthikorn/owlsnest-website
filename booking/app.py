#!/usr/bin/env python3
"""OWL'S NEST table reservation requests and staff confirmation dashboard."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PUBLIC_UI = HERE / "public.html"
ADMIN_UI = HERE / "admin.html"
ADMIN_CONFIG = HERE.parent / "admin" / "config.json"
DB_PATH = Path(os.environ.get("OWL_BOOKING_DB", "/root/owlsnest-data/bookings.sqlite3"))
HOST = os.environ.get("OWL_BOOKING_HOST", "127.0.0.1")
PORT = int(os.environ.get("OWL_BOOKING_PORT", "5607"))
BASE_PATH = "/booking"
TZ = ZoneInfo("Asia/Bangkok")
SESSION_HOURS = 24 * 14
VALID_STATUSES = {"pending", "confirmed", "no_answer", "cancelled", "seated", "completed", "no_show"}
RATE_LIMIT = {}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.chmod(0o700)
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                booking_time TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                preference TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                contact_channel TEXT NOT NULL DEFAULT 'phone',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                assigned_tables TEXT NOT NULL DEFAULT '',
                staff_note TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(booking_date, booking_time)")
    DB_PATH.chmod(0o600)

def admin_config():
    return json.loads(ADMIN_CONFIG.read_text(encoding="utf-8"))

def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()

def make_token(cfg):
    expires = str(int(time.time()) + SESSION_HOURS * 3600)
    sig = hmac.new(cfg["secret"].encode(), ("booking:" + expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"

def valid_token(cfg, token):
    try:
        expires, sig = token.split(".", 1)
        expect = hmac.new(cfg["secret"].encode(), ("booking:" + expires).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expect) and int(expires) > time.time()
    except (ValueError, TypeError):
        return False

def normalize_phone(value):
    return re.sub(r"[^0-9+]", "", str(value or ""))

def validate_booking(body):
    today = datetime.now(TZ).date()
    try:
        booking_date = date.fromisoformat(str(body.get("date", "")))
    except ValueError:
        raise ValueError("กรุณาเลือกวันที่")
    if booking_date < today:
        raise ValueError("ไม่สามารถจองวันที่ผ่านมาแล้ว")
    if booking_date > today + timedelta(days=90):
        raise ValueError("เปิดรับจองล่วงหน้าไม่เกิน 90 วัน")
    booking_time = str(body.get("time", ""))
    if not re.fullmatch(r"(?:1[7-9]|2[0-2]):(?:00|30)|23:00", booking_time):
        raise ValueError("กรุณาเลือกเวลาระหว่าง 17:00–23:00 น.")
    try:
        party_size = int(body.get("party_size", 0))
    except (TypeError, ValueError):
        party_size = 0
    if not 1 <= party_size <= 30:
        raise ValueError("รองรับการจองออนไลน์ 1–30 ท่าน หากมากกว่านี้กรุณาโทรหาร้าน")
    name = str(body.get("name", "")).strip()
    if len(name) < 2 or len(name) > 80:
        raise ValueError("กรุณากรอกชื่อผู้จอง")
    phone = normalize_phone(body.get("phone"))
    digits = re.sub(r"\D", "", phone)
    if not 9 <= len(digits) <= 15:
        raise ValueError("กรุณากรอกเบอร์โทรที่ติดต่อได้")
    preference = str(body.get("preference", "")).strip()[:80]
    channel = str(body.get("contact_channel", "phone"))
    if channel not in {"phone", "line", "messenger"}:
        channel = "phone"
    note = str(body.get("note", "")).strip()[:500]
    return booking_date.isoformat(), booking_time, party_size, preference, name, phone, channel, note

def booking_code(booking_date):
    return "OWL-" + booking_date.replace("-", "")[2:] + "-" + secrets.token_hex(2).upper()

def limited(ip):
    now = time.time()
    recent = [stamp for stamp in RATE_LIMIT.get(ip, []) if now - stamp < 600]
    RATE_LIMIT[ip] = recent
    if len(recent) >= 5:
        return True
    recent.append(now)
    return False

class Handler(BaseHTTPRequestHandler):
    server_version = "OwlBooking"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 32_000:
            raise ValueError("ข้อมูลใหญ่เกินไป")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def authed(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        token = cookie["owl_booking_admin"].value if "owl_booking_admin" in cookie else ""
        return valid_token(admin_config(), token)

    def client_ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def do_HEAD(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            size = len(PUBLIC_UI.read_bytes())
        elif path == "/admin":
            size = len(ADMIN_UI.read_bytes())
        else:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            return self.send(HTTPStatus.OK, PUBLIC_UI.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if path == "/admin":
            return self.send(HTTPStatus.OK, ADMIN_UI.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if path == "/api/me":
            return self.send(HTTPStatus.OK, {"authed": self.authed()})
        if path == "/api/admin/bookings":
            if not self.authed():
                return self.send(HTTPStatus.UNAUTHORIZED, {"error": "ยังไม่ได้เข้าสู่ระบบ"})
            query = parse_qs(parsed.query)
            target = query.get("date", [datetime.now(TZ).date().isoformat()])[0]
            status = query.get("status", [""])[0]
            sql = "SELECT * FROM bookings WHERE booking_date = ?"
            args = [target]
            if status in VALID_STATUSES:
                sql += " AND status = ?"
                args.append(status)
            sql += " ORDER BY booking_time, created_at"
            with db() as con:
                rows = [dict(row) for row in con.execute(sql, args)]
            return self.send(HTTPStatus.OK, {"bookings": rows, "date": target})
        return self.send(HTTPStatus.NOT_FOUND, {"error": "ไม่พบหน้านี้"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self.body_json()
        except Exception:
            return self.send(HTTPStatus.BAD_REQUEST, {"error": "ข้อมูลไม่ถูกต้อง"})
        if path == "/api/reservations":
            if body.get("website"):
                return self.send(HTTPStatus.OK, {"ok": True})
            if limited(self.client_ip()):
                return self.send(HTTPStatus.TOO_MANY_REQUESTS, {"error": "ส่งคำขอหลายครั้งเกินไป กรุณารอสักครู่"})
            try:
                values = validate_booking(body)
            except ValueError as exc:
                return self.send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            now = datetime.now(TZ).isoformat(timespec="seconds")
            code = booking_code(values[0])
            with db() as con:
                con.execute("""
                    INSERT INTO bookings
                    (code, created_at, updated_at, booking_date, booking_time, party_size,
                     preference, customer_name, phone, contact_channel, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (code, now, now, *values))
            return self.send(HTTPStatus.CREATED, {
                "ok": True, "code": code,
                "message": "ร้านได้รับคำขอจองแล้ว กรุณารอการโทรยืนยันจากทางร้าน"
            })
        if path == "/api/login":
            cfg = admin_config()
            ok = hmac.compare_digest(
                hash_password(str(body.get("password", "")), cfg["salt"]), cfg["password_hash"]
            )
            time.sleep(0.5)
            if not ok:
                return self.send(HTTPStatus.UNAUTHORIZED, {"error": "รหัสผ่านไม่ถูกต้อง"})
            cookie = (f"owl_booking_admin={make_token(cfg)}; Path={BASE_PATH}/; HttpOnly; "
                      f"SameSite=Lax; Secure; Max-Age={SESSION_HOURS * 3600}")
            return self.send(HTTPStatus.OK, {"ok": True}, cookie=cookie)
        if path == "/api/logout":
            cookie = f"owl_booking_admin=; Path={BASE_PATH}/; HttpOnly; SameSite=Lax; Secure; Max-Age=0"
            return self.send(HTTPStatus.OK, {"ok": True}, cookie=cookie)
        if path == "/api/admin/update":
            if not self.authed():
                return self.send(HTTPStatus.UNAUTHORIZED, {"error": "ยังไม่ได้เข้าสู่ระบบ"})
            try:
                booking_id = int(body.get("id"))
            except (TypeError, ValueError):
                return self.send(HTTPStatus.BAD_REQUEST, {"error": "รายการไม่ถูกต้อง"})
            status = str(body.get("status", ""))
            if status not in VALID_STATUSES:
                return self.send(HTTPStatus.BAD_REQUEST, {"error": "สถานะไม่ถูกต้อง"})
            tables = str(body.get("assigned_tables", "")).strip()[:100]
            staff_note = str(body.get("staff_note", "")).strip()[:500]
            now = datetime.now(TZ).isoformat(timespec="seconds")
            with db() as con:
                cur = con.execute("""
                    UPDATE bookings SET status=?, assigned_tables=?, staff_note=?, updated_at=? WHERE id=?
                """, (status, tables, staff_note, now, booking_id))
            if not cur.rowcount:
                return self.send(HTTPStatus.NOT_FOUND, {"error": "ไม่พบรายการจอง"})
            return self.send(HTTPStatus.OK, {"ok": True})
        return self.send(HTTPStatus.NOT_FOUND, {"error": "ไม่พบหน้านี้"})

def main():
    init_db()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
