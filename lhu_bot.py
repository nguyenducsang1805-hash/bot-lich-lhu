import json
import asyncio
import os
from datetime import datetime, timedelta, time, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import requests
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= 1. CẤU HÌNH HỆ THỐNG =================
TELEGRAM_BOT_TOKEN = "8856605368:AAFI2s8Ma3GlPoJAC19rpLxlibO-7LwmU3U"
MY_CHAT_ID = "8060536211"
STUDENT_ID = "125000887"
AUTH_TOKEN = "Bearer UZy69OUHzvzsDXilDrjmcJFQ7dzMCAyYjkiyzGoplvY.9FDB39487782900E"

CACHE_FILE = "lhu_schedule_cache.json"
VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# ================= 2. BỘ KẾT NỐI API CALEN LHU =================
def fetch_lhu_schedule_week(student_id: str, monday_dt: datetime):
    url = "https://tapi.lhu.edu.vn/calen/auth/XemLich_LichSinhVien"
    monday_iso = monday_dt.strftime("%Y-%m-%dT00:00:00.000Z")

    payload = {
        "StudentID": str(student_id),
        "Ngay": monday_iso,
        "PageIndex": 1,
        "PageSize": 100
    }

    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Authorization": AUTH_TOKEN,
        "Origin": "https://calen.lhu.edu.vn",
        "Referer": "https://calen.lhu.edu.vn/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        res_json = response.json()
        
        raw_data = res_json.get("data", [])
        items = []
        if isinstance(raw_data, list):
            for sub_list in raw_data:
                if isinstance(sub_list, list) and len(sub_list) > 0 and "TenMonHoc" in sub_list[0]:
                    items = sub_list
                    break

        parsed_schedule = []
        for item in items:
            item_id = str(item.get("ID") or item.get("NhomID") or "")
            subject = item.get("TenMonHoc", "Môn học")
            room = item.get("TenPhong", "Chưa rõ phòng")
            facility = item.get("TenCoSo", "")
            if facility:
                room = f"{room} ({facility})"
                
            teacher = item.get("GiaoVien", "")
            group = item.get("TenNhom", "")
            status = item.get("TinhTrang", 0)
            
            start_iso = item.get("ThoiGianBD", "")
            end_iso = item.get("ThoiGianKT", "")
            
            date_str = ""
            time_str = ""
            if start_iso:
                dt_part = start_iso.split("T")
                date_str = dt_part[0]
                time_start = dt_part[1][:5] if len(dt_part) > 1 else ""
                time_end = end_iso.split("T")[1][:5] if "T" in end_iso else ""
                time_str = f"{time_start} - {time_end}".strip(" -")
            
            # Nhận diện môn báo nghỉ qua cờ TinhTrang hoặc chữ phòng/môn
            is_off = False
            check_str = (room + " " + subject).lower()
            if status not in [0, 1] or "nghỉ" in check_str or "hoãn" in check_str or "báo nghỉ" in check_str:
                is_off = True

            parsed_schedule.append({
                "id": item_id,
                "subject": subject,
                "room": room,
                "time": time_str if time_str else "Theo thông báo",
                "date": date_str,
                "teacher": teacher,
                "group": group,
                "status": status,
                "is_off": is_off
            })
            
        return parsed_schedule
    except Exception as e:
        print(f"Lỗi API LHU: {e}")
        return []

def fetch_lhu_schedule(student_id: str):
    """Quét cả tuần này và tuần tới để không bỏ sót lịch báo nghỉ sớm"""
    now = datetime.now(VN_TZ)
    current_monday = now - timedelta(days=now.weekday())
    next_monday = current_monday + timedelta(days=7)

    sched_current = fetch_lhu_schedule_week(student_id, current_monday)
    sched_next = fetch_lhu_schedule_week(student_id, next_monday)

    all_items = sched_current + sched_next
    unique_map = {f"{x['subject']}_{x['date']}_{x['time']}": x for x in all_items}
    return list(unique_map.values())

