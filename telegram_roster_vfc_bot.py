import os
import re
import io
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from PyPDF2 import PdfReader

# =========================
# STORAGE SIMPLE
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
# CLASIFICACIÓN
# =========================

def classify_day(vfc, sleep):
    if vfc >= 53 and sleep >= 7:
        return "🟢 VERDE"
    elif vfc <= 49 or sleep < 6:
        return "🔴 ROJO"
    else:
        return "🟡 AMARILLO"

# =========================
# FRMS
# =========================

def frms_analysis(vfc, sleep, roster_text):
    risk = "🟢 LOW"

    if sleep < 5:
        risk = "🔴 HIGH"
    elif sleep < 6:
        risk = "🟡 MODERATE"

    early = "No"
    if roster_text and re.search(r"0[0-5]:", roster_text):
        early = "Sí (inicio temprano)"

    return f"""
⚠️ FRMS:
Riesgo: {risk}
Early start: {early}

Impacto:
- ↓ atención
- ↓ reacción
- ↑ errores si no gestionas energía
"""

# =========================
# NUTRICIÓN (TIME BLOCKING)
# =========================

def nutrition_plan(state):
    if state == "🔴 ROJO":
        return """
🍽️ NUTRICIÓN (RECUPERACIÓN)

07:00 Hidratación + electrolitos  
08:00 Proteína + grasa ligera  
12:00 Comida ligera  
16:00 Snack (fruta + proteína)  
19:00 Cena ligera (proteína + verduras)
"""
    elif state == "🟢 VERDE":
        return """
🍽️ NUTRICIÓN (ALTO RENDIMIENTO)

Desayuno completo + carbs  
Comida fuerte  
Post-entreno proteína + carbs  
Cena ligera
"""
    else:
        return """
🍽️ NUTRICIÓN (BALANCE)

Desayuno limpio  
Comida balanceada  
Snack ligero  
Cena ligera
"""

# =========================
# ENTRENAMIENTO
# =========================

def training_plan(state):
    if state == "🔴 ROJO":
        return "🏋️ Recovery (caminar + movilidad)"
    elif state == "🟢 VERDE":
        return "🏋️ HIIT / Freeletics"
    else:
        return "🏋️ Entrenamiento moderado"

# =========================
# COMANDOS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
🤖 Pilot Performance OS

Comandos:

1. Sube tu roster PDF
2. /vfc 50
3. /sleep 4.1 53
4. /day
""")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await context.bot.get_file(update.message.document.file_id)
    pdf_bytes = await file.download_as_bytearray()

    text = extract_text(pdf_bytes)
    flights = extract_flights(text)

    USER_DATA[update.effective_user.id] = {
        "roster": flights,
        "roster_text": text
    }

    await update.message.reply_text("✅ Roster cargado")

async def vfc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_DATA.setdefault(update.effective_user.id, {})
    USER_DATA[update.effective_user.id]["vfc"] = int(context.args[0])
    await update.message.reply_text("✅ VFC guardada")

async def sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_DATA.setdefault(update.effective_user.id, {})
    USER_DATA[update.effective_user.id]["sleep"] = float(context.args[0])
    USER_DATA[update.effective_user.id]["sleep_score"] = int(context.args[1])
    await update.message.reply_text("✅ Sueño guardado")

async def day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in USER_DATA:
        await update.message.reply_text("Sube primero tu roster")
        return

    data = USER_DATA[user_id]

    vfc = data.get("vfc")
    sleep = data.get("sleep")
    roster = data.get("roster", [])
    roster_text = data.get("roster_text", "")

    if not vfc or not sleep:
        await update.message.reply_text("Faltan datos: /vfc y /sleep")
        return

    state = classify_day(vfc, sleep)

    response = f"""
🧠 ESTADO: {state}
VFC: {vfc} ms
Sueño: {sleep} h

✈️ ROSTER:
{roster[0] if roster else "No detectado"}

{frms_analysis(vfc, sleep, roster_text)}

🏋️ ENTRENAMIENTO:
{training_plan(state)}

{nutrition_plan(state)}

😴 PRIORIDAD:
Dormir 7–8h hoy
"""

    await update.message.reply_text(response)

# =========================
# MAIN
# =========================

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vfc", vfc))
    app.add_handler(CommandHandler("sleep", sleep))
    app.add_handler(CommandHandler("day", day))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    app.run_polling()

if __name__ == "__main__":
    main()
