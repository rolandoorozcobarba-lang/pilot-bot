import os
import re
import io
import logging
from datetime import datetime
from typing import List, Optional

from PyPDF2 import PdfReader
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    DocumentHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)

# =========================
# GLOBAL STATE (simple)
# =========================
USER_DATA = {}

# =========================
# PDF PARSER
# =========================

def extract_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text


def extract_flights(text):
    flights = []
    lines = text.split("\n")

    for line in lines:
        if re.search(r"Y\d+", line):
            flights.append(line.strip())

    return flights


# =========================
# LOGIC
# =========================

def classify_day(vfc, sleep):
    if vfc >= 53 and sleep >= 7:
        return "🟢 VERDE"
    elif vfc <= 49 or sleep < 6:
        return "🔴 ROJO"
    else:
        return "🟡 AMARILLO"


def frms_analysis(vfc, sleep):
    if sleep < 5:
        return "🔴 HIGH FATIGUE RISK\n- sueño insuficiente\n- menor atención\n- mayor probabilidad de error"
    elif sleep < 6:
        return "🟡 MODERATE FATIGUE\n- fatiga leve\n- monitorear carga"
    else:
        return "🟢 LOW RISK"


def nutrition_plan(state):
    if state == "🔴 ROJO":
        return """🍽️ NUTRICIÓN
07:00 hidratación + electrolitos
08:00 proteína + grasa ligera
12:00 comida ligera
16:00 snack
19:00 cena ligera"""
    elif state == "🟢 VERDE":
        return """🍽️ NUTRICIÓN
Desayuno completo + carbs
Comida fuerte
Post-entreno proteína
Cena ligera"""
    else:
        return """🍽️ NUTRICIÓN
Balanceado + limpio"""


def training_plan(state):
    if state == "🔴 ROJO":
        return "🏋️ Solo caminar / movilidad"
    elif state == "🟢 VERDE":
        return "🏋️ HIIT / Freeletics"
    else:
        return "🏋️ Entrenamiento moderado"


# =========================
# HANDLERS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot listo 🚀\n\n"
        "1. Sube tu PDF del roster\n"
        "2. Usa:\n"
        "/status 50 4.1"
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await context.bot.get_file(update.message.document.file_id)
    pdf_bytes = await file.download_as_bytearray()

    text = extract_text(pdf_bytes)
    flights = extract_flights(text)

    USER_DATA[update.effective_user.id] = {
        "roster": flights
    }

    await update.message.reply_text("✅ Roster cargado")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in USER_DATA:
        await update.message.reply_text("Primero sube tu roster PDF")
        return

    try:
        vfc = int(context.args[0])
        sleep = float(context.args[1])
    except:
        await update.message.reply_text("Formato: /status 50 4.1")
        return

    state = classify_day(vfc, sleep)

    response = f"""
🧠 ESTADO: {state}
VFC: {vfc} ms
Sueño: {sleep} h

✈️ ROSTER:
{USER_DATA[user_id]['roster'][0] if USER_DATA[user_id]['roster'] else "No detectado"}

⚠️ FRMS:
{frms_analysis(vfc, sleep)}

🏋️ ENTRENAMIENTO:
{training_plan(state)}

{nutrition_plan(state)}
"""

    await update.message.reply_text(response)


# =========================
# MAIN
# =========================

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(DocumentHandler(filters.Document.PDF, handle_pdf))

    app.run_polling()


if __name__ == "__main__":
    main()
