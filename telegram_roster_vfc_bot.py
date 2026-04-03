import os
import re
import io
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from PyPDF2 import PdfReader

# OpenAI opcional
USE_OPENAI = True
try:
    from openai import OpenAI
except Exception:
    USE_OPENAI = False
    OpenAI = None


# =========================
# CONFIG
# =========================
DATA_FILE = "pilot_os_data.json"
TZ = "America/Mexico_City"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

USER_DATA: Dict[str, Any] = {}
client = None
if USE_OPENAI and OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        client = None


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
    "No controles el mundo; controla tu respuesta.",
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


# =========================
# UTILIDADES
# =========================
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TZ))


def today_local_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def tomorrow_local_str() -> str:
    return (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")


def user_key(update: Update) -> str:
    return str(update.effective_user.id)


def safe_clean(line: str) -> str:
    return " ".join(line.split()).strip()


def hhmm_to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def minutes_to_hhmm(minutes: int) -> str:
    minutes = minutes % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def hhmm_to_hours(hhmm: str) -> float:
    return round(hhmm_to_minutes(hhmm) / 60, 2)


def parse_month_day_key(key: str, year: int) -> Optional[date]:
    try:
        mon_abbr, day_s = key.split()
        return date(year, MONTHS[mon_abbr], int(day_s))
    except Exception:
        return None


def date_to_key(dt: date) -> str:
    return f"{dt.strftime('%b').upper()} {dt.strftime('%d')}"


def get_daily_quotes(date_str: str) -> Dict[str, str]:
    idx = datetime.strptime(date_str, "%Y-%m-%d").timetuple().tm_yday - 1
    return {
        "stoic": STOIC_QUOTES[idx % len(STOIC_QUOTES)],
        "wisdom": ECC_QUOTES[idx % len(ECC_QUOTES)],
    }


# =========================
# STORAGE
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


# =========================
# PDF
# =========================
def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


# =========================
# ROSTER PARSER ROBUSTO
# =========================
def parse_planning_period_year(text: str) -> int:
    m = re.search(r"Planning Period:\s*\d{2}[A-Z]{3}(\d{4})-\d{2}[A-Z]{3}\d{4}", text)
    if m:
        return int(m.group(1))
    return now_local().year


def parse_planning_period_text(text: str) -> Optional[str]:
    m = re.search(r"Planning Period:\s*([^\n]+)", text)
    return m.group(1).strip() if m else None


def parse_roster_table(text: str) -> Dict[str, Any]:
    year = parse_planning_period_year(text)
    planning_period = parse_planning_period_text(text)

    lines = [safe_clean(l) for l in text.splitlines() if safe_clean(l)]
    in_table = False

    calendar_days: Dict[str, Dict[str, Any]] = {}
    duty_start_days: Dict[str, Dict[str, Any]] = {}
    current_key = None
    pending_overnights: Dict[str, Dict[str, Any]] = {}

    date_line_re = re.compile(r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d{2})([A-Z]{3})\s+(.*)$")
    full_active_with_co = re.compile(
        r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})$"
    )
    full_active_no_co = re.compile(
        r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})$"
    )
    partial_start = re.compile(
        r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})$"
    )
    continuation_end = re.compile(
        r"^(Y\d+)\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})$"
    )
    passive_full = re.compile(r"^P\s+(Y\d+)\s+(.*)$")
    htl_re = re.compile(r"^HTL\b")
    non_flight_re = re.compile(r"^(DMR|LCB)\b")

    def ensure_day(day_key: str):
        if day_key not in calendar_days:
            dt = parse_month_day_key(day_key, year)
            calendar_days[day_key] = {
                "date_key": day_key,
                "iso_date": dt.isoformat() if dt else None,
                "rows": [],
                "active_flights": [],
                "passive_flights": [],
                "hotels": [],
                "non_flight": [],
                "active_block_minutes": 0,
                "passive_block_minutes": 0,
                "earliest_checkin": None,
                "latest_checkout": None,
            }

    def ensure_duty(day_key: str):
        if day_key not in duty_start_days:
            dt = parse_month_day_key(day_key, year)
            duty_start_days[day_key] = {
                "date_key": day_key,
                "iso_date": dt.isoformat() if dt else None,
                "active_block_minutes": 0,
                "passive_block_minutes": 0,
                "active_flights": [],
                "passive_flights": [],
                "earliest_checkin": None,
                "route": [],
                "flags": [],
            }

    def add_active(calendar_key: str, flight: Dict[str, Any], duty_key: Optional[str] = None):
        ensure_day(calendar_key)
        calendar_days[calendar_key]["active_flights"].append(flight)
        calendar_days[calendar_key]["active_block_minutes"] += flight.get("block_minutes", 0)

        ci = flight.get("check_in")
        co = flight.get("check_out")
        if ci:
            if not calendar_days[calendar_key]["earliest_checkin"] or hhmm_to_minutes(ci) < hhmm_to_minutes(calendar_days[calendar_key]["earliest_checkin"]):
                calendar_days[calendar_key]["earliest_checkin"] = ci
        if co:
            if not calendar_days[calendar_key]["latest_checkout"] or hhmm_to_minutes(co) > hhmm_to_minutes(calendar_days[calendar_key]["latest_checkout"]):
                calendar_days[calendar_key]["latest_checkout"] = co

        if duty_key:
            ensure_duty(duty_key)
            duty_start_days[duty_key]["active_flights"].append(flight)
            duty_start_days[duty_key]["active_block_minutes"] += flight.get("block_minutes", 0)
            if flight.get("check_in"):
                if not duty_start_days[duty_key]["earliest_checkin"] or hhmm_to_minutes(flight["check_in"]) < hhmm_to_minutes(duty_start_days[duty_key]["earliest_checkin"]):
                    duty_start_days[duty_key]["earliest_checkin"] = flight["check_in"]
            if flight.get("origin"):
                if not duty_start_days[duty_key]["route"]:
                    duty_start_days[duty_key]["route"].append(flight["origin"])
            if flight.get("dest"):
                if not duty_start_days[duty_key]["route"] or duty_start_days[duty_key]["route"][-1] != flight["dest"]:
                    duty_start_days[duty_key]["route"].append(flight["dest"])

    def add_passive(calendar_key: str, flight: Dict[str, Any], duty_key: Optional[str] = None):
        ensure_day(calendar_key)
        calendar_days[calendar_key]["passive_flights"].append(flight)
        calendar_days[calendar_key]["passive_block_minutes"] += flight.get("block_minutes", 0)
        if duty_key:
            ensure_duty(duty_key)
            duty_start_days[duty_key]["passive_flights"].append(flight)
            duty_start_days[duty_key]["passive_block_minutes"] += flight.get("block_minutes", 0)

    for line in lines:
        if line.startswith("Date DD Activity"):
            in_table = True
            continue
        if line.startswith("Crew Onboard"):
            break
        if not in_table:
            continue

        m_date = date_line_re.match(line)
        content = line

        if m_date:
            mon, day_s, _, rest = m_date.groups()
            current_key = f"{mon} {day_s}"
            ensure_day(current_key)
            ensure_duty(current_key)
            content = rest

        if not current_key:
            continue

        calendar_days[current_key]["rows"].append(content)

        if htl_re.match(content):
            calendar_days[current_key]["hotels"].append(content)
            duty_start_days[current_key]["flags"].append("HTL")
            continue

        if non_flight_re.match(content):
            calendar_days[current_key]["non_flight"].append(content)
            duty_start_days[current_key]["flags"].append(content.split()[0])
            continue

        m_passive = passive_full.match(content)
        if m_passive:
            flt, rest = m_passive.groups()
            times = re.findall(r"\b\d{2}:\d{2}\b", rest)
            block = times[-2] if len(times) >= 2 else None
            add_passive(
                current_key,
                {
                    "flight": flt,
                    "raw": content,
                    "block": block,
                    "block_minutes": hhmm_to_minutes(block) if block else 0,
                },
                duty_key=current_key,
            )
            continue

        m1 = full_active_with_co.match(content)
        if m1:
            flt, ci, orig, std, dest, sta, co, blc, _ = m1.groups()
            add_active(
                current_key,
                {
                    "flight": flt,
                    "check_in": ci,
                    "origin": orig,
                    "std": std,
                    "dest": dest,
                    "sta": sta,
                    "check_out": co,
                    "block": blc,
                    "block_minutes": hhmm_to_minutes(blc),
                    "raw": content,
                },
                duty_key=current_key,
            )
            continue

        m2 = full_active_no_co.match(content)
        if m2:
            flt, ci, orig, std, dest, sta, blc, _ = m2.groups()
            add_active(
                current_key,
                {
                    "flight": flt,
                    "check_in": ci,
                    "origin": orig,
                    "std": std,
                    "dest": dest,
                    "sta": sta,
                    "check_out": None,
                    "block": blc,
                    "block_minutes": hhmm_to_minutes(blc),
                    "raw": content,
                },
                duty_key=current_key,
            )
            continue

        m3 = partial_start.match(content)
        if m3:
            flt, ci, orig, std = m3.groups()
            pending_overnights[flt] = {
                "start_day": current_key,
                "flight": flt,
                "check_in": ci,
                "origin": orig,
                "std": std,
            }
            continue

        m4 = continuation_end.match(content)
        if m4:
            flt, dest, sta, co, blc, _ = m4.groups()
            start_info = pending_overnights.pop(flt, None)
            if start_info:
                add_active(
                    current_key,
                    {
                        "flight": flt,
                        "check_in": None,
                        "origin": start_info["origin"],
                        "std": start_info["std"],
                        "dest": dest,
                        "sta": sta,
                        "check_out": co,
                        "block": blc,
                        "block_minutes": hhmm_to_minutes(blc),
                        "raw": f"{start_info['flight']} {start_info['check_in']} {start_info['origin']} {start_info['std']} ... {dest} {sta} {co} {blc}",
                    },
                    duty_key=start_info["start_day"],
                )
            else:
                add_active(
                    current_key,
                    {
                        "flight": flt,
                        "check_in": None,
                        "origin": None,
                        "std": None,
                        "dest": dest,
                        "sta": sta,
                        "check_out": co,
                        "block": blc,
                        "block_minutes": hhmm_to_minutes(blc),
                        "raw": content,
                    },
                    duty_key=current_key,
                )
            continue

    return {
        "planning_period": planning_period,
        "year": year,
        "calendar_days": calendar_days,
        "duty_start_days": duty_start_days,
    }


