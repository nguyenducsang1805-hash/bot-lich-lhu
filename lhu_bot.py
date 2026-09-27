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

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# ================= 2. BỘ KẾT NỐI VÀ BÓC TÁCH TRỰC TIẾP TỪ LHU =================
def parse_schedule_item(item, force_off=False):
    item_id = str(item.get("ID") or item.get("NhomID") or "")
    subject = str(item.get("TenMonHoc") or "Môn học").strip()
    room = str(item.get("TenPhong") or "Chưa rõ phòng").strip()
    facility = str(item.get("TenCoSo") or "").strip()
    if facility:
        room = f"{room} ({facility})"
        
    teacher = str(item.get("GiaoVien") or "").strip()
    group = str(item.get("TenNhom") or "").strip()
    status = item.get("TinhTrang", 0)
    note = str(item.get("GhiChu") or item.get("Note") or item.get("TenLoai") or "").strip()
    
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

    # Kiểm tra trực tiếp trạng thái Báo Nghỉ từ trường LHU
    all_text = f"{subject} {room} {note}".lower()
    is_off = force_off
    if (
        item.get("IsNghi") is True or 
        item.get("DaNghi") is True or
        item.get("LoaiLich") in [2, "2", "Nghi", "BaoNghi"] or
        item.get("TrangThai") in [2, "2", "Nghi"] or
        status not in [0, 1] or
        "báo nghỉ" in all_text or
        "nghỉ" in all_text or
        "hoãn" in all_text
    ):
        is_off = True

    return {
        "id": item_id,
        "subject": subject,
        "room": room,
        "time": time_str if time_str else "Theo thông báo",
        "date": date_str,
        "teacher": teacher,
        "group": group,
        "status": status,
        "note": note,
        "is_off": is_off
    }

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
        parsed_schedule = []

        if isinstance(raw_data, list):
            for table_idx, sub_list in enumerate(raw_data):
                if isinstance(sub_list, list):
                    # Nếu API trả về bảng riêng cho lịch báo nghỉ
                    is_off_table = (table_idx > 0)
                    for item in sub_list:
                        if isinstance(item, dict) and "TenMonHoc" in item:
                            parsed_schedule.append(parse_schedule_item(item, force_off=is_off_table))

        # Ưu tiên ghi nhận trạng thái BÁO NGHỈ
        merged = {}
        for s in parsed_schedule:
            k = f"{s['subject']}_{s['date']}_{s['time']}"
            if k not in merged or s['is_off']:
                merged[k] = s

        return list(merged.values())

    except Exception as e:
        print(f"Lỗi API LHU: {e}")
        return []

def fetch_lhu_schedule(student_id: str):
    """Lấy trực tiếp tuần này và tuần tới"""
    now = datetime.now(VN_TZ)
    current_monday = now - timedelta(days=now.weekday())
    next_monday = current_monday + timedelta(days=7)

    sched_current = fetch_lhu_schedule_week(student_id, current_monday)
    sched_next = fetch_lhu_schedule_week(student_id, next_monday)

    all_items = sched_current + sched_next
    unique_dict = {}
    for x in all_items:
        key = f"{x['subject']}_{x['date']}_{x['time']}"
        if key not in unique_dict or x['is_off']:
            unique_dict[key] = x
    return list(unique_dict.values())