# ================= 3. SO SÁNH & PHÁT HIỆN THAY ĐỔI / BÁO NGHỈ =================
def detect_schedule_changes(old_list, new_list):
    changes = []
    old_map = {f"{x['subject']}_{x['date']}": x for x in old_list}
    new_map = {f"{x['subject']}_{x['date']}": x for x in new_list}

    for key, item in new_map.items():
        if key in old_map:
            old_item = old_map[key]
            
            # 1. Phát hiện chuyển trạng thái sang BÁO NGHỈ -> Cảnh báo ngay
            if not old_item.get("is_off") and item.get("is_off"):
                changes.append(
                    f"🚨 **THÔNG BÁO BÁO NGHỈ TIẾT HỌC!**\n"
                    f"• Môn học: *{item['subject']}*\n"
                    f"   📅 Ngày học: `{item['date']}`\n"
                    f"   ⏰ Khung giờ: `{item['time']}`\n"
                    f"   👨‍🏫 GV: {item['teacher']}\n"
                    f"   ⚠️ **Hệ thống vừa cập nhật: Giảng viên/Nhà trường đã cho nghỉ tiết này!**"
                )
                continue

            # 2. Phát hiện môn học bù / học lại
            if old_item.get("is_off") and not item.get("is_off"):
                changes.append(
                    f"🟢 **THÔNG BÁO CÓ LỊCH HỌC LẠI / HỌC BÙ:**\n"
                    f"• Môn học: *{item['subject']}*\n"
                    f"   📅 Ngày: `{item['date']}`\n"
                    f"   ⏰ Giờ: `{item['time']}`\n"
                    f"   📍 Phòng: `{item['room']}`"
                )
                continue

            # 3. Phát hiện đổi phòng hoặc đổi ca học
            diffs = []
            if old_item['room'] != item['room']:
                diffs.append(f"   📍 Phòng học: `{old_item['room']}` ➔ `{item['room']}`")
            if old_item['time'] != item['time']:
                diffs.append(f"   ⏰ Giờ học: `{old_item['time']}` ➔ `{item['time']}`")
            if diffs:
                changes.append(f"🔄 **CẬP NHẬT LỊCH: {item['subject']}** (Ngày `{item['date']}`):\n" + "\n".join(diffs))
        else:
            # Có môn học mới được thêm vào
            changes.append(
                f"➕ **CÓ MÔN HỌC MỚI ĐƯỢC XẾP LỊCH:**\n"
                f"• Môn: *{item['subject']}*\n"
                f"   📅 Ngày: `{item['date']}`\n"
                f"   ⏰ Giờ: `{item['time']}`\n"
                f"   📍 Phòng: `{item['room']}`"
            )

    for key, item in old_map.items():
        if key not in new_map:
            changes.append(f"❌ **MÔN HỌC ĐÃ BỊ HỦY BỎ:**\n• *{item['subject']}* (Ngày `{item['date']}`)")

    return changes

# ================= 4. CÁC TÁC VỤ TỰ ĐỘNG CHẠY NGẦM =================
async def job_morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Tự động nhắc lịch hôm nay lúc 06:00 sáng"""
    now = datetime.now(VN_TZ)
    today_ymd = now.strftime("%Y-%m-%d")
    today_dmy = now.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    today_classes = [c for c in schedule if c.get("date") == today_ymd]

    if today_classes:
        msg = f"📚 **LỊCH HỌC HÔM NAY ({today_dmy}):**\n\n"
        for idx, c in enumerate(today_classes, 1):
            status_text = " 🛑 *(ĐÃ BÁO NGHỈ)*" if c.get("is_off") else ""
            msg += f"*{idx}. {c['subject']}*{status_text}\n"
            msg += f"   ⏰ Giờ: `{c['time']}`\n"
            msg += f"   📍 Phòng: `{c['room']}`\n"
            if c['teacher']:
                msg += f"   👨‍🏫 GV: {c['teacher']}\n"
            msg += "\n"
        await context.bot.send_message(chat_id=MY_CHAT_ID, text=msg, parse_mode="Markdown")

async def job_evening_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Tự động báo lịch ngày mai lúc 20:00 tối"""
    tomorrow = datetime.now(VN_TZ) + timedelta(days=1)
    tomorrow_ymd = tomorrow.strftime("%Y-%m-%d")
    tomorrow_dmy = tomorrow.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    tomorrow_classes = [c for c in schedule if c.get("date") == tomorrow_ymd]

    if not tomorrow_classes:
        msg = f"🌙 **LỊCH HỌC NGÀY MAI ({tomorrow_dmy}):**\nNgày mai bạn không có lịch học! Tối nay nghỉ ngơi thoải mái nhé."
    else:
        msg = f"🌙 **LỊCH HỌC NGÀY MAI ({tomorrow_dmy}):**\n\n"
        for idx, c in enumerate(tomorrow_classes, 1):
            status_text = " 🛑 *(ĐÃ BÁO NGHỈ)*" if c.get("is_off") else ""
            msg += f"*{idx}. {c['subject']}*{status_text}\n"
            msg += f"   ⏰ Giờ: `{c['time']}`\n"
            msg += f"   📍 Phòng: `{c['room']}`\n"
            if c['teacher']:
                msg += f"   👨‍🏫 GV: {c['teacher']}\n"
            msg += "\n"
        msg += "👉 Chú ý các tiết có đánh dấu báo nghỉ để không phải lên trường nhé!"

    await context.bot.send_message(chat_id=MY_CHAT_ID, text=msg, parse_mode="Markdown")

async def job_auto_check_updates(context: ContextTypes.DEFAULT_TYPE):
    """Tự động quét thay đổi liên tục: Có báo nghỉ là gửi tin ngay tức khắc"""
    current_schedule = fetch_lhu_schedule(STUDENT_ID)
    if not current_schedule:
        return

    old_schedule = []
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
                old_schedule = cached_data.get("schedule", [])
        except Exception:
            old_schedule = []

    if old_schedule:
        changes = detect_schedule_changes(old_schedule, current_schedule)
        if changes:
            alert_msg = "⚡ **THÔNG BÁO KHẨN TỪ NHÀ TRƯỜNG:**\n\n" + "\n\n".join(changes)
            await context.bot.send_message(chat_id=MY_CHAT_ID, text=alert_msg, parse_mode="Markdown")

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"schedule": current_schedule}, f, ensure_ascii=False, indent=2)

