#!/usr/bin/env python3
"""
ONSITE310S Inspector Bot
------------------------
Report builder only (/new).
Bookings are handled by the separate booking bot.
"""

import os
import re
import json
import logging
import tempfile
import asyncio
import threading
import random
import string
from typing import Dict, Any
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
import pdfplumber
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("INSPECTOR_BOT_TOKEN") or os.getenv("BOT_TOKEN")
TELEGRAPH_ACCESS_TOKEN = os.getenv("TELEGRAPH_ACCESS_TOKEN", "")

if not BOT_TOKEN:
    raise RuntimeError("INSPECTOR_BOT_TOKEN (or BOT_TOKEN) is required")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(
    WAITING_VEHICLE,
    WAITING_PDFS,
    WAITING_ID_PHOTOS,
    WAITING_EXTERIOR,
    WAITING_ENGINE_UNDER,
    WAITING_INTERIOR,
    WAITING_WHEELS,
    WAITING_SUMMARY,
    WAITING_OBSERVATIONS,
    WAITING_CONCERNS,
    WAITING_RATING,
) = range(11)


def generate_client_code() -> str:
    return "ONS-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


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


async def publish_to_telegraph(access_token: str, title: str, markdown_content: str) -> str:
    html = markdown_to_telegraph_html(markdown_content)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.telegra.ph/createPage",
            data={
                "access_token": access_token,
                "title": title[:256],
                "author_name": "ONSITE310S Inc.",
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
        return ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 *ONSITE310S Inspector Bot*\n\n"
        "• /new — Start a new inspection report\n"
        "• /cancel — Cancel current report",
        parse_mode="Markdown",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def new_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["pdfs"] = {}
    context.user_data["photos"] = {}
    context.user_data["summary"] = ""
    context.user_data["observations"] = ""
    context.user_data["concerns"] = ""
    context.user_data["rating"] = ""
    await update.message.reply_text(
        "🆕 *New Inspection Report*\n\n"
        "First, enter the *Client Code* from the booking\n"
        "(example: `ONS-A3K9`) or type *new* to generate one.",
        parse_mode="Markdown",
    )
    context.user_data["awaiting_code"] = True
    return WAITING_VEHICLE


async def receive_vehicle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if context.user_data.get("awaiting_code"):
        if text.lower() == "new":
            code = generate_client_code()
        else:
            code = text.upper().replace(" ", "")
            if not code.startswith("ONS-"):
                code = "ONS-" + code
        context.user_data["client_code"] = code
        context.user_data["awaiting_code"] = False
        await update.message.reply_text(
            f"🔖 Client Code: `{code}`\n\n"
            "Now send vehicle details:\n"
            "`Year Make Model Trim | VIN | Odometer_km`\n\n"
            "Example:\n`2019 Honda Civic EX | 2HGFC2F59KH123456 | 87450`",
            parse_mode="Markdown",
        )
        return WAITING_VEHICLE

    parts = [p.strip() for p in text.split("|")]
    vehicle = {"year": "YYYY", "make": "MAKE", "model": "MODEL", "trim": "", "vin": "N/A", "odo": "N/A"}
    if parts:
        tokens = parts[0].split()
        if tokens and tokens[0].isdigit():
            vehicle["year"] = tokens[0]
            if len(tokens) > 1: vehicle["make"] = tokens[1]
            if len(tokens) > 2: vehicle["model"] = tokens[2]
            if len(tokens) > 3: vehicle["trim"] = " ".join(tokens[3:])
    if len(parts) >= 2: vehicle["vin"] = parts[1]
    if len(parts) >= 3: vehicle["odo"] = parts[2]
    context.user_data["vehicle"] = vehicle

    await update.message.reply_text(
        f"✅ Vehicle: *{vehicle['year']} {vehicle['make']} {vehicle['model']}*\n"
        f"Client Code: `{context.user_data.get('client_code')}`\n\n"
        "*Phase 1 — Upload the 4 PDFs:*\n"
        "1. Carfax Report\n"
        "2. Full System Diagnostic Scan\n"
        "3. Battery / Starter / Alternator Test\n"
        "4. Paint Thickness Report\n\n"
        "Send them one by one, then type *done*.",
        parse_mode="Markdown",
    )
    return WAITING_PDFS


async def receive_pdfs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text == "done":
        await update.message.reply_text(
            "*Phase 2 — Identification photos*\n"
            "Send 2 photos:\n"
            "1. VIN plaque / door jamb sticker\n"
            "2. Odometer / dashboard cluster\n\n"
            "Then type *done*.",
            parse_mode="Markdown",
        )
        return WAITING_ID_PHOTOS

    file_obj = update.message.document
    if not file_obj:
        await update.message.reply_text("Please send a PDF or type *done*.", parse_mode="Markdown")
        return WAITING_PDFS

    file_name = file_obj.file_name or "document.pdf"
    tg_file = await file_obj.get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        await tg_file.download_to_drive(tmp.name)
        extracted = extract_text_from_pdf(tmp.name)

    lower = file_name.lower()
    category = "other"
    if any(k in lower for k in ["carfax", "history"]):
        category = "carfax"
    elif any(k in lower for k in ["autel", "scan", "diagnostic"]):
        category = "scan"
    elif any(k in lower for k in ["battery", "alternator", "starter", "batt"]):
        category = "battery"
    elif any(k in lower for k in ["paint", "nexptg", "thickness"]):
        category = "paint"

    context.user_data["pdfs"][category] = {"name": file_name, "path": tmp.name, "text": extracted[:5000]}
    await update.message.reply_text(f"✅ Received *{file_name}*. Send more or type *done*.", parse_mode="Markdown")
    return WAITING_PDFS


async def collect_photos(update, context, phase_name, next_state, next_prompt, key):
    text = (update.message.text or "").strip().lower()
    if text == "done":
        await update.message.reply_text(next_prompt, parse_mode="Markdown")
        return next_state

    file_obj = update.message.photo[-1] if update.message.photo else update.message.document
    if not file_obj:
        await update.message.reply_text(f"Please send a photo for {phase_name}, or type *done*.", parse_mode="Markdown")
        return None

    tg_file = await file_obj.get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        await tg_file.download_to_drive(tmp.name)
        context.user_data["photos"].setdefault(key, []).append(tmp.name)

    count = len(context.user_data["photos"].get(key, []))
    await update.message.reply_text(f"✅ Photo {count} received for {phase_name}. Send more or type *done*.")
    return None


async def receive_id_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await collect_photos(
        update, context, "Identification",
        WAITING_EXTERIOR,
        "*Phase 2 — Exterior photos*\nSend 4 photos: Front, Rear, Left Side, Right Side.\nThen type *done*.",
        "id",
    )
    return result if result is not None else WAITING_ID_PHOTOS


async def receive_exterior(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await collect_photos(
        update, context, "Exterior",
        WAITING_ENGINE_UNDER,
        "*Phase 3 — Engine & Underbody*\nSend 5 photos: 1 Engine Bay + 4 Underbody.\nThen type *done*.",
        "exterior",
    )
    return result if result is not None else WAITING_EXTERIOR


async def receive_engine_under(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await collect_photos(
        update, context, "Engine & Underbody",
        WAITING_INTERIOR,
        "*Phase 3 — Interior & Cargo*\nSend 5 photos: 4 Seats + 1 Trunk/Cargo.\nThen type *done*.",
        "engine_under",
    )
    return result if result is not None else WAITING_ENGINE_UNDER


async def receive_interior(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await collect_photos(
        update, context, "Interior",
        WAITING_WHEELS,
        "*Phase 3 — Rims & Tire Depths*\nSend 8 photos: 4 Rims/Wheels + 4 Tire Tread Depth Gauge readings.\nThen type *done*.",
        "interior",
    )
    return result if result is not None else WAITING_INTERIOR


async def receive_wheels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await collect_photos(
        update, context, "Wheels & Tires",
        WAITING_SUMMARY,
        "*Phase 4 — AI Inspection Summary*\nEnter your summary of what occurred during the inspection.\nType *done* when finished.",
        "wheels",
    )
    return result if result is not None else WAITING_WHEELS


async def receive_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() == "done":
        await update.message.reply_text(
            "*Phase 4 — Test Drive / Inspection Observations*\n"
            "Enter your observations.\nType *done* when finished.",
            parse_mode="Markdown",
        )
        return WAITING_OBSERVATIONS
    context.user_data["summary"] = (context.user_data.get("summary", "") + "\n" + text).strip()
    await update.message.reply_text("✅ Added. Type *done* to continue.")
    return WAITING_SUMMARY


async def receive_observations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() == "done":
        await update.message.reply_text(
            "*Phase 4 — Key Concerns & Findings*\n"
            "Enter any concerning or urgent issues.\nType *done* when finished.",
            parse_mode="Markdown",
        )
        return WAITING_CONCERNS
    context.user_data["observations"] = (context.user_data.get("observations", "") + "\n" + text).strip()
    await update.message.reply_text("✅ Added. Type *done* to continue.")
    return WAITING_OBSERVATIONS


async def receive_concerns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() == "done":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Good Condition", callback_data="rating_good")],
            [InlineKeyboardButton("🟡 Fair Condition", callback_data="rating_fair")],
            [InlineKeyboardButton("🔴 Poor Condition", callback_data="rating_poor")],
        ])
        await update.message.reply_text(
            "*Phase 5 — Overall Condition Rating*\nSelect one:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return WAITING_RATING
    context.user_data["concerns"] = (context.user_data.get("concerns", "") + "\n" + text).strip()
    await update.message.reply_text("✅ Added. Type *done* to continue.")
    return WAITING_CONCERNS


async def receive_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rating_map = {
        "rating_good": "🟢 Good Condition",
        "rating_fair": "🟡 Fair Condition",
        "rating_poor": "🔴 Poor Condition",
    }
    context.user_data["rating"] = rating_map.get(query.data, "Not rated")
    await query.edit_message_text(f"Rating: {context.user_data['rating']}")
    return await compile_report(update, context)


async def compile_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vehicle = context.user_data.get("vehicle", {})
    client_code = context.user_data.get("client_code", "ONS-????")
    title = f"{vehicle.get('year','')} {vehicle.get('make','')} {vehicle.get('model','')}".strip()

    report = f"# ONSITE310S Inspection Report\n## {title}\n"
    report += f"**Client Code:** {client_code}\n"
    report += f"**VIN:** {vehicle.get('vin','N/A')}\n"
    report += f"**Odometer:** {vehicle.get('odo','N/A')} km\n"
    report += f"**Overall Rating:** {context.user_data.get('rating','N/A')}\n\n"
    report += "## Inspection Summary\n" + context.user_data.get("summary", "None provided.") + "\n\n"
    report += "## Test Drive / Observations\n" + context.user_data.get("observations", "None provided.") + "\n\n"
    report += "## Key Concerns & Findings\n" + context.user_data.get("concerns", "None noted.") + "\n\n"
    report += "## Documents\n"
    for cat, info in context.user_data.get("pdfs", {}).items():
        report += f"- {cat.title()}: {info['name']}\n"
    photo_count = sum(len(v) for v in context.user_data.get("photos", {}).values())
    report += f"\n## Photos Collected: {photo_count}\n"

    msg = update.callback_query.message if update.callback_query else update.message
    try:
        token = TELEGRAPH_ACCESS_TOKEN or await create_telegraph_account()
        url = await publish_to_telegraph(token, f"{client_code} — {title}", report)
        await msg.reply_text(
            f"✅ Report ready\n🔖 Client Code: `{client_code}`\n🔗 {url}\n\n"
            f"Share this link via Telegram, WhatsApp, or email."
        )
    except Exception as e:
        logger.error(f"Telegraph failed: {e}")
        await msg.reply_text(f"Report ready (link failed):\n🔖 {client_code}\n\n{report[:3500]}")

    context.user_data.clear()
    return ConversationHandler.END


def main():
    # Health-check server so Render free Web Service stays alive
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass

    def run_health():
        try:
            server = HTTPServer(("0.0.0.0", port), HealthHandler)
            server.serve_forever()
        except Exception as e:
            logger.error(f"Health server failed: {e}")

    threading.Thread(target=run_health, daemon=True).start()
    logger.info(f"Health check listening on port {port}")

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_report)],
        states={
            WAITING_VEHICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_vehicle)],
            WAITING_PDFS: [
                MessageHandler(filters.Document.ALL, receive_pdfs),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pdfs),
            ],
            WAITING_ID_PHOTOS: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_id_photos),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_id_photos),
            ],
            WAITING_EXTERIOR: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_exterior),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exterior),
            ],
            WAITING_ENGINE_UNDER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_engine_under),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_engine_under),
            ],
            WAITING_INTERIOR: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_interior),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_interior),
            ],
            WAITING_WHEELS: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_wheels),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_wheels),
            ],
            WAITING_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_summary)],
            WAITING_OBSERVATIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_observations)],
            WAITING_CONCERNS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_concerns)],
            WAITING_RATING: [CallbackQueryHandler(receive_rating, pattern="^rating_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    logger.info("ONSITE310S Inspector Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
