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

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# FRASES
# =========================
STOIC_QUOTES = [
    "Domina la primera reacción.",
    "La disciplina es libertad.",
    "Haz lo necesario hoy.",
]

ECC_QUOTES = [
    "La prudencia protege al hombre.",
    "El sabio escucha antes de hablar.",
    "La paciencia da fruto.",
]


def get_quotes(date):
    idx = datetime.strptime(date, "%Y-%m-%d").timetuple().tm_yday
    return STOIC_QUOTES[idx % len(STOIC_QUOTES)], ECC_QUOTES[idx % len(ECC_QUOTES)]


# =========================
# UTILS
# =========================
def now():
    return datetime.now(ZoneInfo("America/Mexico_City"))


def today():
    return now().strftime("%Y-%m-%d")


def hhmm_to_hours(h):
    a, b = h.split(":")
    return int(a) + int(b) / 60


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


def get_tomorrow_key():
    return (now() + timedelta(days=1)).strftime("%b %d").upper()


def find_today_assignment(roster):
    key = now().strftime("%b %d").upper()
    return roster.get(key)


def find_tomorrow_assignment(roster):
    return roster.get(get_tomorrow_key())


def extract_time(text):
    if not text:
        return None
    m = re.search(r"(\d{2}:\d{2})", text)
    return m.group(1) if m else None


# =========================
# INTELIGENCIA
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

    if sum(sleep) / len(sleep) < 6:
        return "deuda de sueño"

    return "estable"


def predict(vfc, sleep):
    if sleep < 5:
        return "mañana probablemente en rojo"
    if vfc < 48:
        return "fatiga acumulándose"
    return "estable"


def detect_risk(vfc, sleep, trend):
    if sleep < 5 and vfc < 48:
        return "alto"
    if trend == "fatiga creciente":
        return "medio"
    return "bajo"


def sleep_strategy(next_assignment):
    if not next_assignment:
        return None

    t = extract_time(next_assignment.get("raw"))
    if not t:
        return None

    h, m = map(int, t.split(":"))
    checkin = h * 60 + m

    wake = checkin - 120
    sleep = wake - (7 * 60)

    return {
        "checkin": t,
        "sleep_time": f"{sleep//60:02d}:{sleep%60:02d}",
        "wake_time": f"{wake//60:02d}:{wake%60:02d}",
    }


# =========================
# IA (FIX IMPORTANTE)
# =========================
def generate_ai(payload):
    stoic, ecc = get_quotes(payload["date"])

    if not client:
        return "❌ No hay API KEY configurada"

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Eres un coach de rendimiento de élite para pilotos. Habla humano, claro y útil."
                },
                {
                    "role": "user",
                    "content": f"""
Analiza esto:

{json.dumps(payload, indent=2)}

Da:
- interpretación clara
- impacto real
- decisiones clave
- estrategia de descanso considerando mañana

Termina con:
🪶 {stoic}
📖 {ecc}
"""
                }
            ],
            temperature=0.9
        )

        return response.choices[0].message.content

    except Exception as e:
        return f"❌ ERROR IA: {str(e)}"


# =========================
# TELEGRAM
# =========================
async def start(update, context):
    await update.message.reply_text("Bot listo 🚀")


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
    await update.message.reply_text("¿VFC?")


async def capture(update, context):
    user = str(update.effective_user.id)
    state = USER_DATA.get(user, {}).get("state")

    if not state:
        return

    txt = update.message.text.strip()

    if state == "vfc":
        USER_DATA[user]["temp"]["vfc"] = int(txt)
        USER_DATA[user]["state"] = "sleep"
        await update.message.reply_text("Sueño hh:mm?")
        save()
        return

    if state == "sleep":
        USER_DATA[user]["temp"]["sleep"] = txt
        USER_DATA[user]["temp"]["sleep_hours"] = hhmm_to_hours(txt)
        USER_DATA[user]["state"] = "score"
        await update.message.reply_text("Score?")
        save()
        return

    if state == "score":
        temp = USER_DATA[user]["temp"]
        temp["score"] = int(txt)

        USER_DATA.setdefault(user, {}).setdefault("metrics_by_day", {})
        USER_DATA[user]["metrics_by_day"][temp["date"]] = temp

        trend = analyze_trend(USER_DATA[user])
        prediction = predict(temp["vfc"], temp["sleep_hours"])
        state_label = "🟢" if temp["vfc"] > 52 else "🔴" if temp["vfc"] < 49 else "🟡"

        tomorrow = find_tomorrow_assignment(USER_DATA[user].get("roster", {}))
        sleep_plan = sleep_strategy(tomorrow)

        payload = {
            **temp,
            "state": state_label,
            "trend": trend,
            "prediction": prediction,
            "risk": detect_risk(temp["vfc"], temp["sleep_hours"], trend),
            "tomorrow": tomorrow,
            "sleep_plan": sleep_plan
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
