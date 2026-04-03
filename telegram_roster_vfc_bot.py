import os
import re
import io
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from PyPDF2 import PdfReader

# =========================
# CONFIG
# =========================
DATA_FILE = "data.json"
TZ = "America/Mexico_City"
USER_DATA: Dict[str, Any] = {}

# =========================
# UTILS
# =========================
def now():
    return datetime.now(ZoneInfo(TZ))

def today():
    return now().strftime("%Y-%m-%d")

def tomorrow():
    return (now() + timedelta(days=1)).strftime("%Y-%m-%d")

def hhmm_to_hours(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h + m/60

def hhmm_to_minutes(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h*60 + m

def minutes_to_hhmm(m):
    return f"{m//60:02d}:{m%60:02d}"

def extract_times(text):
    return re.findall(r"\d{2}:\d{2}", text)

# =========================
# STORAGE
# =========================
def load():
    global USER_DATA
    if os.path.exists(DATA_FILE):
        USER_DATA = json.load(open(DATA_FILE))

def save():
    json.dump(USER_DATA, open(DATA_FILE,"w"), indent=2)

# =========================
# ROSTER SIMPLE (ROBUSTO)
# =========================
def parse_roster(text):
    roster = {}
    current = None

    for line in text.split("\n"):
        line = line.strip()

        match = re.match(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{1,2}", line)
        if match:
            current = match.group(0)
            roster[current] = []
            continue

        if current:
            times = extract_times(line)
            if len(times) >= 2:
                dep = times[0]
                arr = times[1]

                t1 = hhmm_to_minutes(dep)
                t2 = hhmm_to_minutes(arr)

                duration = (t2 - t1) % 1440
                roster[current].append(duration/60)

    return roster

def roster_summary(roster):
    total = 0
    heavy = []

    for d, flights in roster.items():
        h = sum(flights)
        total += h
        if h > 6:
            heavy.append(f"{d} ({round(h,1)}h)")

    return round(total,1), heavy

# =========================
# FATIGA
# =========================
def fatigue_score(vfc, sleep):
    score = 100
    if sleep < 6:
        score -= 25
    if vfc < 50:
        score -= 10
    return score

def fatigue_level(score):
    if score < 60: return "🔴 HIGH"
    if score < 80: return "🟡 MODERATE"
    return "🟢 LOW"

def wocl(checkin):
    if not checkin: return "LOW"
    h = int(checkin.split(":")[0])
    if 2 <= h < 6: return "CRITICAL"
    if h < 8: return "MODERATE"
    return "LOW"

# =========================
# PLAN TEXTO (AMIGABLE)
# =========================
def generate_plan(data):

    lvl = data["fatigue_level"]

    text = f"""
Resumen de hoy
Hoy estás en {lvl}. Vienes con buena base, pero el contexto manda.

Fatiga y WOCL
- Fatiga: {data['fatigue_score']} / {lvl}
- WOCL mañana: {data['wocl']}

Lo más importante
Tu prioridad hoy es proteger el descanso.

Movimiento recomendado hoy
"""

    if data["wocl"] in ["CRITICAL","MODERATE"]:
        text += """- Caminata suave 20–30 min
- Movilidad
- Evitar HIIT o cargas pesadas
"""
    else:
        text += """- Entrenamiento moderado
- Fuerza ligera o caminata
"""

    text += f"""

Plan práctico
- Cena ligera
- Dormir: {data['sleep_time']}

Cierre
Hoy no toca empujar, toca prepararte.
"""

    return text

# =========================
# TELEGRAM
# =========================
async def start(update, context):
    await update.message.reply_text(
        "✈️ Bot listo\n\n"
        "1. Sube tu roster\n"
        "2. Usa /plan"
    )

async def handle_pdf(update, context):
    user = str(update.effective_user.id)

    file = await context.bot.get_file(update.message.document.file_id)
    pdf = await file.download_as_bytearray()

    text = ""
    for p in PdfReader(io.BytesIO(pdf)).pages:
        text += p.extract_text() or ""

    roster = parse_roster(text)
    total, heavy = roster_summary(roster)

    USER_DATA[user] = {"roster": roster}
    save()

    await update.message.reply_text(
        f"Roster cargado ✅\n\nTotal: {total}h\n\nDías pesados:\n" +
        ("\n".join(heavy) if heavy else "Ninguno")
    )

async def plan(update, context):
    user = str(update.effective_user.id)

    USER_DATA.setdefault(user,{})
    USER_DATA[user]["state"] = "vfc"

    await update.message.reply_text("Dame tu VFC")

async def capture(update, context):
    user = str(update.effective_user.id)
    state = USER_DATA.get(user,{}).get("state")

    if not state:
        return

    txt = update.message.text

    if state == "vfc":
        USER_DATA[user]["vfc"] = int(txt)
        USER_DATA[user]["state"] = "sleep"
        await update.message.reply_text("Horas de sueño hh:mm")
        return

    if state == "sleep":
        USER_DATA[user]["sleep"] = hhmm_to_hours(txt)
        USER_DATA[user]["state"] = None

        vfc = USER_DATA[user]["vfc"]
        sleep = USER_DATA[user]["sleep"]

        score = fatigue_score(vfc, sleep)
        lvl = fatigue_level(score)

        plan = generate_plan({
            "fatigue_score": score,
            "fatigue_level": lvl,
            "wocl": "CRITICAL",
            "sleep_time": "18:30"
        })

        await update.message.reply_text(plan)

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