def roster_summary(parsed: Dict[str, Any]) -> Dict[str, Any]:
    calendar_days = parsed["calendar_days"]
    duty_days = parsed["duty_start_days"]
    year = parsed["year"]

    sortable = []
    for key, v in calendar_days.items():
        dt = parse_month_day_key(key, year)
        if dt and (v["active_block_minutes"] > 0 or v["passive_block_minutes"] > 0 or v["hotels"] or v["non_flight"]):
            sortable.append((dt, key, v))
    sortable.sort(key=lambda x: x[0])

    first_visible = sortable[0][0].isoformat() if sortable else None
    last_visible = sortable[-1][0].isoformat() if sortable else None

    total_active = sum(v["active_block_minutes"] for _, _, v in sortable)
    total_passive = sum(v["passive_block_minutes"] for _, _, v in sortable)
    total_roster = total_active + total_passive

    heavy_calendar = []
    for _, key, v in sortable:
        hrs = round((v["active_block_minutes"] + v["passive_block_minutes"]) / 60, 2)
        if hrs > 6:
            heavy_calendar.append({"date": key, "hours": hrs})

    alerts = []
    for i in range(len(sortable)):
        window = sortable[i:i+7]
        if len(window) < 7:
            continue
        hrs = round(sum((x[2]["active_block_minutes"] + x[2]["passive_block_minutes"]) for x in window) / 60, 2)
        if hrs > 30:
            alerts.append(f"🔥 Exceso {window[0][1]}→{window[-1][1]} ({hrs}h)")
        elif hrs > 27:
            alerts.append(f"⚠️ Cerca del límite {window[0][1]}→{window[-1][1]} ({hrs}h)")

    top3 = sorted(
        [{"date": key, "hours": round((v["active_block_minutes"] + v["passive_block_minutes"]) / 60, 2)} for _, key, v in sortable],
        key=lambda x: x["hours"],
        reverse=True
    )[:3]

    heavy_duties = []
    for key, v in duty_days.items():
        hrs = round((v["active_block_minutes"] + v["passive_block_minutes"]) / 60, 2)
        if hrs > 6:
            route = " → ".join(v["route"]) if v["route"] else "N/D"
            heavy_duties.append({"date": key, "hours": hrs, "route": route})

    return {
        "planning_period": parsed["planning_period"],
        "visible_start": first_visible,
        "visible_end": last_visible,
        "total_roster_hours": round(total_roster / 60, 2),
        "total_active_hours": round(total_active / 60, 2),
        "total_passive_hours": round(total_passive / 60, 2),
        "days_with_active_flight": sum(1 for _, _, v in sortable if v["active_block_minutes"] > 0),
        "heavy_calendar_days": heavy_calendar,
        "heavy_duty_days": heavy_duties,
        "alerts_30_in_7": alerts,
        "top3_calendar": top3,
    }


