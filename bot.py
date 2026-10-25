#!/usr/bin/env python3
"""
ONSITE310S Telegram Bot
-----------------------
1) Inspection report builder (/new)
2) Booking notifications with inline buttons:
   [ Call Client ]  [ Open Map ]  [ Confirm ]
"""

import os
import re
import json
import logging
import tempfile
import asyncio
import threading
from datetime import datetime
from typing import Dict, Any, Optional
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
import pdfplumber
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAPH_ACCESS_TOKEN = os.getenv("TELEGRAPH_ACCESS_TOKEN", "")
# Your personal Telegram chat ID (get it by messaging @userinfobot or from /start)
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states for report builder
(
    WAITING_VEHICLE,
    WAITING_FILES,
    WAITING_NOTES,
    WAITING_CONFIRM,
) = range(4)

# ──────────────────────────────────────────────────────────────
# BOOKING: format message + inline keyboard
# ──────────────────────────────────────────────────────────────
def build_booking_keyboard(phone: str, location: str, date: str, time: str, vehicle: str) -> InlineKeyboardMarkup:
    """Inline buttons under every booking notification."""
    # Normalize phone for tel: link
    phone_digits = re.sub(r"[^\d+]", "", phone or "")
    if phone_digits and not phone_digits.startswith("+"):
        # Assume Canada/US if 10 digits
        if len(phone_digits) == 10:
            phone_digits = "+1" + phone_digits
        elif len(phone_digits) == 11 and phone_digits.startswith("1"):
            phone_digits = "+" + phone_digits

    maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(location or '')}"

    # Google Calendar quick-add style link (opens create event)
    cal_title = quote(f"ONSITE310S Inspection – {vehicle or 'Vehicle'}")
    cal_details = quote(f"Client phone: {phone}\nLocation: {location}\nTime: {time}")
    cal_url = (
        f"https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={cal_title}&details={cal_details}"
    )

    buttons = []

    if phone_digits:
        buttons.append(
            InlineKeyboardButton("📞 Call Client", url=f"tel:{phone_digits}")
        )

    if location:
        buttons.append(
            InlineKeyboardButton("📍 Open Map", url=maps_url)
        )

    # Confirm is a callback so we can mark it handled in chat
    buttons.append(
        InlineKeyboardButton("✅ Confirm", callback_data="booking_confirm")
    )

    # Second row: calendar
    row2 = [
        InlineKeyboardButton("📅 Add to Calendar", url=cal_url)
    ]

    return InlineKeyboardMarkup([buttons, row2])


def format_booking_message(data: Dict[str, str]) -> str:
    """Human-readable booking alert for Telegram."""
    name = data.get("name", "—")
    phone = data.get("phone", "—")
    email = data.get("email", "")
    vehicle = data.get("vehicle", "—")
    location = data.get("location", "—")
    date = data.get("date", "—")
    time = data.get("time", "—")
    notes = data.get("notes", "")

    text = (
        f"🆕 *New Inspection Booking*\n\n"
        f"👤 *Name:* {name}\n"
        f"📱 *Phone:* `{phone}`\n"
    )
    if email:
        text += f"✉️ *Email:* {email}\n"
    text += (
        f"🚗 *Vehicle:* {vehicle}\n"
        f"📍 *Location:* {location}\n"
        f"🗓️ *When:* {date} — {time}\n"
    )
    if notes:
        text += f"\n📝 *Notes:* {notes}\n"
    text += f"\n_Received {datetime.now().strftime('%Y-%m-%d %H:%M')}_"
    return text


