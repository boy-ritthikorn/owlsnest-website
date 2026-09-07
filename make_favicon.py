#!/usr/bin/env python3
"""สร้าง favicon นกฮูกสำหรับเว็บ Owl's Nest (โทนดำ-ทองให้เข้ากับเว็บ)

ใช้: python3 make_favicon.py   -> สร้าง favicon.ico + apple-touch-icon.png แล้ว deploy
"""
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
DEPLOY_DIRS = [Path("/var/www/owlsnest"), Path("/root/downloads/owlsnest-website")]

S = 1024                # วาดใหญ่แล้วค่อยย่อ ขอบจะเนียน
BG = (10, 10, 11, 255)  # --bg ของเว็บ
GOLD = (200, 164, 92, 255)  # --gold ของเว็บ


def draw_owl():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=190, fill=BG)

    # กระจุกขนหูอยู่ค่อนไปทางกลางหัว (ถ้าอยู่ริมสุดจะกลายเป็นหน้าแมว)
    d.polygon([(300, 330), (392, 132), (486, 300)], fill=GOLD)
    d.polygon([(724, 330), (632, 132), (538, 300)], fill=GOLD)

    # หัวนกฮูก กว้างและแบนกว่าตัว
    d.ellipse([150, 262, 874, 900], fill=GOLD)

    # ดวงตาโต ชิดกัน = เอกลักษณ์ของนกฮูก
    for cx in (356, 668):
        d.ellipse([cx - 112, 404, cx + 112, 628], fill=BG)
        d.ellipse([cx - 56, 468, cx + 56, 564], fill=GOLD)

    # จะงอยปากเล็ก ๆ ระหว่างตา
    d.polygon([(512, 556), (476, 664), (548, 664)], fill=BG)

    return img


def main():
    owl = draw_owl()

    ico = ROOT / "favicon.ico"
    owl.resize((256, 256), Image.LANCZOS).save(
        ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )

    touch = ROOT / "apple-touch-icon.png"
    owl.resize((180, 180), Image.LANCZOS).convert("RGB").save(touch, optimize=True)

    for target in DEPLOY_DIRS:
        if target.is_dir():
            for f in (ico, touch):
                shutil.copy2(f, target / f.name)

    print(f"favicon.ico {ico.stat().st_size:,} bytes · apple-touch-icon.png {touch.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