def find_day_assignment(parsed: Dict[str, Any], iso_date: str) -> Optional[Dict[str, Any]]:
    dt = datetime.strptime(iso_date, "%Y-%m-%d").date()
    key = date_to_key(dt)
    day = parsed["calendar_days"].get(key)
    if not day:
        return None

    flights = day["active_flights"]
    if not flights and not day["passive_flights"] and not day["hotels"] and not day["non_flight"]:
        return None

    route = []
    for f in flights:
        if f.get("origin") and not route:
            route.append(f["origin"])
        if f.get("dest"):
            if not route or route[-1] != f["dest"]:
                route.append(f["dest"])

    checkin = day.get("earliest_checkin")
    total_hours = round((day["active_block_minutes"] + day["passive_block_minutes"]) / 60, 2)

    return {
        "date_key": key,
        "iso_date": iso_date,
        "check_in": checkin,
        "total_hours": total_hours,
        "active_hours": round(day["active_block_minutes"] / 60, 2),
        "passive_hours": round(day["passive_block_minutes"] / 60, 2),
        "route": " → ".join(route) if route else None,
        "active_flights": flights,
        "passive_flights": day["passive_flights"],
        "hotels": day["hotels"],
        "non_flight": day["non_flight"],
    }


# =========================
# FRMS / FATIGA
# =========================
def analyze_trend(user_block: Dict[str, Any]) -> str:
    metrics = user_block.get("metrics_by_day", {})
    dates = sorted(metrics.keys())[-7:]
    if len(dates) < 3:
        return "sin datos"

    vfc_vals = [metrics[d]["vfc"] for d in dates]
    sleep_vals = [metrics[d]["sleep_hours"] for d in dates]

    if vfc_vals[-1] < vfc_vals[0] - 3:
        return "fatiga creciente"
    if sum(sleep_vals) / len(sleep_vals) < 6:
        return "deuda de sueño"
    return "estable"