async def send_booking_to_owner(context: ContextTypes.DEFAULT_TYPE, data: Dict[str, str]) -> None:
    """Send booking notification with inline buttons to owner."""
    chat_id = OWNER_CHAT_ID or context.bot_data.get("owner_chat_id")
    if not chat_id:
        logger.error("OWNER_CHAT_ID not set and no owner_chat_id stored. Message the bot with /start first.")
        return

    text = format_booking_message(data)
    keyboard = build_booking_keyboard(
        phone=data.get("phone", ""),
        location=data.get("location", ""),
        date=data.get("date", ""),
        time=data.get("time", ""),
        vehicle=data.get("vehicle", ""),
    )
    await context.bot.send_message(
        chat_id=int(chat_id),
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Confirm button — ask for the confirmed appointment time."""
    query = update.callback_query
    await query.answer()

    if query.data != "booking_confirm":
        return

    original = query.message.text or ""
    # Store booking details so we can use them when the owner replies with the time
    context.user_data["pending_booking"] = original
    context.user_data["pending_chat_id"] = query.message.chat_id
    context.user_data["pending_message_id"] = query.message.message_id

    await query.edit_message_text(
        text=original + "\n\n⏳ Awaiting your confirmed time…"
    )

    await query.message.reply_text(
        "What date and time should I confirm with the customer?\n\n"
        "Reply with something like:\n"
        "• Tomorrow at 10:30am\n"
        "• Oct 28 at 2pm\n"
        "• Monday 9am"
    )


async def handle_confirmed_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner replies with the confirmed appointment time."""
    if "pending_booking" not in context.user_data:
        return  # not in a confirmation flow

    confirmed_time = update.message.text.strip()
    original = context.user_data.pop("pending_booking")
    context.user_data.pop("pending_chat_id", None)
    context.user_data.pop("pending_message_id", None)

    # Extract details
    lines = original.splitlines()
    buyer = "there"
    email = ""
    vehicle = "your vehicle"
    for line in lines:
        clean = line.replace("*", "").replace("`", "")
        if "Buyer:" in clean or "Name:" in clean:
            buyer = clean.split(":", 1)[-1].strip()
        if "Email:" in clean or "Buyer Email:" in clean:
            email = clean.split(":", 1)[-1].strip()
        if "Vehicle:" in clean:
            vehicle = clean.split(":", 1)[-1].strip()

    confirm_text = (
        f"Hi {buyer},\n\n"
        f"This is Nigel from ONSITE310S. Your pre-purchase inspection for the {vehicle} is confirmed for:\n\n"
        f"{confirmed_time}\n\n"
        f"I'll see you then. A receipt will also be sent from QuickBooks.\n\n"
        f"Thank you,\n"
        f"Nigel Devonish\n"
        f"ONSITE310S Inc.\n"
        f"Mobile Pre-Purchase Inspections\n"
        f"$200 + tax · Mississauga & GTA"
    )

    # Send email via Resend
    resend_key = os.getenv("RESEND_API_KEY")
    email_sent = False
    if email and resend_key:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {resend_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": "ONSITE310S <bookings@onsite310s.ca>",
                        "to": [email],
                        "subject": f"Inspection Confirmed — {confirmed_time}",
                        "text": confirm_text,
                    },
                    timeout=15,
                )
                if r.status_code in (200, 201):
                    email_sent = True
                else:
                    logger.error(f"Resend error: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"Email send failed: {e}")

    status = "✅ Email sent to the customer." if email_sent else "📋 Copy and send this to the customer:"
    await update.message.reply_text(f"{status}\n\n{confirm_text}")


# ──────────────────────────────────────────────────────────────
# TELEGRAPH HELPERS (report builder)
# ──────────────────────────────────────────────────────────────
async def create_telegraph_account(short_name: str = "ONSITE310S") -> str:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.telegra.ph/createAccount",
            data={"short_name": short_name, "author_name": "ONSITE310S Inc."},
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]["access_token"]
        raise RuntimeError(f"Telegraph account creation failed: {data}")


async def publish_to_telegraph(
    access_token: str,
    title: str,
    markdown_content: str,
    author: str = "ONSITE310S Inc.",
) -> str:
    html = markdown_to_telegraph_html(markdown_content)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.telegra.ph/createPage",
            data={
                "access_token": access_token,
                "title": title[:256],
                "author_name": author,
                "content": html,
                "return_content": "false",
            },
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]["url"]
        raise RuntimeError(f"Telegraph publish failed: {data}")


def markdown_to_telegraph_html(md: str) -> str:
    nodes = []
    for line in md.strip().splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("# "):
            nodes.append({"tag": "h3", "children": [line[2:].strip()]})
        elif line.startswith("## "):
            nodes.append({"tag": "h4", "children": [line[3:].strip()]})
        elif line.strip() == "---":
            nodes.append({"tag": "hr"})
        else:
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
            if text.startswith("* ") or text.startswith("- "):
                nodes.append({"tag": "p", "children": ["• " + text[2:]]})
            else:
                nodes.append({"tag": "p", "children": [text]})
    return json.dumps(nodes)