# ================= 5. LỆNH ĐIỀU KHIỂN =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 **Bot Calen LHU đã sẵn sàng 24/7!**\n\n"
        "• `/today` : Xem lịch hôm nay\n"
        "• `/tomorrow` : Xem lịch ngày mai\n"
        "• `/week` : Xem lịch học\n"
        "• `/check` : Quét kiểm tra lịch đổi ngay"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(VN_TZ)
    today_ymd = now.strftime("%Y-%m-%d")
    today_dmy = now.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    today_classes = [c for c in schedule if c.get("date") == today_ymd]

    if not today_classes:
        await update.message.reply_text(f"🌿 Hôm nay ({today_dmy}) bạn không có môn học nào!")
        return

    msg = f"📚 **LỊCH HỌC HÔM NAY ({today_dmy}):**\n\n"
    for idx, c in enumerate(today_classes, 1):
        status_text = " 🛑 *(ĐÃ BÁO NGHỈ)*" if c.get("is_off") else ""
        msg += f"*{idx}. {c['subject']}*{status_text}\n"
        msg += f"   ⏰ Giờ: `{c['time']}`\n"
        msg += f"   📍 Phòng: `{c['room']}`\n"
        if c['teacher']:
            msg += f"   👨‍🏫 GV: {c['teacher']}\n"
        msg += "\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tomorrow = datetime.now(VN_TZ) + timedelta(days=1)
    tomorrow_ymd = tomorrow.strftime("%Y-%m-%d")
    tomorrow_dmy = tomorrow.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    tomorrow_classes = [c for c in schedule if c.get("date") == tomorrow_ymd]

    if not tomorrow_classes:
        await update.message.reply_text(f"🌿 Ngày mai ({tomorrow_dmy}) bạn không có môn học nào!")
        return

    msg = f"📚 **LỊCH HỌC NGÀY MAI ({tomorrow_dmy}):**\n\n"
    for idx, c in enumerate(tomorrow_classes, 1):
        status_text = " 🛑 *(ĐÃ BÁO NGHỈ)*" if c.get("is_off") else ""
        msg += f"*{idx}. {c['subject']}*{status_text}\n"
        msg += f"   ⏰ Giờ: `{c['time']}`\n"
        msg += f"   📍 Phòng: `{c['room']}`\n"
        if c['teacher']:
            msg += f"   👨‍🏫 GV: {c['teacher']}\n"
        msg += "\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    schedule = fetch_lhu_schedule(STUDENT_ID)
    if not schedule:
        await update.message.reply_text("❌ Không có dữ liệu lịch học.")
        return

    msg = "📅 **LỊCH HỌC CALEN LHU:**\n\n"
    for idx, c in enumerate(schedule, 1):
        try:
            d_display = datetime.strptime(c['date'], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            d_display = c['date']

        status_text = " 🛑 *(ĐÃ BÁO NGHỈ)*" if c.get("is_off") else ""
        msg += f"*{idx}. {c['subject']}*{status_text}\n"
        msg += f"   📅 Ngày: `{d_display}`\n"
        msg += f"   ⏰ Giờ: `{c['time']}`\n"
        msg += f"   📍 Phòng: `{c['room']}`\n"
        if c['teacher']:
            msg += f"   👨‍🏫 GV: {c['teacher']}\n"
        msg += "\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Đang quét thay đổi với Calen LHU...")
    await job_auto_check_updates(context)
    await update.message.reply_text("✅ Quét xong!")

# ================= 6. WEB SERVER ĐỂ RENDER CHẠY FREE =================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot LHU is running 24/7!")

    def log_message(self, format, *args):
        return

def run_web():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# ================= 7. KHỞI TẠO VÀ CHẠY BOT =================
if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("tomorrow", tomorrow_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("check", check_cmd))

    job_queue = app.job_queue

    # 1. Báo sáng: đúng 06:00 mỗi ngày (Giờ VN)
    job_queue.run_daily(job_morning_reminder, time=time(hour=6, minute=0, tzinfo=VN_TZ), name="morning_job")

    # 2. Báo tối: đúng 20:00 mỗi ngày (Giờ VN)
    job_queue.run_daily(job_evening_reminder, time=time(hour=20, minute=0, tzinfo=VN_TZ), name="evening_job")

    # 3. Quét ngầm mỗi 900 giây (15 phút) - Lần đầu chạy sau 30 giây
    job_queue.run_repeating(job_auto_check_updates, interval=900, first=30, name="update_job")

    print(">>> Bot đã chạy chế độ cảnh báo tức thì theo giờ Việt Nam!")
    app.run_polling()
