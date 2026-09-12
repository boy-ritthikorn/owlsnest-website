#!/usr/bin/env python3
"""OWL'S NEST Messenger assistant — draft-first human review workflow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import sqlite3
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("OWL_ASSISTANT_ENV", "/root/.secrets/owlsnest-messenger-assistant.env"))
DB_FILE = ROOT / "data" / "assistant.db"
KNOWLEDGE_FILE = ROOT / "knowledge.json"
MUSIC_FILE = Path("/root/owlsnest-website/data.json")
GRAPH_VERSION = "v23.0"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


CFG = load_env(ENV_FILE)


def config(key: str, default: str = "") -> str:
    return os.environ.get(key, CFG.get(key, default))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mid TEXT UNIQUE,
                psid TEXT NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
                text TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'facebook'
            );
            CREATE INDEX IF NOT EXISTS idx_messages_psid_time
                ON messages(psid, created_at DESC);
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL UNIQUE,
                reply TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'low',
                needs_human INTEGER NOT NULL DEFAULT 1,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            );
            """
        )


def now() -> int:
    return int(time.time())


def safe_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def graph_request(method: str, path: str, *, payload: dict | None = None) -> dict:
    token = config("META_PAGE_TOKEN")
    if not token:
        raise RuntimeError("ยังไม่ได้ตั้งค่า META_PAGE_TOKEN")
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
    if method == "GET":
        response = requests.get(url, params={**(payload or {}), "access_token": token}, timeout=20)
    else:
        response = requests.post(url, params={"access_token": token}, json=payload or {}, timeout=20)
    data = response.json()
    if not response.ok or "error" in data:
        message = data.get("error", {}).get("message", f"HTTP {response.status_code}")
        raise RuntimeError(f"Meta API: {message}")
    return data


def sender_name(psid: str) -> str:
    try:
        data = graph_request("GET", psid, payload={"fields": "first_name,last_name"})
        return " ".join(x for x in [data.get("first_name", ""), data.get("last_name", "")] if x).strip()
    except Exception:
        return ""


def load_knowledge() -> dict:
    knowledge = json.loads(KNOWLEDGE_FILE.read_text(encoding="utf-8"))
    if MUSIC_FILE.exists():
        try:
            knowledge["live_music"] = json.loads(MUSIC_FILE.read_text(encoding="utf-8"))["music"]
        except (KeyError, json.JSONDecodeError):
            pass
    return knowledge


def recent_history(psid: str, limit: int = 12) -> list[dict[str, str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT direction, text FROM messages WHERE psid=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (psid, limit),
        ).fetchall()
    return [
        {"role": "customer" if row["direction"] == "in" else "staff", "text": row["text"]}
        for row in reversed(rows)
    ]


def extract_output_text(data: dict) -> str:
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                return part.get("text", "")
    raise RuntimeError("OpenAI ไม่ได้ส่งข้อความตอบกลับ")


def generate_reply(psid: str) -> dict:
    api_key = config("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("ยังไม่ได้ตั้งค่า OPENAI_API_KEY")

    instructions = (
        "คุณคือผู้ช่วยแอดมิน OWL'S NEST Bar & Bistro ตอบภาษาเดียวกับลูกค้าแบบอบอุ่น กระชับ "
        "เป็นธรรมชาติ ไม่ขายยัดเยียด ไม่อ้างว่าเป็นมนุษย์ ใช้เฉพาะข้อมูลที่ให้มา ห้ามเดาเวลา ราคา "
        "โต๊ะว่าง หรือการยืนยันจอง หากไม่แน่ใจให้ needs_human=true ถ้าลูกค้าจะจองให้เก็บวัน เวลา "
        "จำนวนคน ชื่อ และเบอร์โทรทีละเรื่อง และบอกว่าพนักงานจะยืนยันอีกครั้ง"
    )
    body = {
        "model": config("OPENAI_MODEL", "gpt-5.6-luna"),
        "instructions": instructions,
        "input": json.dumps(
            {"restaurant_facts": load_knowledge(), "conversation": recent_history(psid)},
            ensure_ascii=False,
        ),
        "reasoning": {"effort": "none"},
        "store": False,
        "max_output_tokens": 350,
        "safety_identifier": safe_id(psid),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "reply_draft",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "reply": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "needs_human": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["reply", "confidence", "needs_human", "reason"],
                    "additionalProperties": False,
                },
            }
        },
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=45,
    )
    data = response.json()
    if not response.ok:
        message = data.get("error", {}).get("message", f"HTTP {response.status_code}")
        raise RuntimeError(f"OpenAI API: {message}")
    return json.loads(extract_output_text(data))