def extract_text_from_pdf(file_path: str) -> str:
    try:
        with pdfplumber.open(file_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return f"[Could not extract text: {e}]"


def build_report(data: Dict[str, Any]) -> str:
    vehicle = data.get("vehicle", {})
    year = vehicle.get("year", "YYYY")
    make = vehicle.get("make", "MAKE")
    model = vehicle.get("model", "MODEL")
    trim = vehicle.get("trim", "")
    vin = vehicle.get("vin", "XXXXXXXXXXXXXXX")
    odo_scan = vehicle.get("odo_scan", "N/A")
    odo_carfax = vehicle.get("odo_carfax", "N/A")
    owners = vehicle.get("owners", "N/A")
    accident = vehicle.get("accident", "No accidents reported")
    province = vehicle.get("province", "ON")
    title = f"{year} {make} {model} {trim}".strip()

    hist = data.get("history", {})
    batt = data.get("battery", {})
    diag = data.get("diagnostics", {})
    paint = data.get("paint", {})
    notes = data.get("notes") or ["No additional technician notes provided."]
    modules = diag.get("modules", [])

    report = f"""# 🚗 Vehicle Inspection Summary: {title}
* **VIN:** `{vin}`
* **Odometer:** {odo_scan} km (Scan) / {odo_carfax} km (Carfax)
* **History:** {owners}, {accident} ({province})

---

## 1. 📜 History & Documentation (Carfax)
* **Accidents & Damage:** {hist.get('accidents', accident)}
* **Ownership:** {hist.get('ownership', f'{owners} owner(s) · Registered in {province}')}
* **Odometer Note:** {hist.get('odo_note', f'Scan: {odo_scan} km · Carfax last: {odo_carfax} km')}

## 2. ⚡ Electrical & Battery Health
* **Battery Status:** **{batt.get('status', 'Good Battery')} ({batt.get('soh', 'N/A')}% State of Health)**. Measured capacity: {batt.get('measured_cca', 'N/A')} CCA vs {batt.get('rated_cca', 'N/A')} CCA rating.
* **Cranking & Charging:** {batt.get('cranking', 'Normal cranking')} and {batt.get('charging', 'Charging normal')}.

## 3. 🔍 Diagnostic Scan Findings (Autel)
Total Stored/Current Codes: {diag.get('total_codes', 0)} across {len(modules)} modules:
"""
    if modules:
        for mod in modules:
            name = mod.get("name", "Module")
            count = mod.get("count", 0)
            codes = mod.get("codes", [])
            if codes:
                parts = [f"Code `{c.get('code', '?')}` ({c.get('desc', '')})" for c in codes]
                report += f"* **{name} ({count} codes):** {', '.join(parts)}.\n"
            else:
                report += f"* **{name} ({count} codes):** —\n"
    else:
        report += "* No diagnostic trouble codes stored or pending.\n"

    report += f"""
## 4. 🎨 Paint Thickness & Body Panels (NexPTG)
* **Readings Overview:** Measured in μm using {paint.get('substrate', 'Fe / Fe+Zn')} substrate detection.
* **Original Paint Profile:**
  * Hood/Bonnet: {paint.get('hood', 'N/A')} μm
  * Left Side Panels: {paint.get('left', 'N/A')} μm
  * Right Side Panels: {paint.get('right', 'N/A')} μm
  * Roof: {paint.get('roof', 'N/A')} μm
  * Trunk/Boot: {paint.get('trunk', 'N/A')} μm
* **Conclusion:** {paint.get('conclusion', 'See full paint report.')}

---

### 💡 Technician Recommendations / Next Steps
"""
    for i, note in enumerate(notes, 1):
        report += f"{i}. {note}\n"
    report += f"\n---\n*Report generated by ONSITE310S Inc. · {datetime.now().strftime('%Y-%m-%d %H:%M')}*"
    return report.strip()


# ──────────────────────────────────────────────────────────────
# COMMANDS
# ──────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Remember owner chat for booking notifications
    if update.effective_chat:
        context.bot_data["owner_chat_id"] = update.effective_chat.id
        logger.info(f"Owner chat_id set to {update.effective_chat.id}")

    await update.message.reply_text(
        "👋 *ONSITE310S Bot*\n\n"
        "• `/new` — Start a new inspection report\n"
        "• `/chatid` — Show your Telegram chat ID (needed for website bookings)\n"
        "• `/cancel` — Cancel current report\n\n"
        "Booking alerts from the website will appear here with Call / Map / Confirm buttons.",
        parse_mode="Markdown",
    )


async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id if update.effective_chat else "?"
    await update.message.reply_text(
        f"Your chat ID is:\n`{cid}`\n\n"
        f"Add this to your `.env` as:\n`OWNER_CHAT_ID={cid}`",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ──────────────────────────────────────────────────────────────
# REPORT CONVERSATION
# ──────────────────────────────────────────────────────────────
async def new_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["files"] = {}
    context.user_data["notes"] = []
    context.user_data["raw_texts"] = {}
    await update.message.reply_text(
        "🆕 *New Inspection Report*\n\n"
        "Send vehicle details:\n"
        "`Year Make Model Trim | VIN | Odometer_km | Owners | Accident status | Province`\n\n"
        "Example:\n`2019 Honda Civic EX | 2HGFC2F59KH123456 | 87450 | 2 | No accidents | ON`",
        parse_mode="Markdown",
    )
    return WAITING_VEHICLE


async def receive_vehicle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = [p.strip() for p in text.split("|")]
    vehicle = {
        "year": "YYYY", "make": "MAKE", "model": "MODEL", "trim": "",
        "vin": "XXXXXXXXXXXXXXX", "odo_scan": "N/A", "odo_carfax": "N/A",
        "owners": "N/A", "accident": "No accidents reported", "province": "ON",
    }
    if parts:
        tokens = parts[0].split()
        if tokens and tokens[0].isdigit():
            vehicle["year"] = tokens[0]
            if len(tokens) > 1:
                vehicle["make"] = tokens[1]
            if len(tokens) > 2:
                vehicle["model"] = tokens[2]
            if len(tokens) > 3:
                vehicle["trim"] = " ".join(tokens[3:])
    if len(parts) >= 2:
        vehicle["vin"] = parts[1]
    if len(parts) >= 3:
        vehicle["odo_scan"] = parts[2]
    if len(parts) >= 4:
        vehicle["owners"] = parts[3]
    if len(parts) >= 5:
        vehicle["accident"] = parts[4]
    if len(parts) >= 6:
        vehicle["province"] = parts[5]

    context.user_data["vehicle"] = vehicle
    await update.message.reply_text(
        f"✅ Vehicle: *{vehicle['year']} {vehicle['make']} {vehicle['model']}*\n"
        f"VIN: `{vehicle['vin']}`\n\n"
        "Upload PDFs (Carfax, Autel, Battery, Paint). Type *done* when finished.",
        parse_mode="Markdown",
    )
    return WAITING_FILES


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    file_obj = message.document or (message.photo[-1] if message.photo else None)
    if not file_obj:
        await message.reply_text("Please send a PDF or image.")
        return WAITING_FILES

    file_name = getattr(file_obj, "file_name", None) or f"photo_{file_obj.file_id}.jpg"
    tg_file = await file_obj.get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file_name)[1]) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    extracted = ""
    if file_name.lower().endswith(".pdf") or (getattr(file_obj, "mime_type", "") or "").find("pdf") >= 0:
        extracted = extract_text_from_pdf(tmp_path)
        context.user_data["raw_texts"][file_name] = extracted[:15000]

    lower = file_name.lower()
    category = "other"
    if any(k in lower for k in ["carfax", "history"]):
        category = "carfax"
    elif any(k in lower for k in ["autel", "scan", "diagnostic", "dtc"]):
        category = "diagnostics"
    elif any(k in lower for k in ["battery", "batt"]):
        category = "battery"
    elif any(k in lower for k in ["paint", "nexptg", "thickness"]):
        category = "paint"

    context.user_data["files"][category] = {
        "name": file_name, "path": tmp_path, "extracted": extracted[:3000]
    }
    await message.reply_text(f"✅ Received *{file_name}* ({category}). Send more or type *done*.", parse_mode="Markdown")
    return WAITING_FILES


