#!/usr/bin/env python3
"""หลังบ้านแก้ข้อมูลเว็บ Owl's Nest — แก้ตารางดนตรี / เมนู / ราคา แล้วสร้างหน้าเว็บใหม่ทันที"""
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import time
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path("/root/owlsnest-website")
HERE = Path(__file__).resolve().parent
DATA = SITE / "data.json"
BACKUPS = HERE / "backups"
CONFIG = HERE / "config.json"
UI = HERE / "admin.html"
BUILD = SITE / "build.py"
IMAGES = SITE / "images"

BASE_PATH = os.environ.get("OWL_ADMIN_BASE_PATH", "/owl-admin").rstrip("/")
HOST = os.environ.get("OWL_ADMIN_HOST", "127.0.0.1")
PORT = int(os.environ.get("OWL_ADMIN_PORT", "5605"))
SITE_URL = os.environ.get("OWL_SITE_URL", "https://owlsnestbar.com/")
SESSION_HOURS = 24 * 14
KEEP_BACKUPS = 30


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    CONFIG.chmod(0o600)


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def make_token(cfg):
    expires = str(int(time.time()) + SESSION_HOURS * 3600)
    sig = hmac.new(cfg["secret"].encode(), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_token(cfg, token):
    try:
        expires, sig = token.split(".", 1)
    except ValueError:
        return False
    expect = hmac.new(cfg["secret"].encode(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expect) and int(expires) > time.time()


def backup_data():
    BACKUPS.mkdir(exist_ok=True)
    if DATA.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DATA, BACKUPS / f"data-{stamp}.json")
    old = sorted(BACKUPS.glob("data-*.json"))[:-KEEP_BACKUPS]
    for f in old:
        f.unlink()


class Handler(BaseHTTPRequestHandler):
    server_version = "OwlAdmin"

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------
    def send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4_000_000:
            raise ValueError("ข้อมูลใหญ่เกินไป")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def authed(self, cfg):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        token = cookie["owl_admin"].value if "owl_admin" in cookie else ""
        return valid_token(cfg, token)

    # ---------- routes ----------
    def do_GET(self):
        cfg = load_config()
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            return self.send(HTTPStatus.OK, UI.read_text(encoding="utf-8"),
                             "text/html; charset=utf-8")
        if path == "/data":
            if not self.authed(cfg):
                return self.send(HTTPStatus.UNAUTHORIZED, {"error": "ยังไม่ได้เข้าสู่ระบบ"})
            images = sorted(p.name for p in IMAGES.glob("*.jpg"))
            return self.send(HTTPStatus.OK, {
                "data": json.loads(DATA.read_text(encoding="utf-8")),
                "images": images,
                "site_url": SITE_URL,
            })
        if path == "/me":
            return self.send(HTTPStatus.OK, {"authed": self.authed(cfg)})
        return self.send(HTTPStatus.NOT_FOUND, {"error": "ไม่พบหน้านี้"})

    def do_POST(self):
        cfg = load_config()
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/login":
            try:
                body = self.body_json()
            except Exception:
                return self.send(HTTPStatus.BAD_REQUEST, {"error": "ข้อมูลไม่ถูกต้อง"})
            ok = hmac.compare_digest(
                hash_password(str(body.get("password", "")), cfg["salt"]), cfg["password_hash"]
            )
            time.sleep(0.6)
            if not ok:
                return self.send(HTTPStatus.UNAUTHORIZED, {"error": "รหัสผ่านไม่ถูกต้อง"})
            cookie = (f"owl_admin={make_token(cfg)}; Path={BASE_PATH}/; HttpOnly; "
                      f"SameSite=Lax; Secure; Max-Age={SESSION_HOURS * 3600}")
            return self.send(HTTPStatus.OK, {"ok": True}, cookie=cookie)

        if path == "/logout":
            cookie = f"owl_admin=; Path={BASE_PATH}/; HttpOnly; SameSite=Lax; Secure; Max-Age=0"
            return self.send(HTTPStatus.OK, {"ok": True}, cookie=cookie)

        if not self.authed(cfg):
            return self.send(HTTPStatus.UNAUTHORIZED, {"error": "ยังไม่ได้เข้าสู่ระบบ"})

        if path == "/save":
            try:
                body = self.body_json()
            except Exception as exc:
                return self.send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            data = body.get("data")
            for key in ("music", "signature", "wine", "menu", "drinks"):
                if key not in (data or {}):
                    return self.send(HTTPStatus.BAD_REQUEST, {"error": f"ข้อมูลไม่ครบ ({key})"})
            backup_data()
            DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            result = subprocess.run(["/usr/bin/python3", str(BUILD)],
                                    capture_output=True, text=True, cwd=str(SITE), timeout=60)
            if result.returncode != 0:
                return self.send(HTTPStatus.INTERNAL_SERVER_ERROR,
                                 {"error": "สร้างเว็บไม่สำเร็จ: " + (result.stderr or "")[-400:]})
            return self.send(HTTPStatus.OK, {"ok": True, "message": result.stdout.strip()})

        if path == "/password":
            try:
                body = self.body_json()
            except Exception:
                return self.send(HTTPStatus.BAD_REQUEST, {"error": "ข้อมูลไม่ถูกต้อง"})
            new = str(body.get("new", ""))
            if len(new) < 8:
                return self.send(HTTPStatus.BAD_REQUEST, {"error": "รหัสผ่านต้องยาวอย่างน้อย 8 ตัว"})
            if not hmac.compare_digest(
                hash_password(str(body.get("current", "")), cfg["salt"]), cfg["password_hash"]
            ):
                return self.send(HTTPStatus.UNAUTHORIZED, {"error": "รหัสผ่านเดิมไม่ถูกต้อง"})
            cfg["salt"] = secrets.token_hex(16)
            cfg["password_hash"] = hash_password(new, cfg["salt"])
            save_config(cfg)
            cookie = (f"owl_admin={make_token(cfg)}; Path={BASE_PATH}/; HttpOnly; "
                      f"SameSite=Lax; Secure; Max-Age={SESSION_HOURS * 3600}")
            return self.send(HTTPStatus.OK, {"ok": True}, cookie=cookie)

        return self.send(HTTPStatus.NOT_FOUND, {"error": "ไม่พบหน้านี้"})


def main():
    if not CONFIG.exists():
        salt = secrets.token_hex(16)
        password = secrets.token_urlsafe(9)
        save_config({"salt": salt, "password_hash": hash_password(password, salt),
                     "secret": secrets.token_hex(32)})
        print(f"สร้างรหัสผ่านเริ่มต้น: {password}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