def wocl_risk(checkin: Optional[str]) -> str:
    if not checkin:
        return "LOW"
    h = int(checkin.split(":")[0])
    if 2 <= h < 6:
        return "CRITICAL"
    if h < 8:
        return "MODERATE"
    return "LOW"


def fatigue_score(vfc: int, sleep_hours: float, trend: str, wocl: str) -> int:
    score = 100
    if sleep_hours < 5:
        score -= 40
    elif sleep_hours < 6:
        score -= 25
    elif sleep_hours < 7:
        score -= 10

    if vfc < 48:
        score -= 20
    elif vfc < 50:
        score -= 10

    if trend == "fatiga creciente":
        score -= 15
    elif trend == "deuda de sueño":
        score -= 10

    if wocl == "CRITICAL":
        score -= 25
    elif wocl == "MODERATE":
        score -= 10

    return max(score, 0)


def fatigue_level(score: int) -> str:
    if score < 40:
        return "🔥 CRITICAL"
    if score < 60:
        return "🔴 HIGH"
    if score < 80:
        return "🟡 MODERATE"
    return "🟢 LOW"


def next_day_sleep_plan(tomorrow_assignment: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if not tomorrow_assignment or not tomorrow_assignment.get("check_in"):
        return None
    checkin_min = hhmm_to_minutes(tomorrow_assignment["check_in"])
    wake = checkin_min - 120
    sleep = wake - (8 * 60)
    return {
        "checkin": tomorrow_assignment["check_in"],
        "wake_time": minutes_to_hhmm(wake),
        "sleep_time": minutes_to_hhmm(sleep),
    }


def build_time_blocking(today_assignment: Optional[Dict[str, Any]], sleep_plan: Optional[Dict[str, str]], fatigue_lvl: str) -> List[str]:
    blocks = []

    if sleep_plan:
        blocks.append(f"Despertar mañana: {sleep_plan['wake_time']}")
        blocks.append(f"Dormir hoy idealmente: {sleep_plan['sleep_time']}")

    if today_assignment and today_assignment.get("check_in"):
        ci = today_assignment["check_in"]
        ci_min = hhmm_to_minutes(ci)
        blocks.append(f"Pre-duty: {minutes_to_hhmm(ci_min - 60)} snack ligero / hidratación")
        blocks.append(f"Check-in: {ci}")
        blocks.append(f"Post primer bloque: {minutes_to_hhmm(ci_min + 180)} proteína + carbs moderados")
        blocks.append(f"Mitad del día: {minutes_to_hhmm(ci_min + 300)} snack ligero")
        blocks.append("Cena: ligera y fácil de digerir")
    else:
        if fatigue_lvl in ("🔴 HIGH", "🔥 CRITICAL"):
            blocks.extend([
                "Desayuno: proteína + grasa ligera",
                "Comida: proteína + verduras + carbs moderados",
                "Snack: fruta + proteína",
                "Cena: ligera",
            ])
        else:
            blocks.extend([
                "Desayuno: completo y limpio",
                "Comida: balanceada",
                "Snack: ligero",
                "Cena: ligera",
            ])

    return blocks


# =========================
# OPENAI / FALLBACK
# =========================
def generate_ai_plan(payload: Dict[str, Any]) -> str:
    quotes = get_daily_quotes(payload["date"])

    if not client:
        return generate_fallback_plan(payload, quotes)

    system_prompt = """
Eres un coach elite de rendimiento para pilotos y experto en FRMS tipo AIMS.

Tu salida debe:
- sonar humana, clara y útil
- priorizar lo importante
- interpretar el estado del día
- integrar el roster de hoy y de mañana si existe
- explicar el riesgo FRMS (LOW/MODERATE/HIGH/CRITICAL)
- dar una decisión clave del día
- incluir un time blocking práctico
- considerar la hora ideal de dormir hoy para cumplir 8h antes del siguiente duty

No uses checklist robotizado.
No inventes datos del roster.
Si no hay asignación en un día, no la menciones.
Cierra con:
🪶 frase estoica
📖 frase inspirada en sabiduría
"""

    user_prompt = f"""
Analiza este contexto y genera un briefing personal del día:

{json.dumps(payload, ensure_ascii=False, indent=2)}

Que se sienta como un coach elite que habla con un piloto profesional.
Usa estas frases al final:
Estoica: {quotes['stoic']}
Sabiduría: {quotes['wisdom']}
"""

    try:
        res = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
        )
        return res.choices[0].message.content.strip()
    except Exception:
        return generate_fallback_plan(payload, quotes)


