import os
import re
import io
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from PyPDF2 import PdfReader

DATA_FILE = "user_metrics.json"
USER_DATA = {}


# =========================
# PERSISTENCIA
# =========================

def load_data():
    global USER_DATA
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            USER_DATA = json.load(f)
    else:
        USER_DATA = {}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(USER_DATA, f, ensure_ascii=False, indent=2)


def get_user_key(user_id: int) -> str:
    return str(user_id)


# =========================
# PDF
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
# UTILIDADES
# =========================

def hhmm_to_hours(hhmm):
    h, m = hhmm.split(":")
    return int(h) + int(m) / 60


def classify_day(vfc, sleep):
    if vfc >= 53 and sleep >= 7:
        return "🟢 VERDE"
    elif vfc <= 49 or sleep < 6:
        return "🔴 ROJO"
    else:
        return "🟡 AMARILLO"


def frms(vfc, sleep, roster_text):
    risk = "🟢 LOW"

    if sleep < 5:
        risk = "🔴 HIGH"
    elif sleep < 6:
        risk = "🟡 MODERATE"

    early = "No"
    if roster_text and re.search(r"0[0-5]:", roster_text):
        early = "Sí"

    return f"""⚠️ FRMS
Riesgo: {risk}
Early start: {early}
"""


def nutrition(state):
    if state == "🔴 ROJO":
        return "Comer ligero, proteína + verduras, hidratarte mucho."
    elif state == "🟢 VERDE":
        return "Puedes cargar carbs + proteína, día productivo."
    else:
        return "Balanceado y limpio."


def training(state):
    if state == "🔴 ROJO":
        return "Solo caminar / movilidad."
    elif state == "🟢 VERDE":
        return "HIIT / Freeletics."
    else:
        return "Entrenamiento moderado."


def today_local_str():
    return datetime.now(ZoneInfo("America/Mexico_City")).strftime("%Y-%m-%d")


# =========================
# COMANDOS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot listo 🚀\n\n"
        "1. Sube tu roster PDF\n"
        "2. Usa /plan\n"
        "3. Mándame los datos en una sola línea"
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    tg_file = await context.bot.get_file(update.message.document.file_id)
    pdf_bytes = await tg_file.download_as_bytearray()

    text = extract_text(pdf_bytes)
    flights = extract_flights(text)

    USER_DATA.setdefault(user_id, {})
    USER_DATA[user_id]["roster"] = flights
    USER_DATA[user_id]["roster_text"] = text
    USER_DATA[user_id].setdefault("metrics_by_day", {})
    USER_DATA[user_id]["awaiting"] = False

    save_data()

    await update.message.reply_text("Roster cargado ✅")


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    if user_id not in USER_DATA or "roster_text" not in USER_DATA[user_id]:
        await update.message.reply_text("Primero sube tu roster PDF.")
        return

    USER_DATA[user_id]["awaiting"] = True
    save_data()

    await update.message.reply_text(
        "Pásame tus datos en este orden:\n"
        "VFC  sueño(hh:mm)  score de sueño\n\n"
        "Ejemplo:\n"
        "50 04:05 53"
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    if user_id not in USER_DATA or not USER_DATA[user_id].get("metrics_by_day"):
        await update.message.reply_text("No tengo métricas guardadas todavía.")
        return

    metrics = USER_DATA[user_id]["metrics_by_day"]
    dates = sorted(metrics.keys())[-7:]

    lines = ["📊 Últimas métricas guardadas:"]
    for d in dates:
        item = metrics[d]
        lines.append(
            f"{d} → VFC {item['vfc']} | Sueño {item['sleep_hhmm']} | Score {item['score']}"
        )

    await update.message.reply_text("\n".join(lines))


async def export_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    if user_id not in USER_DATA or not USER_DATA[user_id].get("metrics_by_day"):
        await update.message.reply_text("No tengo métricas guardadas para exportar.")
        return

    payload = {
        "user_id": user_id,
        "metrics_by_day": USER_DATA[user_id]["metrics_by_day"]
    }

    file_name = f"metrics_{user_id}.json"
    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(file_name, "rb") as f:
        await update.message.reply_document(document=f, filename=file_name)


# =========================
# CAPTURA DE DATOS
# =========================

async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    if user_id not in USER_DATA:
        return

    if not USER_DATA[user_id].get("awaiting"):
        return

    text = update.message.text.strip()

    try:
        parts = text.split()
        if len(parts) != 3:
            raise ValueError("Formato inválido")

        vfc = int(parts[0])
        sleep_hhmm = parts[1]

        if not re.fullmatch(r"\d{1,2}:\d{2}", sleep_hhmm):
            raise ValueError("Sueño inválido")

        sleep = hhmm_to_hours(sleep_hhmm)
        score = int(parts[2])

    except Exception:
        await update.message.reply_text(
            "Formato inválido.\n"
            "Usa:\n"
            "VFC  sueño(hh:mm)  score\n\n"
            "Ejemplo:\n"
            "50 04:05 53"
        )
        return

    USER_DATA[user_id]["awaiting"] = False

    roster = USER_DATA[user_id].get("roster", [])
    roster_text = USER_DATA[user_id].get("roster_text", "")

    state = classify_day(vfc, sleep)
    today = today_local_str()

    USER_DATA[user_id].setdefault("metrics_by_day", {})
    USER_DATA[user_id]["metrics_by_day"][today] = {
        "vfc": vfc,
        "sleep_hhmm": sleep_hhmm,
        "sleep_hours": round(sleep, 2),
        "score": score,
        "saved_at": datetime.now(ZoneInfo("America/Mexico_City")).isoformat()
    }

    save_data()

    response = f"""🧠 {state}
Fecha: {today}
VFC: {vfc}
Sueño: {sleep_hhmm} ({sleep:.1f}h)
Score: {score}

✈️ {roster[0] if roster else "Sin vuelo detectado"}

{frms(vfc, sleep, roster_text)}

🏋️ {training(state)}

🍽️ {nutrition(state)}

😴 Dormir 7–8h hoy

💾 Métricas guardadas para esta fecha.
"""

    await update.message.reply_text(response)


# =========================
# MAIN
# =========================

def main():
    load_data()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Falta TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("export", export_metrics))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
