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

# =========================
# CONFIG
# =========================
DATA_FILE = "data.json"
USER_DATA = {}

# =========================
# UTILS
# =========================
def now():
    return datetime.now(ZoneInfo("America/Mexico_City"))

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
    json.dump(USER_DATA, open(DATA_FILE, "w"), indent=2)

# =========================
# PARSER MEJORADO
# =========================
def parse_roster(text):
    roster = {}
    current_date = None

    for line in text.split("\n"):
        line = line.strip()

        # detectar fecha tipo MAR 31
        match = re.match(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{1,2}", line)
        if match:
            current_date = match.group(0)
            roster[current_date] = {"flights": [], "raw": []}
            continue

        if current_date:
            roster[current_date]["raw"].append(line)

            times = extract_times(line)
            if len(times) >= 2:
                roster[current_date]["flights"].append(times)

    return roster

# =========================
# CALCULO HORAS REAL
# =========================
def calc_hours(flights):
    total_minutes = 0

    for times in flights:
        if len(times) >= 2:
            dep = times[0]
            arr = times[1]

            h1, m1 = map(int, dep.split(":"))
            h2, m2 = map(int, arr.split(":"))

            t1 = h1 * 60 + m1
            t2 = h2 * 60 + m2

            duration = (t2 - t1) % 1440
            total_minutes += duration

    return round(total_minutes / 60, 2)

# =========================
# ANALISIS PRO
# =========================
def roster_analysis(roster):
    days = []

    for date, info in roster.items():
        hours = calc_hours(info["flights"])
        days.append((date, hours))

    total_hours = sum(h for _, h in days)

    # días pesados
    heavy_days = [f"{d} ({round(h,1)}h)" for d,h in days if h > 6]

    # ventanas 7 días
    alerts = []
    for i in range(len(days)):
        window = days[i:i+7]
        if len(window) < 7:
            continue

        hours = sum(h for _, h in window)

        if hours > 30:
            alerts.append(f"🔥 Exceso {window[0][0]}→{window[-1][0]} ({round(hours,1)}h)")
        elif hours > 27:
            alerts.append(f"⚠️ Límite {window[0][0]}→{window[-1][0]} ({round(hours,1)}h)")

    return {
        "total": round(total_hours, 1),
        "heavy": heavy_days,
        "alerts": alerts
    }

# =========================
# TELEGRAM
# =========================
async def handle_pdf(update, context):
    user = str(update.effective_user.id)

    file = await context.bot.get_file(update.message.document.file_id)
    pdf = await file.download_as_bytearray()

    text = ""
    reader = PdfReader(io.BytesIO(pdf))
    for p in reader.pages:
        text += p.extract_text() or ""

    roster = parse_roster(text)
    analysis = roster_analysis(roster)

    USER_DATA.setdefault(user, {})
    USER_DATA[user]["roster"] = roster
    USER_DATA[user]["analysis"] = analysis

    save()

    await update.message.reply_text(f"""
Roster cargado ✅

✈️ Total: {analysis['total']}h

⚠️ Días pesados:
{chr(10).join(analysis['heavy']) if analysis['heavy'] else 'Ninguno'}

🚨 Alertas:
{chr(10).join(analysis['alerts']) if analysis['alerts'] else 'Sin riesgo'}
""")

# =========================
# MAIN
# =========================
def main():
    load()

    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    app.run_polling()

if __name__ == "__main__":
    main()
