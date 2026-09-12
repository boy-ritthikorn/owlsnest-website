# OWL'S NEST Messenger Assistant

ระบบช่วยร่างคำตอบ Facebook Messenger สำหรับ OWL'S NEST Bar & Bistro

## หลักการ

- รับข้อความจาก Meta webhook และเก็บใน SQLite
- ใช้ OpenAI Responses API สร้างร่างคำตอบแบบ structured JSON
- พนักงานตรวจ แก้ และกดส่งเองจากหน้า `/owl-assistant/`
- ไม่ยืนยันโต๊ะว่างหรือข้อมูลที่ไม่มีในฐานความรู้
- ไม่ส่งข้อความอัตโนมัติในเวอร์ชันแรก

## รัน

```bash
python3 app.py
python3 -m unittest discover -s tests -v
```

Source อยู่ที่ `/root/owlsnest-website/messenger-assistant/` และสำรองไปพร้อม repo เว็บไซต์
ค่าลับอยู่ที่ `/root/.secrets/owlsnest-messenger-assistant.env` และไม่เก็บใน Git

## Production

- Dashboard: `https://newton-ritthikornkorjai.incomeinclick.in.th/owl-assistant/`
- Webhook: `https://newton-ritthikornkorjai.incomeinclick.in.th/owl-assistant/webhook`
- Service: `owl-assistant.service`
- Port: `127.0.0.1:5606`
- Default model: `gpt-5.6-luna`

ระบบจะรับข้อความจริงแล้ว แต่จะไม่สร้างร่างจนกว่าจะใส่ `OPENAI_API_KEY` ที่หน้า `/owl-assistant/settings`