def generate_fallback_plan(payload: Dict[str, Any], quotes: Dict[str, str]) -> str:
    parts = []
    parts.append(f"Fecha: {payload['date']}")
    parts.append(f"Estado operativo: {payload['fatigue_level']}")
    parts.append(f"Fatigue score: {payload['fatigue_score']}")
    parts.append(f"Tendencia: {payload['trend']}")
    parts.append("")

    if payload.get("today_assignment"):
        ta = payload["today_assignment"]
        parts.append("Hoy sí tienes asignación.")
        if ta.get("route"):
            parts.append(f"Ruta: {ta['route']}")
        if ta.get("check_in"):
            parts.append(f"Check-in: {ta['check_in']}")
        parts.append(f"Block total hoy: {ta['total_hours']} h")
        parts.append("")

    if payload.get("tomorrow_assignment"):
        tm = payload["tomorrow_assignment"]
        parts.append("Mañana también tienes asignación.")
        if tm.get("route"):
            parts.append(f"Ruta mañana: {tm['route']}")
        if tm.get("check_in"):
            parts.append(f"Check-in mañana: {tm['check_in']}")
        if payload.get("sleep_plan"):
            parts.append(f"Para cumplir 8h, hoy deberías dormir cerca de {payload['sleep_plan']['sleep_time']}.")
        parts.append("")

    parts.append("Time blocking sugerido:")
    for b in payload["time_blocking"]:
        parts.append(f"- {b}")
    parts.append("")
    parts.append(f"🪶 {quotes['stoic']}")
    parts.append(f"📖 {quotes['wisdom']}")
    return "\n".join(parts)


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot listo ✅\n\n"
        "1. Súbeme tu roster PDF\n"
        "2. Usa /plan cada día\n"
        "3. Yo conservaré el roster hasta que subas uno nuevo"
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = user_key(update)

    tg_file = await context.bot.get_file(update.message.document.file_id)
    pdf_bytes = await tg_file.download_as_bytearray()

    text = extract_pdf_text(pdf_bytes)
    parsed = parse_roster_table(text)
    summary = roster_summary(parsed)

    USER_DATA.setdefault(u, {})
    USER_DATA[u]["roster_parsed"] = parsed
    USER_DATA[u]["roster_summary"] = summary
    USER_DATA[u]["conversation_state"] = None
    USER_DATA[u].setdefault("metrics_by_day", {})
    save_data()

    heavy = summary["heavy_calendar_days"]
    heavy_text = "\n".join([f"- {x['date']} ({x['hours']}h)" for x in heavy[:5]]) if heavy else "Ninguno"

    top3 = summary["top3_calendar"]
    top3_text = "\n".join([f"- {x['date']} ({x['hours']}h)" for x in top3]) if top3 else "Ninguno"

    alerts_text = "\n".join(summary["alerts_30_in_7"]) if summary["alerts_30_in_7"] else "Sin riesgo"

    visible_period = "N/D"
    if summary["visible_start"] and summary["visible_end"]:
        visible_period = f"{summary['visible_start']} → {summary['visible_end']}"

    msg = (
        "Roster cargado ✅\n\n"
        f"Periodo visible: {visible_period}\n"
        f"Horas roster (incluye pasivos): {summary['total_roster_hours']}h\n"
        f"Horas activas: {summary['total_active_hours']}h\n"
        f"Horas pasivas: {summary['total_passive_hours']}h\n"
        f"Días con vuelo activo: {summary['days_with_active_flight']}\n\n"
        f"Días pesados (>6h calendario):\n{heavy_text}\n\n"
        f"Top 3 días con más block:\n{top3_text}\n\n"
        f"Alertas 30h / 7 días:\n{alerts_text}"
    )
    await update.message.reply_text(msg)


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = user_key(update)
    USER_DATA.setdefault(u, {})

    if "roster_parsed" not in USER_DATA[u]:
        await update.message.reply_text("Primero súbeme tu roster PDF.")
        return

    USER_DATA[u]["conversation_state"] = "awaiting_vfc"
    USER_DATA[u]["pending_plan"] = {"date": today_local_str()}
    save_data()

    await update.message.reply_text("Buen día. ¿Cuál fue tu VFC de 7 días?")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = user_key(update)
    metrics = USER_DATA.get(u, {}).get("metrics_by_day", {})
    if not metrics:
        await update.message.reply_text("No tengo métricas guardadas todavía.")
        return

    dates = sorted(metrics.keys())[-7:]
    lines = ["Últimas métricas:"]
    for d in dates:
        m = metrics[d]
        lines.append(f"{d} → VFC {m['vfc']} | Sueño {m['sleep_hhmm']} | Score {m['score']}")
    await update.message.reply_text("\n".join(lines))