async def receive_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() in ("done", "finish", "ready", "generate"):
        return await ask_confirm(update, context)
    if update.message.voice:
        await update.message.reply_text("🎤 Voice received — please also send a short text summary.")
        return WAITING_NOTES
    context.user_data.setdefault("notes", []).append(text)
    await update.message.reply_text("✅ Note added. Type *done* to generate report.", parse_mode="Markdown")
    return WAITING_NOTES


async def ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = {
        "vehicle": context.user_data.get("vehicle", {}),
        "history": {},
        "battery": {},
        "diagnostics": {"total_codes": 0, "modules": []},
        "paint": {},
        "notes": context.user_data.get("notes", []),
    }
    for cat, info in context.user_data.get("files", {}).items():
        extracted = info.get("extracted", "")
        if cat == "battery" and extracted:
            if "good" in extracted.lower():
                data["battery"]["status"] = "Good Battery"
            m = re.search(r"(\d{1,3})\s*%", extracted)
            if m:
                data["battery"]["soh"] = m.group(1)
        if cat == "diagnostics" and extracted:
            codes = re.findall(r"\b([PCBU]\d{4})\b", extracted)
            if codes:
                data["diagnostics"]["total_codes"] = len(set(codes))
                data["diagnostics"]["modules"] = [{
                    "name": "Detected Modules",
                    "count": len(set(codes)),
                    "codes": [{"code": c, "desc": "See Autel PDF"} for c in sorted(set(codes))[:8]],
                }]

    context.user_data["report_data"] = data
    preview = build_report(data)
    for i in range(0, len(preview), 4000):
        await update.message.reply_text(preview[i:i + 4000])
    await update.message.reply_text(
        "Type *publish* to go live on telegra.ph, or *edit* to add notes.",
        parse_mode="Markdown",
    )
    return WAITING_CONFIRM


