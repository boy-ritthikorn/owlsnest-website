# OWL'S NEST Bar & Bistro — เว็บไซต์

เว็บไซต์ร้าน OWL'S NEST Bar & Bistro (ปากเกร็ด นนทบุรี) — บาร์ไวน์ อาหารสเปน และดนตรีสด

**เว็บจริง:** https://owlsnestbar.com
**หลังบ้านแก้เมนู/ตารางดนตรี:** https://newton-ritthikornkorjai.incomeinclick.in.th/owl-admin/

---

## เว็บนี้ทำงานยังไง

เว็บเป็น **static** ทั้งหมด (ไฟล์ HTML ธรรมดา ไม่มีฐานข้อมูล) เพื่อให้ Google อ่านเนื้อหาได้ครบ — สำคัญมากเพราะเว็บนี้ทำขึ้นเพื่อ SEO

เวลาแก้เนื้อหา ไม่ได้แก้ `index.html` ตรง ๆ แต่ทำแบบนี้:

```
data.json  ─┐
            ├─→  build.py  ─→  index.html  ─→  /var/www/owlsnest/
template.html ┘
```

- `data.json` — เนื้อหาที่แก้ได้ (ตารางดนตรี, จานเด่น, ไวน์, เมนูอาหาร, เครื่องดื่ม)
- `template.html` — โครงหน้าเว็บ มีจุดเสียบข้อมูล `{{SCHED}}` `{{DISHES}}` `{{WINE}}` `{{MENUNAV}}` `{{MENU}}` `{{DRINKS}}`
- `build.py` — เอาสองอันมารวมกันเป็น `index.html` แล้วก๊อปไปที่เว็บจริง

> ⚠️ **ห้ามเปลี่ยนไปใช้ JavaScript โหลดข้อมูลตอนเปิดหน้า** — Google จะอ่านเนื้อหาไม่เห็น เท่ากับพังเป้าหมายทั้งโปรเจกต์

### คำสั่ง

```bash
python3 build.py          # สร้าง index.html ใหม่ + deploy ขึ้นเว็บจริง
python3 build.py --dry    # ลองสร้างดูเฉย ๆ ไม่ deploy
python3 make_favicon.py   # สร้าง favicon.ico + apple-touch-icon.png ใหม่
```

**หลังแก้ `build.py` หรือ `template.html` ต้องทดสอบเสมอ:** สร้างใหม่แล้วเทียบกับไฟล์เดิม ต้องต่างกันเฉพาะจุดที่ตั้งใจแก้เท่านั้น

```bash
cp index.html /tmp/before.html
python3 build.py --dry > /tmp/after.html
diff /tmp/before.html /tmp/after.html
```

---

## ไฟล์ในโปรเจกต์

| ไฟล์ | คืออะไร |
|---|---|
| `template.html` | โครงหน้าเว็บ + CSS + SEO tags + Meta Pixel |
| `data.json` | เนื้อหาเมนูและตารางดนตรีทั้งหมด |
| `build.py` | ตัวสร้างหน้าเว็บ |
| `index.html` | ผลลัพธ์ที่สร้างออกมา (ไฟล์ที่ deploy จริง) |
| `make_favicon.py` | วาด favicon นกฮูกด้วย Pillow |
| `images/` | รูปที่ใช้บนเว็บ |
| `robots.txt`, `sitemap.xml` | บอก Google ว่ามาเก็บข้อมูลได้ |
| `gbp-checklist.html` | หน้า checklist ตั้งค่า Google Business Profile |
| `admin/app.py` | เซิร์ฟเวอร์หลังบ้าน (Python stdlib ล้วน ไม่มี dependency) |
| `admin/admin.html` | หน้าจอหลังบ้าน ภาษาไทย ใช้บนมือถือได้ |
| `messenger-assistant/` | ระบบรับข้อความ Facebook และสร้างร่างคำตอบให้พนักงานตรวจ |

---

## ค่าตั้งบนเซิร์ฟเวอร์

| หัวข้อ | ค่า |
|---|---|
| โฟลเดอร์เว็บจริง | `/var/www/owlsnest/` |
| หลังบ้าน (systemd) | `owl-admin.service` — พอร์ต `127.0.0.1:5605` |
| nginx โดเมนใหม่ | `/etc/nginx/sites-available/owlsnestbar` |
| nginx ลิงก์เก่า `/owl/` | 301 ไป `https://owlsnestbar.com` |
| SSL | Let's Encrypt (certbot ต่ออายุอัตโนมัติ) |
| Meta Pixel ID | `1078746361515763` |

---

## ไฟล์ที่ไม่ได้เก็บใน repo นี้ (ดู `.gitignore`)

| ไฟล์ | อยู่ที่ไหน / กู้ยังไง |
|---|---|
| `admin/config.json` | **มีรหัสผ่านหลังบ้าน** อยู่บนเซิร์ฟเวอร์เท่านั้น ถ้าหายให้ลบไฟล์แล้วรีสตาร์ท `owl-admin` ระบบจะสร้างรหัสใหม่ให้ |
| `admin/backups/` | ข้อมูลสำรองอัตโนมัติ 30 ครั้งล่าสุด สร้างเองทุกครั้งที่กดบันทึก |
| `drive-src/` | รูปดิบ 100 ไฟล์ (~28 MB) ต้นฉบับอยู่ใน Google Drive ของเจ้าของร้าน |
| `*.bak` | ไฟล์สำรองก่อนแก้ — git เก็บประวัติให้อยู่แล้ว |