# ================= 3. CÁC TÁC VỤ TỰ ĐỘNG CHẠY THEO GIỜ =================
async def job_morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Tự động chạy 06:00 sáng: CHỈ HIỆN MÔN CẦN ĐI HỌC (Ẩn môn đã báo nghỉ)"""
    now = datetime.now(VN_TZ)
    today_ymd = now.strftime("%Y-%m-%d")
    today_dmy = now.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    # Lọc lấy môn hôm nay mà KHÔNG BỊ BÁO NGHỈ
    today_active_classes = [c for c in schedule if c.get("date") == today_ymd and not c.get("is_off")]

    if not today_active_classes:
        msg = f"📚 <b>LỊCH HỌC HÔM NAY ({today_dmy}):</b>\n🌿 Hôm nay tất cả các môn đều đã báo nghỉ hoặc không có tiết, bạn không có môn nào cần đến trường!"
    else:
        msg = f"📚 <b>LỊCH HỌC HÔM NAY ({today_dmy}):</b>\n\n"
        for idx, c in enumerate(today_active_classes, 1):
            msg += f"<b>{idx}. {c['subject']}</b>\n"
            msg += f"   ⏰ Giờ: <code>{c['time']}</code>\n"
            msg += f"   📍 Phòng: <code>{c['room']}</code>\n"
            if c['teacher']:
                msg += f"   👨‍🏫 GV: {c['teacher']}\n"
            msg += "\n"

    await context.bot.send_message(chat_id=MY_CHAT_ID, text=msg, parse_mode="HTML")

async def job_evening_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Tự động chạy 20:00 tối: BÁO LỊCH NGÀY MAI (Gạch ngang môn báo nghỉ)"""
    tomorrow = datetime.now(VN_TZ) + timedelta(days=1)
    tomorrow_ymd = tomorrow.strftime("%Y-%m-%d")
    tomorrow_dmy = tomorrow.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    tomorrow_classes = [c for c in schedule if c.get("date") == tomorrow_ymd]

    if not tomorrow_classes:
        msg = f"🌙 <b>LỊCH HỌC NGÀY MAI ({tomorrow_dmy}):</b>\nNgày mai bạn không có lịch học!"
    else:
        msg = f"🌙 <b>LỊCH HỌC NGÀY MAI ({tomorrow_dmy}):</b>\n\n"
        for idx, c in enumerate(tomorrow_classes, 1):
            if c.get("is_off"):
                msg += f"> 🛑 <b>{idx}. <s>{c['subject']}</s></b>  👉 <b>[ ĐÃ BÁO NGHỈ ]</b>\n"
                msg += f">    ⏰ Giờ: <s>{c['time']}</s>\n"
                msg += f">    📍 Phòng: <s>{c['room']}</s>\n"
                if c['teacher']:
                    msg += f">    👨‍🏫 GV: {c['teacher']}\n"
            else:
                msg += f"<b>{idx}. {c['subject']}</b>\n"
                msg += f"   ⏰ Giờ: <code>{c['time']}</code>\n"
                msg += f"   📍 Phòng: <code>{c['room']}</code>\n"
                if c['teacher']:
                    msg += f"   👨‍🏫 GV: {c['teacher']}\n"
            msg += "\n"

    await context.bot.send_message(chat_id=MY_CHAT_ID, text=msg, parse_mode="HTML")

# ================= 4. CÁC LỆNH ĐIỀU KHIỂN =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 <b>Bot Calen LHU đã sẵn sàng 24/7!</b>\n\n"
        "• /today : Lịch hôm nay (Chỉ hiện môn cần đi học)\n"
        "• /tomorrow : Lịch ngày mai (Gạch ngang môn báo nghỉ)\n"
        "• /week : Danh sách lịch học sắp tới"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(VN_TZ)
    today_ymd = now.strftime("%Y-%m-%d")
    today_dmy = now.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    today_active_classes = [c for c in schedule if c.get("date") == today_ymd and not c.get("is_off")]

    if not today_active_classes:
        msg = f"📚 <b>LỊCH HỌC HÔM NAY ({today_dmy}):</b>\n🌿 Hôm nay tất cả các môn đều đã báo nghỉ hoặc không có tiết, bạn không có môn nào cần đến trường!"
    else:
        msg = f"📚 <b>LỊCH HỌC HÔM NAY ({today_dmy}):</b>\n\n"
        for idx, c in enumerate(today_active_classes, 1):
            msg += f"<b>{idx}. {c['subject']}</b>\n"
            msg += f"   ⏰ Giờ: <code>{c['time']}</code>\n"
            msg += f"   📍 Phòng: <code>{c['room']}</code>\n"
            if c['teacher']:
                msg += f"   👨‍🏫 GV: {c['teacher']}\n"
            msg += "\n"

    await update.message.reply_text(msg, parse_mode="HTML")