async def confirm_and_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if text in ("edit", "notes", "add"):
        await update.message.reply_text("Send more notes, then type *done*.")
        return WAITING_NOTES
    if text not in ("publish", "yes", "ok", "generate", "send"):
        await update.message.reply_text("Type *publish* or *edit*.")
        return WAITING_CONFIRM

    data = context.user_data.get("report_data", {})
    report_md = build_report(data)
    vehicle = data.get("vehicle", {})
    title = f"{vehicle.get('year', '')} {vehicle.get('make', '')} {vehicle.get('model', '')} Inspection".strip()

    access_token = TELEGRAPH_ACCESS_TOKEN
    if not access_token:
        try:
            access_token = await create_telegraph_account()
        except Exception as e:
            await update.message.reply_text(f"Telegraph failed ({e}). Markdown only:\n\n{report_md[:3500]}")
            return ConversationHandler.END

    try:
        url = await publish_to_telegraph(access_token, title, report_md)
        await update.message.reply_text(f"✅ Report published!\n\n🌐 {url}")
    except Exception as e:
        await update.message.reply_text(f"Publish failed ({e}).\n\n{report_md[:3500]}")

    for info in context.user_data.get("files", {}).values():
        try:
            os.unlink(info["path"])
        except Exception:
            pass
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for info in context.user_data.get("files", {}).values():
        try:
            os.unlink(info["path"])
        except Exception:
            pass
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────
# OPTIONAL: receive booking JSON via private message pattern
# (Website should use Cloudflare Worker → Bot API for reliability)
# ──────────────────────────────────────────────────────────────
async def maybe_booking_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If someone sends a JSON booking payload, format it with buttons."""
    text = (update.message.text or "").strip()
    if not text.startswith("{") or "vehicle" not in text.lower():
        return
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return
        # Re-send as formatted booking with buttons
        keyboard = build_booking_keyboard(
            phone=str(data.get("phone", "")),
            location=str(data.get("location", "")),
            date=str(data.get("date", "")),
            time=str(data.get("time", "")),
            vehicle=str(data.get("vehicle", "")),
        )
        await update.message.reply_text(
            format_booking_message({k: str(v) for k, v in data.items()}),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception:
        pass


def main():
    # Force a clean event loop (fixes Python 3.12+ / 3.14 on Render)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Health check so Render Web Service doesn't kill the process
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass

    def run_health():
        try:
            server = HTTPServer(("0.0.0.0", port), HealthHandler)
            logger.info(f"Health check listening on 0.0.0.0:{port}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"Health server failed: {e}")

    threading.Thread(target=run_health, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_report)],
        states={
            WAITING_VEHICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_vehicle)],
            WAITING_FILES: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, receive_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes),
            ],
            WAITING_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes),
                MessageHandler(filters.VOICE, receive_notes),
            ],
            WAITING_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_and_publish)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CallbackQueryHandler(booking_callback, pattern="^booking_"))
    app.add_handler(conv)
    # Handle confirmed appointment time replies (must be before maybe_booking_json)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirmed_time))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, maybe_booking_json))

    logger.info("ONSITE310S Bot starting…")
    # Run polling inside the event loop we created
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
