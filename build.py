#!/usr/bin/env python3
"""สร้าง index.html จาก template.html + data.json แล้ว deploy ขึ้นเว็บจริง

ใช้: python3 build.py            -> สร้าง + deploy
     python3 build.py --dry      -> สร้างอย่างเดียว พิมพ์ผลออก stdout
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "template.html"
DATA = ROOT / "data.json"
OUTPUT = ROOT / "index.html"
DEPLOY = [Path("/var/www/owlsnest/index.html"), Path("/root/downloads/owlsnest-website/index.html")]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esca(s):
    return esc(s).replace('"', "&quot;")


def item(it, ind):
    th = f"<i>{esc(it['th'])}</i>" if it.get("th") else ""
    return (f'{ind}<div class="item"><span class="nm"><b>{esc(it["en"])}</b>{th}</span>'
            f'<span class="dot"></span><span class="pr">{esc(it["price"])}</span></div>')


def column(groups, ind, gap):
    """คอลัมน์เมนู: หัวข้อย่อย h4 (ถ้ามี) ตามด้วยรายการ"""
    out = []
    for i, g in enumerate(groups):
        if g.get("heading"):
            if gap and i > 0:
                out.append("")
            style = f' style="margin-top:{g["mt"]}px"' if g.get("mt") else ""
            out.append(f'{ind}<h4{style}>{esc(g["heading"])}</h4>')
        out += [item(x, ind) for x in g["items"]]
    return out


def render_sched(days):
    out = []
    for d in days:
        out.append('      <div class="day">')
        out.append(f'        <p class="dow">{esc(d["en"])}</p>')
        out.append(f'        <div class="dow-th">{esc(d["th"])}</div>')
        for s in d["slots"]:
            out.append(f'        <div class="slot"><span class="genre">{esc(s["genre"])}</span>'
                       f'<span class="tm">{esc(s["time"])}</span></div>')
        out.append("      </div>")
    return "\n".join(out)


def render_dishes(dishes):
    out = []
    for d in dishes:
        out.append('      <div class="dish">')
        out.append(f'        <img src="{esca(d["img"])}" alt="{esca(d["alt"])}" loading="lazy" '
                   f'width="{esca(d["w"])}" height="{esca(d["h"])}">')
        out.append('        <div class="cap">')
        out.append(f'          <h3>{esc(d["en"])}</h3>')
        out.append(f'          <div class="th">{esc(d["th"])}</div>')
        out.append(f'          <div class="pr">{esc(d["price"])} บาท</div>')
        out.append("        </div>")
        out.append("      </div>")
    return "\n".join(out)


def render_wine(wine):
    out = ["    <div>"]
    for i, c in enumerate(wine["categories"]):
        if i > 0:
            out.append("")
        style = f' style="margin-top:{c["mt"]}px"' if c.get("mt") else ""
        out.append(f'      <div class="mcat"{style}>')
        out.append(f'        <h4>{esc(c["title"])}</h4>')
        out.append('        <div class="cols2">')
        out += [item(x, "        ") for x in c["items"]]
        out.append("        </div>")
        out.append("      </div>")
    out.append("    </div>")
    out.append(f'    <div class="glass-note">{esc(wine["glass_note"])}</div>')
    return "\n".join(out)


def render_nav(blocks):
    return "\n".join(f'      <a href="#{esca(b["id"])}">{esc(b["nav"])}</a>' for b in blocks)


def render_menu(blocks):
    out = []
    for i, b in enumerate(blocks):
        if i > 0:
            out.append("")
        out.append(f'    <div class="mblock" id="{esca(b["id"])}">')
        out.append(f'      <h3>{esc(b["title"])}</h3>')
        if b.get("sub"):
            out.append(f'      <p class="sub">{esc(b["sub"])}</p>')
        out.append('      <div class="mgrid">')
        for col in b["columns"]:
            out.append('        <div class="mcat">')
            out += column(col, "          ", gap=False)
            out.append("        </div>")
        out.append("      </div>")
        out.append("    </div>")
    return "\n".join(out)


def render_drinks(cols):
    out = []
    for i, col in enumerate(cols):
        if i > 0:
            out.append("")
        out.append('      <div class="mcat">')
        out += column(col, "        ", gap=True)
        out.append("      </div>")
    return "\n".join(out)


def build():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    html = TEMPLATE.read_text(encoding="utf-8")
    for marker, value in {
        "{{SCHED}}": render_sched(data["music"]),
        "{{DISHES}}": render_dishes(data["signature"]),
        "{{WINE}}": render_wine(data["wine"]),
        "{{MENUNAV}}": render_nav(data["menu"]),
        "{{MENU}}": render_menu(data["menu"]),
        "{{DRINKS}}": render_drinks(data["drinks"]),
    }.items():
        if marker not in html:
            raise SystemExit(f"template.html ไม่มี {marker}")
        html = html.replace(marker, value)
    return html


def main():
    html = build()
    if "--dry" in sys.argv:
        sys.stdout.write(html)
        return
    OUTPUT.write_text(html, encoding="utf-8")
    for target in DEPLOY:
        if target.parent.is_dir():
            shutil.copy2(OUTPUT, target)
    print(f"สร้างเว็บใหม่แล้ว {len(html):,} ตัวอักษร")


if __name__ == "__main__":
    main()