def create_draft(message_id: int, psid: str) -> None:
    stamp = now()
    try:
        result = generate_reply(psid)
        reply = result["reply"].strip()
        confidence = result["confidence"]
        needs_human = 1 if result["needs_human"] else 0
        reason = result["reason"].strip()
        status = "pending"
        error = ""
    except Exception as exc:
        reply, confidence, needs_human, reason = "", "low", 1, "ต้องให้พนักงานตรวจ"
        status, error = "waiting_setup", str(exc)
    with db() as conn:
        conn.execute(
            """INSERT INTO drafts(message_id, reply, confidence, needs_human, reason, status, error, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET reply=excluded.reply, confidence=excluded.confidence,
               needs_human=excluded.needs_human, reason=excluded.reason, status=excluded.status,
               error=excluded.error, updated_at=excluded.updated_at""",
            (message_id, reply, confidence, needs_human, reason, status, error, stamp, stamp),
        )


def record_incoming(psid: str, mid: str, text: str, source: str = "facebook") -> int | None:
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO messages(mid, psid, sender_name, direction, text, created_at, source) VALUES(?,?,?,?,?,?,?)",
                (mid, psid, sender_name(psid) if source == "facebook" else "ทดสอบ", "in", text, now(), source),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def process_event(event: dict) -> None:
    message = event.get("message", {})
    if message.get("is_echo"):
        return
    psid = str(event.get("sender", {}).get("id", ""))
    mid = str(message.get("mid", ""))
    text = str(message.get("text", "")).strip()
    if not psid or not mid:
        return
    if not text:
        text = "[ลูกค้าส่งรูปภาพ สติกเกอร์ หรือไฟล์ — กรุณาเปิดดูใน Facebook]"
    message_id = record_incoming(psid, mid, text)
    if message_id:
        create_draft(message_id, psid)


def send_message(psid: str, text: str) -> str:
    data = graph_request(
        "POST",
        "me/messages",
        payload={"recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": text}},
    )
    return str(data.get("message_id", ""))


def rows_for_dashboard() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """SELECT d.*, m.psid, m.sender_name, m.text AS customer_text, m.created_at AS message_time
               FROM drafts d JOIN messages m ON m.id=d.message_id
               ORDER BY d.updated_at DESC LIMIT 100"""
        ).fetchall()


def page(body: str, title: str = "ผู้ช่วยตอบแชต OWL'S NEST") -> bytes:
    setup = not bool(config("OPENAI_API_KEY"))
    notice = '<div class="notice">ยังไม่ได้เชื่อม OpenAI API — ระบบรับข้อความได้ แต่ยังสร้างร่างคำตอบไม่ได้</div>' if setup else ""
    return f"""<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#0c0c0d;--card:#171719;--gold:#c8a45c;--text:#eee9df;--muted:#aaa49a;--line:#39342d}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,sans-serif}}
.wrap{{max-width:980px;margin:auto;padding:24px 16px 60px}}h1{{font-size:25px;color:var(--gold);margin:0}}.sub{{color:var(--muted);margin:6px 0 20px}}
.notice{{padding:13px 15px;background:#412d0d;border:1px solid #8d672c;border-radius:8px;margin:16px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:14px 0}}.meta{{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:13px}}
.customer{{font-size:17px;margin:12px 0;padding:12px;background:#222225;border-radius:8px}}textarea,input{{width:100%;background:#101012;color:var(--text);border:1px solid #50483c;border-radius:7px;padding:11px;font:inherit}}
textarea{{min-height:88px;resize:vertical}}button{{border:0;border-radius:7px;padding:10px 15px;font-weight:700;cursor:pointer}}.send{{background:var(--gold);color:#111}}.regen{{background:#302c27;color:var(--text)}}
.actions{{display:flex;gap:8px;margin-top:10px}}.high{{color:#79c789}}.medium{{color:#e7c16e}}.low{{color:#ef8585}}.empty{{text-align:center;color:var(--muted);padding:50px 10px}}
@media(max-width:520px){{.actions{{flex-direction:column}}button{{width:100%}}}}
</style></head><body><main class="wrap"><h1>OWL'S NEST · ผู้ช่วยตอบแชต</h1><p class="sub">โหมดร่างคำตอบ — ระบบจะไม่ส่งหาลูกค้าเอง</p>{notice}{body}</main></body></html>""".encode("utf-8")


def dashboard() -> bytes:
    cards = []
    csrf = html.escape(config("CSRF_TOKEN"), quote=True)
    for row in rows_for_dashboard():
        name = html.escape(row["sender_name"] or "ลูกค้า Facebook")
        customer_text = html.escape(row["customer_text"])
        reply = html.escape(row["reply"])
        reason = html.escape(row["reason"] or row["error"])
        status = html.escape(row["status"])
        confidence = html.escape(row["confidence"])
        cards.append(f"""<section class="card"><div class="meta"><b>{name}</b><span>สถานะ: {status}</span><span class="{confidence}">ความมั่นใจ: {confidence}</span></div>
<div class="customer">{customer_text}</div><form method="post" action="send"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="draft_id" value="{row['id']}">
<textarea name="reply" placeholder="ร่างคำตอบ">{reply}</textarea>{f'<p class="meta">เหตุผลที่ควรให้คนตรวจ: {reason}</p>' if reason else ''}
<div class="actions"><button class="send" type="submit">ตรวจแล้ว ส่งให้ลูกค้า</button><button class="regen" type="submit" formaction="regenerate">สร้างร่างใหม่</button></div></form></section>""")
    if not cards:
        cards.append('<div class="empty"><p>ยังไม่มีข้อความใหม่</p><form method="post" action="simulate"><input type="hidden" name="csrf" value="'+csrf+'"><input name="message" placeholder="พิมพ์ข้อความลูกค้าเพื่อทดสอบ"><div class="actions"><button class="regen" type="submit">ทดลองสร้างร่าง</button></div></form></div>')
    return page("".join(cards))


