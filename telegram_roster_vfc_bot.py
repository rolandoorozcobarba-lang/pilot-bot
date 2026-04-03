# =========================
# IMPORTS
# =========================
import os
import re
import io
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from PyPDF2 import PdfReader
from openai import OpenAI

# =========================
# CONFIG
# =========================
DATA_FILE = "user_metrics.json"
USER_DATA = {}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# UTILS
# =========================
def now():
    return datetime.now(ZoneInfo("America/Mexico_City"))

def today():
    return now().strftime("%Y-%m-%d")

def hhmm_to_hours(h):
    h, m = map(int, h.split(":"))
    return h + m/60


# =========================
# STORAGE
# =========================
def load():
    global USER_DATA
    if os.path.exists(DATA_FILE):
        USER_DATA = json.load(open(DATA_FILE))

def save():
    json.dump(USER_DATA, open(DATA_FILE, "w"), indent=2)


# =========================
# ROSTER
# =========================
def parse_roster(text):
    data = {}
    for line in text.split("\n"):
        if re.match(r"(JAN|FEB|MAR|APR)", line):
            parts = line.split()
            date = f"{parts[0]} {parts[1]}"
            data[date] = {"raw": line}
    return data

def key_today():
    return now().strftime("%b %d").upper()

def key_tomorrow():
    return (now() + timedelta(days=1)).strftime("%b %d").upper()

def extract_time(text):
    if not text:
        return None
    m = re.search(r"(\d{2}:\d{2})", text)
    return m.group(1) if m else None


# =========================
# FRMS (tipo AIMS simplificado)
# =========================
def frms_level(vfc, sleep, checkin):
    if sleep < 4:
        return "🔥 CRITICAL"
    if sleep < 5 or vfc < 47:
        return "🔴 HIGH"
    if sleep < 6:
        return "🟡 MODERATE"
    return "🟢 LOW"


# =========================
# FATIGA
# =========================
def analyze_trend(user):
    metrics = user.get("metrics_by_day", {})
    dates = sorted(metrics.keys())[-7:]

    if len(dates) < 3:
        return "sin datos"

    vfc = [metrics[d]["vfc"] for d in dates]
    sleep = [metrics[d]["sleep_hours"] for d in dates]

    if vfc[-1] < vfc[0] - 3:
        return "fatiga creciente"

    if sum(sleep)/len(sleep) < 6:
        return "deuda de sueño"

    return "estable"


# =========================
# SLEEP PLAN
# =========================
def sleep_plan(next_assignment):
    if not next_assignment:
        return None

    t = extract_time(next_assignment.get("raw"))
    if not t:
        return None

    h, m = map(int, t.split(":"))
    checkin = h * 60 + m

    wake = checkin - 120
    sleep = wake - (8 * 60)

    return {
        "checkin": t,
        "wake": f"{wake//60:02d}:{wake%60:02d}",
        "sleep": f"{sleep//60:02d}:{sleep%60:02d}"
    }


# =========================
# TIME BLOCKING
# =========================
def build_timeblock(sleep_data):
    if not sleep_data:
        return "Día libre → entrena normal y duerme 7-8h"

    return f"""
Wake: {sleep_data['wake']}
Comida 1: 60-90 min después
Entrenamiento: ligero o movilidad
Comida 2: media jornada
Siesta: 20 min (si aplica)
Cena: 3h antes de dormir
Sleep: {sleep_data['sleep']}
"""


# =========================
# IA
# =========================
def generate_ai(payload):

    system = """
Eres un coach de rendimiento de élite para pilotos.

Analiza como experto en FRMS tipo AIMS.

Incluye:
1. Estado del día
2. Impacto en desempeño
3. Análisis de asignación (si existe)
4. Nivel de riesgo FRMS
5. Decisión del día (clave)
6. Time blocking claro
7. Estrategia de descanso para mañana

No uses formato rígido.
Habla humano, claro y directo.
"""

    user = f"""
Analiza este contexto:

{json.dumps(payload, indent=2)}

Genera un plan completo tipo coach elite.
"""

    try:
        res = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=0.9
        )
        return res.choices[0].message.content

    except Exception as e:
        return f"ERROR IA: {e}"


# =========================
# TELEGRAM FLOW
# =========================
async def start(update, context):
    await update.message.reply_text("Bot listo ✈️ usa /plan")


async def handle_pdf(update, context):
    user = str(update.effective_user.id)

    file = await context.bot.get_file(update.message.document.file_id)
    pdf = await file.download_as_bytearray()

    text = ""
    reader = PdfReader(io.BytesIO(pdf))
    for p in reader.pages:
        text += p.extract_text() or ""

    USER_DATA.setdefault(user, {})
    USER_DATA[user]["roster"] = parse_roster(text)

    save()
    await update.message.reply_text("Roster cargado ✅")


async def plan(update, context):
    user = str(update.effective_user.id)

    USER_DATA.setdefault(user, {})
    USER_DATA[user]["state"] = "vfc"
    USER_DATA[user]["temp"] = {"date": today()}

    save()
    await update.message.reply_text("VFC?")


async def capture(update, context):
    user = str(update.effective_user.id)
    state = USER_DATA.get(user, {}).get("state")

    if not state:
        return

    txt = update.message.text

    if state == "vfc":
        USER_DATA[user]["temp"]["vfc"] = int(txt)
        USER_DATA[user]["state"] = "sleep"
        await update.message.reply_text("Sueño hh:mm?")
        return

    if state == "sleep":
        USER_DATA[user]["temp"]["sleep"] = txt
        USER_DATA[user]["temp"]["sleep_hours"] = hhmm_to_hours(txt)
        USER_DATA[user]["state"] = "score"
        await update.message.reply_text("Score?")
        return

    if state == "score":
        temp = USER_DATA[user]["temp"]
        temp["score"] = int(txt)

        USER_DATA.setdefault(user, {}).setdefault("metrics_by_day", {})
        USER_DATA[user]["metrics_by_day"][temp["date"]] = temp

        roster = USER_DATA[user].get("roster", {})
        today_assignment = roster.get(key_today())
        tomorrow_assignment = roster.get(key_tomorrow())

        sleep_data = sleep_plan(tomorrow_assignment)

        payload = {
            **temp,
            "trend": analyze_trend(USER_DATA[user]),
            "today_assignment": today_assignment,
            "tomorrow_assignment": tomorrow_assignment,
            "sleep_plan": sleep_data,
            "time_blocking": build_timeblock(sleep_data),
            "frms": frms_level(temp["vfc"], temp["sleep_hours"], extract_time(tomorrow_assignment["raw"]) if tomorrow_assignment else None)
        }

        text = generate_ai(payload)

        USER_DATA[user]["state"] = None
        save()

        await update.message.reply_text(text)


# =========================
# MAIN
# =========================
def main():
    load()

    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