# =========================
# CAPTURE FLOW
# =========================
async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = user_key(update)
    state = USER_DATA.get(u, {}).get("conversation_state")
    if not state:
        return

    text = update.message.text.strip()

    if state == "awaiting_vfc":
        try:
            vfc = int(text)
        except ValueError:
            await update.message.reply_text("Pásame solo el número de VFC. Ejemplo: 50")
            return

        USER_DATA[u]["pending_plan"]["vfc"] = vfc
        USER_DATA[u]["conversation_state"] = "awaiting_sleep"
        save_data()
        await update.message.reply_text("¿Cuánto dormiste? Escríbelo en hh:mm, por ejemplo 04:05")
        return

    if state == "awaiting_sleep":
        if not re.fullmatch(r"\d{1,2}:\d{2}", text):
            await update.message.reply_text("Formato inválido. Usa hh:mm, por ejemplo 04:05")
            return

        USER_DATA[u]["pending_plan"]["sleep_hhmm"] = text
        USER_DATA[u]["pending_plan"]["sleep_hours"] = hhmm_to_hours(text)
        USER_DATA[u]["conversation_state"] = "awaiting_score"
        save_data()
        await update.message.reply_text("¿Cuál fue tu score de sueño?")
        return

    if state == "awaiting_score":
        try:
            score = int(text)
        except ValueError:
            await update.message.reply_text("Pásame solo el número del score. Ejemplo: 53")
            return

        pending = USER_DATA[u]["pending_plan"]
        pending["score"] = score

        USER_DATA[u].setdefault("metrics_by_day", {})
        USER_DATA[u]["metrics_by_day"][pending["date"]] = {
            "vfc": pending["vfc"],
            "sleep_hhmm": pending["sleep_hhmm"],
            "sleep_hours": pending["sleep_hours"],
            "score": pending["score"],
            "saved_at": now_local().isoformat(),
        }

        parsed = USER_DATA[u]["roster_parsed"]
        today_assignment = find_day_assignment(parsed, pending["date"])
        tomorrow_assignment = find_day_assignment(parsed, tomorrow_local_str())

        tr = analyze_trend(USER_DATA[u])
        tomorrow_checkin = tomorrow_assignment["check_in"] if tomorrow_assignment else None
        wocl = wocl_risk(tomorrow_checkin)
        score_num = fatigue_score(pending["vfc"], pending["sleep_hours"], tr, wocl)
        lvl = fatigue_level(score_num)
        sleep_plan = next_day_sleep_plan(tomorrow_assignment)
        time_blocks = build_time_blocking(today_assignment, sleep_plan, lvl)

        payload = {
            "date": pending["date"],
            "vfc": pending["vfc"],
            "sleep_hhmm": pending["sleep_hhmm"],
            "sleep_hours": pending["sleep_hours"],
            "sleep_score": pending["score"],
            "trend": tr,
            "wocl_tomorrow": wocl,
            "fatigue_score": score_num,
            "fatigue_level": lvl,
            "today_assignment": today_assignment,
            "tomorrow_assignment": tomorrow_assignment,
            "sleep_plan": sleep_plan,
            "time_blocking": time_blocks,
            "roster_summary": USER_DATA[u]["roster_summary"],
        }

        plan_text = generate_ai_plan(payload)
        plan_text += "\n\n💾 Métricas guardadas para esta fecha."

        USER_DATA[u]["conversation_state"] = None
        USER_DATA[u].pop("pending_plan", None)
        save_data()

        await update.message.reply_text(plan_text)


# =========================
# MAIN
# =========================
def main():
    load_data()
    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
