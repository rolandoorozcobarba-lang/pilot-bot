# =========================
# IMPORTS
# =========================
import os
import re
import io
import json
from datetime import datetime
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
    "Haz hoy lo que está en tus manos.",
    "Domina la primera reacción.",
    "La disciplina es libertad futura.",
    "Acepta la realidad y trabaja sobre ella.",
    "La calma también es valentía.",
    "Tu carácter se forja en lo incómodo.",
    "No controles el mundo, controla tu respuesta.",
]

ECC_QUOTES = [
    "El sabio escucha antes de hablar.",
    "La prudencia protege al hombre.",
    "La paciencia da fruto en su tiempo.",
    "El corazón humilde aprende con facilidad.",
    "La palabra sabia edifica.",
    "Quien guarda su lengua guarda su alma.",
    "La sabiduría habita en el corazón disciplinado.",
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
# ROSTER (simple)
# =========================
def parse_roster(text):
    data = {}
    for line in text.split("\n"):
        if re.match(r"(JAN|FEB|MAR|APR)", line):
            parts = line.split()
            date = f"{parts[0]} {parts[1]}"
            data[date] = {"raw": line}
    return data


def find_today_assignment(roster):
    key = datetime.now().strftime("%b").upper() + " " + datetime.now().strftime("%d")
    return roster.get(key)


# =========================
# INTELIGENCIA
# =========================
def analyze_trend(user_block):
    metrics = user_block.get("metrics_by_day", {})
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


def predict_tomorrow(vfc, sleep):
    if sleep < 5:
        return "alto riesgo de rojo"
    if vfc < 48:
        return "fatiga acumulada"
    return "estable"


def detect_risk(vfc, sleep, trend):
    if sleep < 5 and vfc < 48:
        return "riesgo alto"
    if trend == "fatiga creciente":
        return "riesgo acumulado"
    if sleep < 6:
        return "riesgo moderado"
    return "bajo"


def performance_engine(state, trend, sleep):
    if state == "🔴":
        return "recovery total"
    if sleep < 6:
        return "entrenamiento ligero"
    if trend == "fatiga creciente":
        return "bajar carga"
    return "entrenamiento fuerte"


def build_profile(user_block):
    metrics = user_block.get("metrics_by_day", {})
    dates = sorted(metrics.keys())[-14:]

    if len(dates) < 5:
        return "sin datos"

    low_sleep = sum(1 for d in dates if metrics[d]["sleep_hours"] < 6)
    return {"low_sleep_ratio": round(low_sleep / len(dates), 2)}


# =========================
# AI
# =========================
def generate_ai(payload):
    stoic, ecc = get_quotes(payload["date"])

    system = """
Eres un coach de rendimiento de élite para pilotos.

Habla como humano.
No checklist.
Prioriza lo importante.

Analiza:
- estado
- tendencia
- riesgo

Da:
- interpretación clara
- impacto real
- decisiones clave

Cierra con frases.
"""

    user = f"""
Analiza:

{json.dumps(payload, indent=2, ensure_ascii=False)}

Incluye:
🪶 {stoic}
📖 {ecc}
"""

    try:
        res = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.9,
        )
        return res.output_text
    except:
        return f"""
Estado: {payload['state']}

Hoy prioriza recuperación.

🪶 {stoic}
📖 {ecc}
"""


def next_level():
    return """

🚀 SIGUIENTE NIVEL

🧠 AI adaptativa:
aprende tus patrones
detecta días peligrosos

📊 dashboard:
VFC vs sueño vs fatiga

🧬 performance engine:
cuándo entrenar
cuándo bajar carga
cuándo hacer recovery
"""


# =========================
# TELEGRAM
# =========================
async def start(update, context):
    await update.message.reply_text("Bot listo 🚀 usa /plan")


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
    USER_DATA[user]["state"] = None

    save()
    await update.message.reply_text("Roster cargado ✅")


async def plan(update, context):
    user = str(update.effective_user.id)

    USER_DATA.setdefault(user, {})

    if "roster" not in USER_DATA[user]:
        await update.message.reply_text("Primero súbeme tu roster PDF.")
        return

    USER_DATA[user]["state"] = "vfc"
    USER_DATA[user]["temp"] = {"date": today()}

    save()
    await update.message.reply_text("¿Cuál es tu VFC de 7 días?")


async def capture(update, context):
    user = str(update.effective_user.id)
    state = USER_DATA.get(user, {}).get("state")

    if not state:
        return

    txt = update.message.text.strip()

    if state == "vfc":
        try:
            USER_DATA[user]["temp"]["vfc"] = int(txt)
        except ValueError:
            await update.message.reply_text("Pásame solo el número de VFC. Ejemplo: 50")
            return

        USER_DATA[user]["state"] = "sleep"
        await update.message.reply_text("¿Cuántas horas dormiste? Escríbelo en hh:mm, por ejemplo 04:05")
        save()
        return

    if state == "sleep":
        if not re.fullmatch(r"\d{1,2}:\d{2}", txt):
            await update.message.reply_text("Formato inválido. Escríbelo como hh:mm, por ejemplo 04:05")
            return

        USER_DATA[user]["temp"]["sleep"] = txt
        USER_DATA[user]["temp"]["sleep_hours"] = hhmm_to_hours(txt)
        USER_DATA[user]["state"] = "score"
        await update.message.reply_text("¿Cuál fue tu score de sueño?")
        save()
        return

    if state == "score":
        try:
            USER_DATA[user]["temp"]["score"] = int(txt)
        except ValueError:
            await update.message.reply_text("Pásame solo el número del score. Ejemplo: 53")
            return

        temp = USER_DATA[user]["temp"]

        USER_DATA.setdefault(user, {}).setdefault("metrics_by_day", {})
        USER_DATA[user]["metrics_by_day"][temp["date"]] = temp

        trend = analyze_trend(USER_DATA[user])
        prediction = predict_tomorrow(temp["vfc"], temp["sleep_hours"])
        state_label = "🟢" if temp["vfc"] > 52 else "🔴" if temp["vfc"] < 49 else "🟡"

        payload = {
            **temp,
            "state": state_label,
            "trend": trend,
            "prediction": prediction,
            "risk": detect_risk(temp["vfc"], temp["sleep_hours"], trend),
            "performance": performance_engine(state_label, trend, temp["sleep_hours"]),
            "profile": build_profile(USER_DATA[user]),
            "assignment": find_today_assignment(USER_DATA[user].get("roster", {})),
        }

        text = generate_ai(payload)
        text += next_level()

        USER_DATA[user]["state"] = None
        USER_DATA[user]["temp"] = {}
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
