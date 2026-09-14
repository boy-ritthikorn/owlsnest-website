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
CREDENTIAL_NOTE = Path("/root/owlsnest-assistant-admin.txt")
DB_FILE = ROOT / "data" / "assistant.db"
KNOWLEDGE_FILE = ROOT / "knowledge.json"
MUSIC_FILE = Path("/root/owlsnest-website/data.json")
GRAPH_VERSION = "v23.0"
CLINIC_PAGE_ID = "704108133083131"
CLINIC_ATTRIBUTION_URL = "http://127.0.0.1:5602/facebook-attribution-ingest"


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
            CREATE TABLE IF NOT EXISTS reply_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            );
            CREATE INDEX IF NOT EXISTS idx_reply_examples_status_time
                ON reply_examples(status, updated_at DESC);
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


def approved_reply_examples(limit: int = 12) -> list[dict[str, str]]:
    with db() as conn:
        rows = conn.execute(
            """SELECT outgoing.text AS staff_text,
                      COALESCE((SELECT incoming.text FROM messages incoming
                                WHERE incoming.psid=outgoing.psid
                                  AND incoming.direction='in'
                                  AND (incoming.created_at < outgoing.created_at
                                       OR (incoming.created_at = outgoing.created_at AND incoming.id < outgoing.id))
                                ORDER BY incoming.created_at DESC, incoming.id DESC LIMIT 1), '') AS customer_text
               FROM reply_examples example
               JOIN messages outgoing ON outgoing.id=example.message_id
               WHERE example.status='approved'
               ORDER BY example.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"customer": row["customer_text"], "staff": row["staff_text"]} for row in rows]


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
        "จำนวนคน ชื่อ และเบอร์โทรทีละเรื่อง และบอกว่าพนักงานจะยืนยันอีกครั้ง "
        "approved_staff_examples ใช้เลียนแบบสำนวนเท่านั้น ห้ามใช้เป็นแหล่งข้อมูลราคา โปรโมชั่น เวลา หรือโต๊ะว่าง"
    )
    body = {
        "model": config("OPENAI_MODEL", "gpt-5.6-luna"),
        "instructions": instructions,
        "input": json.dumps(
            {
                "restaurant_facts": load_knowledge(),
                "approved_staff_examples": approved_reply_examples(),
                "conversation": recent_history(psid),
            },
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


def validate_openai_key(api_key: str) -> None:
    response = requests.get(
        f"https://api.openai.com/v1/models/{config('OPENAI_MODEL', 'gpt-5.6-luna')}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    if not response.ok:
        try:
            message = response.json().get("error", {}).get("message", "ตรวจสอบ API key ไม่สำเร็จ")
        except ValueError:
            message = "ตรวจสอบ API key ไม่สำเร็จ"
        raise RuntimeError(message)


def save_openai_key(api_key: str) -> None:
    if not api_key.startswith("sk-") or len(api_key) < 20:
        raise ValueError("รูปแบบ OpenAI API key ไม่ถูกต้อง")
    validate_openai_key(api_key)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    updated = []
    found = False
    for line in lines:
        if line.startswith("OPENAI_API_KEY="):
            updated.append(f"OPENAI_API_KEY='{api_key}'")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"OPENAI_API_KEY='{api_key}'")
    temp = ENV_FILE.with_suffix(".tmp")
    temp.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, ENV_FILE)
    CFG["OPENAI_API_KEY"] = api_key


def replace_env_values(values: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    updated = []
    remaining = dict(values)
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in remaining:
            updated.append(f"{key}='{remaining.pop(key)}'")
        else:
            updated.append(line)
    updated.extend(f"{key}='{value}'" for key, value in remaining.items())
    temp = ENV_FILE.with_suffix(".tmp")
    temp.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, ENV_FILE)
    CFG.update(values)


def valid_setup_token(token: str) -> bool:
    expected = config("SETUP_TOKEN")
    try:
        expires = int(config("SETUP_EXPIRES", "0"))
    except ValueError:
        return False
    return bool(token and expected) and time.time() < expires and hmac.compare_digest(token, expected)


def save_admin_password(token: str, password: str, confirmation: str) -> None:
    if not valid_setup_token(token):
        raise ValueError("ลิงก์นี้หมดอายุหรือถูกใช้ไปแล้ว")
    if password != confirmation:
        raise ValueError("รหัสผ่านทั้งสองช่องไม่ตรงกัน")
    if len(password) < 12:
        raise ValueError("รหัสผ่านต้องมีอย่างน้อย 12 ตัวอักษร")
    if not (any(x.islower() for x in password) and any(x.isupper() for x in password)
            and any(x.isdigit() for x in password) and any(not x.isalnum() for x in password)):
        raise ValueError("รหัสผ่านต้องมีตัวพิมพ์ใหญ่ ตัวพิมพ์เล็ก ตัวเลข และสัญลักษณ์")
    replace_env_values({"ADMIN_USER": "owladmin", "ADMIN_PASSWORD": password, "SETUP_TOKEN": "", "SETUP_EXPIRES": "0"})
    CREDENTIAL_NOTE.write_text(
        "OWL'S NEST Messenger Assistant\n"
        "URL: https://newton-ritthikornkorjai.incomeinclick.in.th/owl-assistant/\n"
        "Username: owladmin\n"
        f"Password: {password}\n",
        encoding="utf-8",
    )
    CREDENTIAL_NOTE.chmod(0o600)


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
        answered_by_team = conn.execute(
            """SELECT 1 FROM messages incoming
               JOIN messages outgoing ON outgoing.psid=incoming.psid
                 AND outgoing.direction='out' AND outgoing.source='facebook_team'
                 AND outgoing.id > incoming.id
               WHERE incoming.id=? LIMIT 1""",
            (message_id,),
        ).fetchone()
        if answered_by_team:
            status, reason, error = "answered_elsewhere", "ทีมตอบจาก Facebook แล้ว", ""
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


def record_team_echo(event: dict) -> int | None:
    message = event.get("message", {})
    our_app_id = config("META_APP_ID")
    if our_app_id and str(message.get("app_id", "")) == our_app_id:
        return None
    psid = str(event.get("recipient", {}).get("id", ""))
    mid = str(message.get("mid", ""))
    raw_text = str(message.get("text", "")).strip()
    if not psid or not mid:
        return None
    text = raw_text or "[พนักงานส่งรูปภาพ สติกเกอร์ หรือไฟล์จาก Facebook]"
    stamp = now()
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO messages(mid, psid, sender_name, direction, text, created_at, source) VALUES(?,?,?,?,?,?,?)",
                (mid, psid, "ทีม OWL'S NEST", "out", text, stamp, "facebook_team"),
            )
        except sqlite3.IntegrityError:
            return None
        message_id = int(cur.lastrowid)
        conn.execute(
            """UPDATE drafts SET status='answered_elsewhere', reason='ทีมตอบจาก Facebook แล้ว',
                       error='', updated_at=?
               WHERE status IN ('pending', 'waiting_setup')
                 AND message_id IN (
                     SELECT id FROM messages WHERE psid=? AND direction='in' AND id < ?
                 )""",
            (stamp, psid, message_id),
        )
        if raw_text:
            conn.execute(
                "INSERT INTO reply_examples(message_id, status, created_at, updated_at) VALUES(?, 'pending', ?, ?)",
                (message_id, stamp, stamp),
            )
    return message_id


def process_event(event: dict) -> None:
    message = event.get("message", {})
    if message.get("is_echo"):
        record_team_echo(event)
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


def clinic_attribution_payload(page_id: str, event: dict) -> dict | None:
    """Return ad referral metadata only; deliberately exclude message content."""
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    postback = event.get("postback") if isinstance(event.get("postback"), dict) else {}
    referral = event.get("referral")
    if not isinstance(referral, dict):
        referral = message.get("referral")
    if not isinstance(referral, dict):
        referral = postback.get("referral")
    if not isinstance(referral, dict):
        return None
    ad_id = str(referral.get("ad_id", "")).strip()
    psid = str(event.get("sender", {}).get("id", "")).strip()
    if not psid or not ad_id or not ad_id.isdigit():
        return None
    timestamp = event.get("timestamp", now() * 1000)
    mid = str(message.get("mid", "")).strip()
    event_key = mid or "referral-" + safe_id(
        f"{page_id}:{psid}:{ad_id}:{timestamp}:{referral.get('ref', '')}"
    )
    return {
        "page_id": page_id,
        "psid": psid,
        "timestamp": timestamp,
        "event_key": event_key,
        "ad_id": ad_id,
        "source": str(referral.get("source", ""))[:80],
        "type": str(referral.get("type", ""))[:80],
        "ref": str(referral.get("ref", ""))[:500],
    }


def forward_clinic_attribution(page_id: str, event: dict) -> None:
    payload = clinic_attribution_payload(page_id, event)
    secret = config("META_APP_SECRET")
    if not payload or not secret:
        return
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    try:
        requests.post(
            CLINIC_ATTRIBUTION_URL,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Clinic-Attribution-Signature": signature,
            },
            timeout=25,
        ).raise_for_status()
    except Exception as exc:
        print(f"clinic attribution forward failed: {type(exc).__name__}")


def process_page_event(page_id: str, event: dict) -> None:
    if page_id == config("META_PAGE_ID"):
        process_event(event)
    elif page_id == CLINIC_PAGE_ID:
        forward_clinic_attribution(page_id, event)


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


def reply_examples_for_dashboard() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """SELECT example.id, example.status, outgoing.text AS staff_text,
                      COALESCE((SELECT incoming.text FROM messages incoming
                                WHERE incoming.psid=outgoing.psid
                                  AND incoming.direction='in'
                                  AND (incoming.created_at < outgoing.created_at
                                       OR (incoming.created_at = outgoing.created_at AND incoming.id < outgoing.id))
                                ORDER BY incoming.created_at DESC, incoming.id DESC LIMIT 1), '') AS customer_text
               FROM reply_examples example
               JOIN messages outgoing ON outgoing.id=example.message_id
               WHERE example.status='pending'
               ORDER BY example.updated_at DESC LIMIT 50"""
        ).fetchall()


def page(body: str, title: str = "ผู้ช่วยตอบแชต OWL'S NEST", include_notice: bool = True) -> bytes:
    setup = not bool(config("OPENAI_API_KEY"))
    notice = '<div class="notice">ยังไม่ได้เชื่อม OpenAI API — ระบบรับข้อความได้ แต่ยังสร้างร่างคำตอบไม่ได้ · <a href="settings">ตั้งค่าตอนนี้</a></div>' if setup and include_notice else ""
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
.notice a,a{{color:#f1cf86}}label{{display:block;margin:15px 0 7px;color:var(--muted)}}
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
        if row["status"] == "pending":
            actions = '<div class="actions"><button class="send" type="submit">ตรวจแล้ว ส่งให้ลูกค้า</button><button class="regen" type="submit" formaction="regenerate">สร้างร่างใหม่</button></div>'
        elif row["status"] == "waiting_setup":
            actions = '<div class="actions"><button class="regen" type="submit" formaction="regenerate">สร้างร่างใหม่</button></div>'
        else:
            actions = '<p class="meta">รายการนี้ถูกจัดการแล้ว จึงปิดปุ่มส่งเพื่อป้องกันข้อความซ้ำ</p>'
        cards.append(f"""<section class="card"><div class="meta"><b>{name}</b><span>สถานะ: {status}</span><span class="{confidence}">ความมั่นใจ: {confidence}</span></div>
<div class="customer">{customer_text}</div><form method="post" action="send"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="draft_id" value="{row['id']}">
<textarea name="reply" placeholder="ร่างคำตอบ">{reply}</textarea>{f'<p class="meta">เหตุผลที่ควรให้คนตรวจ: {reason}</p>' if reason else ''}
{actions}</form></section>""")
    if not cards:
        cards.append('<div class="empty"><p>ยังไม่มีข้อความใหม่</p><form method="post" action="simulate"><input type="hidden" name="csrf" value="'+csrf+'"><input name="message" placeholder="พิมพ์ข้อความลูกค้าเพื่อทดสอบ"><div class="actions"><button class="regen" type="submit">ทดลองสร้างร่าง</button></div></form></div>')
    examples = []
    for row in reply_examples_for_dashboard():
        customer_text = html.escape(row["customer_text"] or "ไม่พบข้อความก่อนหน้า")
        staff_text = html.escape(row["staff_text"])
        examples.append(f"""<section class="card"><div class="meta"><b>ตัวอย่างคำตอบจากทีม · รอตรวจ</b></div>
<div class="customer">ลูกค้า: {customer_text}</div><p>ทีมตอบ: {staff_text}</p>
<form method="post" action="example-approve"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="example_id" value="{row['id']}">
<div class="actions"><button class="send" type="submit">ใช้เป็นตัวอย่างให้ AI</button><button class="regen" type="submit" formaction="example-reject">ไม่ใช้คำตอบนี้</button></div></form></section>""")
    if examples:
        example_body = "".join(examples)
    else:
        example_body = '<section class="card empty"><p><b>ยังไม่มีคำตอบจากทีมที่รอตรวจ</b></p><p>เมื่อน้องในทีมตอบลูกค้าจาก Facebook โดยตรง รายการจะปรากฏตรงนี้</p></section>'
    example_section = '<h2>เรียนรู้จากคำตอบของทีม</h2><p class="sub">ระบบจะใช้เฉพาะคำตอบที่บอยอนุมัติเป็นตัวอย่างด้านสำนวน ไม่ใช้แทนข้อมูลราคา โปรโมชั่น หรือการยืนยันโต๊ะ</p>' + example_body
    return page("".join(cards) + example_section)


def settings_page() -> bytes:
    csrf = html.escape(config("CSRF_TOKEN"), quote=True)
    state = "เชื่อมต่อแล้ว" if config("OPENAI_API_KEY") else "ยังไม่ได้เชื่อมต่อ"
    body = f"""<section class="card"><p>สถานะ OpenAI API: <b>{state}</b></p>
<p class="meta">API key จะส่งผ่าน HTTPS และบันทึกในไฟล์ลับบนเซิร์ฟเวอร์ ไม่เก็บใน GitHub</p>
<form method="post" action="settings"><input type="hidden" name="csrf" value="{csrf}">
<label for="api_key">OpenAI API key</label><input id="api_key" name="api_key" type="password" autocomplete="off" placeholder="sk-..." required>
<div class="actions"><button class="send" type="submit">ตรวจสอบและบันทึก</button></div></form>
<p><a href="./">← กลับหน้าร่างคำตอบ</a></p></section>"""
    return page(body, "ตั้งค่า OpenAI API")


def one_time_setup_page(token: str) -> bytes:
    if not valid_setup_token(token):
        return page('<div class="notice">ลิงก์นี้หมดอายุหรือถูกใช้ไปแล้ว กรุณาขอลิงก์ใหม่</div>', "ลิงก์หมดอายุ", False)
    escaped = html.escape(token, quote=True)
    body = f"""<section class="card"><h2>ตั้งรหัสหลังบ้านครั้งแรก</h2>
<p class="meta">Username จะเป็น <b>owladmin</b> กรุณาตั้งรหัสที่ไม่ซ้ำกับบัญชีอื่น</p>
<form method="post" action="setup"><input type="hidden" name="token" value="{escaped}">
<label for="password">รหัสผ่านใหม่</label><input id="password" name="password" type="password" autocomplete="new-password" minlength="12" required>
<label for="confirmation">ยืนยันรหัสผ่านอีกครั้ง</label><input id="confirmation" name="confirmation" type="password" autocomplete="new-password" minlength="12" required>
<p class="meta">อย่างน้อย 12 ตัว มีตัวพิมพ์ใหญ่ ตัวพิมพ์เล็ก ตัวเลข และสัญลักษณ์</p>
<div class="actions"><button class="send" type="submit">ตั้งรหัสและปิดลิงก์นี้</button></div></form></section>"""
    return page(body, "ตั้งรหัสหลังบ้าน", False)


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
        if url.path == "/setup":
            token = urllib.parse.parse_qs(url.query).get("token", [""])[0]
            status = 200 if valid_setup_token(token) else 410
            self.send_bytes(status, one_time_setup_page(token))
            return
        if url.path in ("/", ""):
            if self.require_auth():
                self.send_bytes(200, dashboard())
            return
        if url.path == "/settings":
            if self.require_auth():
                self.send_bytes(200, settings_page())
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
                page_id = str(entry.get("id", ""))
                for event in entry.get("messaging", []):
                    threading.Thread(target=process_page_event, args=(page_id, event), daemon=True).start()
            return

        if url.path == "/setup":
            form = self.read_form()
            try:
                save_admin_password(form.get("token", ""), form.get("password", ""), form.get("confirmation", ""))
            except Exception as exc:
                self.send_bytes(400, page(f'<div class="notice">{html.escape(str(exc))}</div>', "ตั้งรหัสไม่สำเร็จ", False))
                return
            self.send_bytes(200, page('<section class="card"><h2>ตั้งรหัสสำเร็จแล้ว</h2><p>ลิงก์นี้ถูกปิดแล้ว กรุณาเข้า Dashboard ด้วย Username <b>owladmin</b> และรหัสที่เพิ่งตั้ง</p><p><a href="./">เข้าสู่ Dashboard →</a></p></section>', "ตั้งรหัสสำเร็จ", False))
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
                    row = conn.execute("SELECT message_id, psid, status FROM drafts JOIN messages ON messages.id=drafts.message_id WHERE drafts.id=?", (draft_id,)).fetchone()
                if row and reply:
                    if row["status"] != "pending":
                        raise ValueError("ร่างนี้ถูกจัดการแล้ว ระบบจึงไม่ส่งซ้ำ")
                    mid = send_message(row["psid"], reply)
                    stamp = now()
                    with db() as conn:
                        conn.execute("UPDATE drafts SET reply=?, status='sent', error='', updated_at=? WHERE id=?", (reply, stamp, draft_id))
                        conn.execute("INSERT OR IGNORE INTO messages(mid, psid, direction, text, created_at, source) VALUES(?,?,?,?,?,?)", (mid or f"sent-{time.time_ns()}", row["psid"], "out", reply, stamp, "assistant_dashboard"))
            elif url.path in ("/example-approve", "/example-reject"):
                example_id = int(form["example_id"])
                status = "approved" if url.path == "/example-approve" else "rejected"
                with db() as conn:
                    conn.execute("UPDATE reply_examples SET status=?, updated_at=? WHERE id=? AND status='pending'", (status, now(), example_id))
            elif url.path == "/settings":
                save_openai_key(form.get("api_key", "").strip())
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