async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tomorrow = datetime.now(VN_TZ) + timedelta(days=1)
    tomorrow_ymd = tomorrow.strftime("%Y-%m-%d")
    tomorrow_dmy = tomorrow.strftime("%d/%m/%Y")

    schedule = fetch_lhu_schedule(STUDENT_ID)
    tomorrow_classes = [c for c in schedule if c.get("date") == tomorrow_ymd]

    if not tomorrow_classes:
        await update.message.reply_text(f"🌿 Ngày mai ({tomorrow_dmy}) bạn không có môn học nào!", parse_mode="HTML")
        return

    msg = f"🌙 <b>LỊCH HỌC NGÀY MAI ({tomorrow_dmy}):</b>\n\n"
    for idx, c in enumerate(tomorrow_classes, 1):
        if c.get("is_off"):
            msg += f"> 🛑 <b>{idx}. <s>{c['subject']}</s></b>  👉 <b>[ ĐÃ BÁO NGHỈ ]</b>\n"
            msg += f">    ⏰ Giờ: <s>{c['time']}</s>\n"
            msg += f">    📍 Phòng: <s>{c['room']}</s>\n"
            if c['teacher']:
                msg += f">    👨‍🏫 GV: {c['teacher']}\n"
        else:
            msg += f"<b>{idx}. {c['subject']}</b>\n"
            msg += f"   ⏰ Giờ: <code>{c['time']}</code>\n"
            msg += f"   📍 Phòng: <code>{c['room']}</code>\n"
            if c['teacher']:
                msg += f"   👨‍🏫 GV: {c['teacher']}\n"
        msg += "\n"

    await update.message.reply_text(msg, parse_mode="HTML")

async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    schedule = fetch_lhu_schedule(STUDENT_ID)
    if not schedule:
        await update.message.reply_text("❌ Không có dữ liệu lịch học.")
        return

    schedule.sort(key=lambda x: (x['date'], x['time']))

    msg = "📅 <b>DANH SÁCH LỊCH HỌC SẮP TỚI:</b>\n\n"
    for idx, c in enumerate(schedule, 1):
        try:
            d_display = datetime.strptime(c['date'], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            d_display = c['date']

        if c.get("is_off"):
            msg += f"> 🛑 <b>{idx}. <s>{c['subject']}</s> [BÁO NGHỈ]</b>\n"
            msg += f">    📅 Ngày: <code>{d_display}</code>\n"
            msg += f">    ⏰ Giờ: <s>{c['time']}</s>\n"
            msg += f">    📍 Phòng: <s>{c['room']}</s>\n"
        else:
            msg += f"<b>{idx}. {c['subject']}</b>\n"
            msg += f"   📅 Ngày: <code>{d_display}</code>\n"
            msg += f"   ⏰ Giờ: <code>{c['time']}</code>\n"
            msg += f"   📍 Phòng: <code>{c['room']}</code>\n"
        
        if c['teacher']:
            msg += f"   👨‍🏫 GV: {c['teacher']}\n"
        msg += "\n"

        if len(msg) > 3500:
            await update.message.reply_text(msg, parse_mode="HTML")
            msg = ""

    if msg:
        await update.message.reply_text(msg, parse_mode="HTML")

# ================= 5. WEB SERVER ĐỂ RENDER CHẠY 24/7 =================
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

# ================= 6. KHỞI TẠO VÀ CHẠY BOT =================
if __name__ == "__main__":
    Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("tomorrow", tomorrow_cmd))
    app.add_handler(CommandHandler("tomorow", tomorrow_cmd))
    app.add_handler(CommandHandler("week", week_cmd))

    job_queue = app.job_queue

    # 1. Báo sáng: đúng 06:00 mỗi ngày (Giờ VN)
    job_queue.run_daily(job_morning_reminder, time=time(hour=6, minute=0, tzinfo=VN_TZ), name="morning_job")

    # 2. Báo tối: đúng 20:00 mỗi ngày (Giờ VN)
    job_queue.run_daily(job_evening_reminder, time=time(hour=20, minute=0, tzinfo=VN_TZ), name="evening_job")

    print(">>> Bot Calen LHU đã sẵn sàng chạy ngầm theo giờ chuẩn Việt Nam!")
    app.run_polling()