class Handler(BaseHTTPRequestHandler):
    server_version = "OwlAssistant/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # Do not log query strings because Meta verification places its token there.
        safe_path = urllib.parse.urlsplit(self.path).path
        print(f"{self.address_string()} - {self.command} {safe_path}")

    def send_bytes(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        user, password = config("ADMIN_USER"), config("ADMIN_PASSWORD")
        if not user or not password:
            return False
        expected = f"{user}:{password}".encode()
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True)
        except Exception:
            return False
        return hmac.compare_digest(supplied, expected)

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="OWL Assistant"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def read_form(self) -> dict[str, str]:
        length = min(int(self.headers.get("Content-Length", "0")), 64_000)
        values = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        return {key: val[-1] for key, val in values.items()}

    def valid_csrf(self, form: dict[str, str]) -> bool:
        token = config("CSRF_TOKEN")
        return bool(token) and hmac.compare_digest(form.get("csrf", ""), token)

    def redirect_home(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "./")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/health":
            self.send_bytes(200, b'{"ok":true}', "application/json")
            return
        if url.path == "/webhook":
            query = urllib.parse.parse_qs(url.query)
            valid = query.get("hub.mode", [""])[0] == "subscribe" and hmac.compare_digest(
                query.get("hub.verify_token", [""])[0], config("META_VERIFY_TOKEN")
            )
            if valid:
                self.send_bytes(200, query.get("hub.challenge", [""])[0].encode(), "text/plain")
            else:
                self.send_bytes(403, b"Forbidden", "text/plain")
            return
        if url.path in ("/", ""):
            if self.require_auth():
                self.send_bytes(200, dashboard())
            return
        self.send_bytes(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/webhook":
            length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
            raw = self.rfile.read(length)
            signature = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(config("META_APP_SECRET").encode(), raw, hashlib.sha256).hexdigest()
            if not config("META_APP_SECRET") or not hmac.compare_digest(signature, expected):
                self.send_bytes(403, b"Invalid signature", "text/plain")
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self.send_bytes(400, b"Invalid JSON", "text/plain")
                return
            self.send_bytes(200, b"EVENT_RECEIVED", "text/plain")
            for entry in payload.get("entry", []):
                for event in entry.get("messaging", []):
                    threading.Thread(target=process_event, args=(event,), daemon=True).start()
            return

        if not self.require_auth():
            return
        form = self.read_form()
        if not self.valid_csrf(form):
            self.send_bytes(403, b"Invalid CSRF token", "text/plain")
            return
        try:
            if url.path == "/simulate":
                text = form.get("message", "").strip()
                if text:
                    message_id = record_incoming("local-test", f"test-{time.time_ns()}", text, "simulator")
                    if message_id:
                        create_draft(message_id, "local-test")
            elif url.path == "/regenerate":
                draft_id = int(form["draft_id"])
                with db() as conn:
                    row = conn.execute("SELECT message_id, psid FROM drafts JOIN messages ON messages.id=drafts.message_id WHERE drafts.id=?", (draft_id,)).fetchone()
                if row:
                    create_draft(int(row["message_id"]), row["psid"])
            elif url.path == "/send":
                draft_id = int(form["draft_id"])
                reply = form.get("reply", "").strip()
                with db() as conn:
                    row = conn.execute("SELECT message_id, psid FROM drafts JOIN messages ON messages.id=drafts.message_id WHERE drafts.id=?", (draft_id,)).fetchone()
                if row and reply:
                    mid = send_message(row["psid"], reply)
                    stamp = now()
                    with db() as conn:
                        conn.execute("UPDATE drafts SET reply=?, status='sent', error='', updated_at=? WHERE id=?", (reply, stamp, draft_id))
                        conn.execute("INSERT INTO messages(mid, psid, direction, text, created_at) VALUES(?,?,?,?,?)", (mid or f"sent-{time.time_ns()}", row["psid"], "out", reply, stamp))
            else:
                self.send_bytes(404, b"Not found", "text/plain")
                return
        except Exception as exc:
            self.send_bytes(500, page(f'<div class="notice">{html.escape(str(exc))}</div><p><a href="./">กลับหน้าหลัก</a></p>', "เกิดข้อผิดพลาด"))
            return
        self.redirect_home()


def main() -> None:
    init_db()
    host = config("HOST", "127.0.0.1")
    port = int(config("PORT", "5606"))
    print(f"OWL assistant listening on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
