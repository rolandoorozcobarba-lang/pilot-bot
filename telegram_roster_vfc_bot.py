import os
import re
import io
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, List

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
# UTILS
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


def user_key(update: Update) -> str:
    return str(update.effective_user.id)


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


def parse_planning_period_year(text: str) -> int:
    m = re.search(r"Planning Period:\s*\d{2}[A-Z]{3}(\d{4})-\d{2}[A-Z]{3}\d{4}", text)
    if m:
        return int(m.group(1))
    return now_local().year


# =========================
# ROSTER PARSER
# =========================
def parse_roster_table(text: str) -> Dict[str, Any]:
    """
    Parser pensado para el formato Jeppesen del roster Volaris.
    Cuenta block activo y pasivo por día calendario.
    """
    year = parse_planning_period_year(text)
    lines = [safe_clean(l) for l in text.splitlines() if safe_clean(l)]

    in_table = False
    current_key = None
    pending_overnights: Dict[str, Dict[str, Any]] = {}
    calendar_days: Dict[str, Dict[str, Any]] = {}

    date_line_re = re.compile(r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d{2})([A-Z]{3})\s+(.*)$")
    full_active_with_co = re.compile(
        r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})$"
    )
    full_active_no_co = re.compile(
        r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})$"
    )
    partial_start = re.compile(r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})$")
    continuation_end = re.compile(r"^(Y\d+)\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})$")
    passive_full = re.compile(r"^P\s+(Y\d+)\s+(.*)$")
    htl_re = re.compile(r"^HTL\b")
    non_flight_re = re.compile(r"^(DMR|LCB)\b")

    def ensure_day(day_key: str):
        if day_key not in calendar_days:
            dt = parse_month_day_key(day_key, year)
            calendar_days[day_key] = {
                "date_key": day_key,
                "iso_date": dt.isoformat() if dt else None,
                "active_flights": [],
                "passive_flights": [],
                "hotels": [],
                "non_flight": [],
                "active_block_minutes": 0,
                "passive_block_minutes": 0,
                "earliest_checkin": None,
                "route": [],
            }

    def add_route(day_key: str, origin: Optional[str], dest: Optional[str]):
        route = calendar_days[day_key]["route"]
        if origin and not route:
            route.append(origin)
        if dest:
            if not route or route[-1] != dest:
                route.append(dest)

    def add_active(day_key: str, flight: Dict[str, Any]):
        ensure_day(day_key)
        calendar_days[day_key]["active_flights"].append(flight)
        calendar_days[day_key]["active_block_minutes"] += flight["block_minutes"]

        if flight.get("check_in"):
            ci = flight["check_in"]
            cur = calendar_days[day_key]["earliest_checkin"]
            if not cur or hhmm_to_minutes(ci) < hhmm_to_minutes(cur):
                calendar_days[day_key]["earliest_checkin"] = ci

        add_route(day_key, flight.get("origin"), flight.get("dest"))

    def add_passive(day_key: str, flight: Dict[str, Any]):
        ensure_day(day_key)
        calendar_days[day_key]["passive_flights"].append(flight)
        calendar_days[day_key]["passive_block_minutes"] += flight["block_minutes"]

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
            content = rest

        if not current_key:
            continue

        if htl_re.match(content):
            calendar_days[current_key]["hotels"].append(content)
            continue

        if non_flight_re.match(content):
            calendar_days[current_key]["non_flight"].append(content)
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
                        "raw": content,
                    },
                )
            continue

    return {
        "year": year,
        "calendar_days": calendar_days,
    }


def roster_summary(parsed: Dict[str, Any]) -> Dict[str, Any]:
    days = parsed["calendar_days"]
    year = parsed["year"]

    sortable = []
    for key, v in days.items():
        dt = parse_month_day_key(key, year)
        if dt and (
            v["active_block_minutes"] > 0
            or v["passive_block_minutes"] > 0
            or v["hotels"]
            or v["non_flight"]
        ):
            sortable.append((dt, key, v))
    sortable.sort(key=lambda x: x[0])

    total_active = sum(v["active_block_minutes"] for _, _, v in sortable)
    total_passive = sum(v["passive_block_minutes"] for _, _, v in sortable)
    total_roster = total_active + total_passive

    heavy_days = []
    for _, key, v in sortable:
        total_h = round((v["active_block_minutes"] + v["passive_block_minutes"]) / 60, 2)
        if total_h > 6:
            heavy_days.append({"date": key, "hours": total_h})

    top3 = sorted(
        [{"date": key, "hours": round((v["active_block_minutes"] + v["passive_block_minutes"]) / 60, 2)} for _, key, v in sortable],
        key=lambda x: x["hours"],
        reverse=True
    )[:3]

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

    visible_start = sortable[0][0].isoformat() if sortable else None
    visible_end = sortable[-1][0].isoformat() if sortable else None

    return {
        "visible_start": visible_start,
        "visible_end": visible_end,
        "total_roster_hours": round(total_roster / 60, 2),
        "total_active_hours": round(total_active / 60, 2),
        "total_passive_hours": round(total_passive / 60, 2),
        "days_with_active_flight": sum(1 for _, _, v in sortable if v["active_block_minutes"] > 0),
        "heavy_days": heavy_days,
        "top3": top3,
        "alerts": alerts,
    }


def find_day_assignment(parsed: Dict[str, Any], iso_date: str) -> Optional[Dict[str, Any]]:
    dt = datetime.strptime(iso_date, "%Y-%m-%d").date()
    key = date_to_key(dt)
    day = parsed["calendar_days"].get(key)
    if not day:
        return None

    if not (day["active_flights"] or day["passive_flights"] or day["hotels"] or day["non_flight"]):
        return None

    return {
        "date_key": key,
        "iso_date": iso_date,
        "check_in": day["earliest_checkin"],
        "active_hours": round(day["active_block_minutes"] / 60, 2),
        "passive_hours": round(day["passive_block_minutes"] / 60, 2),
        "total_hours": round((day["active_block_minutes"] + day["passive_block_minutes"]) / 60, 2),
        "route": " → ".join(day["route"]) if day["route"] else None,
        "active_flights": day["active_flights"],
        "passive_flights": day["passive_flights"],
        "hotels": day["hotels"],
        "non_flight": day["non_flight"],
    }


# =========================
# FATIGA / PLAN
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


def fatigue_score(vfc: int, sleep_hours: float, sleep_score: int, trend: str, wocl: str) -> int:
    score = 100

    if sleep_hours < 5:
        score -= 35
    elif sleep_hours < 6:
        score -= 20
    elif sleep_hours < 7:
        score -= 8

    if sleep_score < 60:
        score -= 18
    elif sleep_score < 75:
        score -= 10
    elif sleep_score < 85:
        score -= 5

    if vfc < 48:
        score -= 15
    elif vfc < 50:
        score -= 8

    if trend == "fatiga creciente":
        score -= 15
    elif trend == "deuda de sueño":
        score -= 10

    if wocl == "CRITICAL":
        score -= 20
    elif wocl == "MODERATE":
        score -= 8

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
        blocks.append(f"Pre-duty: {minutes_to_hhmm(ci_min - 60)} snack ligero e hidratación")
        blocks.append(f"Check-in: {ci}")
        blocks.append(f"Mitad de jornada: {minutes_to_hhmm(ci_min + 240)} snack ligero")
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
# IA / FALLBACK
# =========================
def generate_fallback_plan(payload: Dict[str, Any], quotes: Dict[str, str]) -> str:
    parts = []

    parts.append("Resumen de hoy")
    parts.append(
        f"Hoy estás en {payload['fatigue_level']}. "
        f"No es un mal día, pero el contexto manda."
    )
    parts.append("")

    parts.append("Fatiga y WOCL")
    parts.append(f"- Fatiga: {payload['fatigue_score']} / {payload['fatigue_level']}")
    parts.append(f"- WOCL mañana: {payload['wocl_tomorrow']}")
    parts.append("")

    parts.append("Lo más importante")
    if payload.get("sleep_plan"):
        parts.append(f"Tu prioridad hoy es proteger el descanso para poder dormir cerca de {payload['sleep_plan']['sleep_time']}.")
    else:
        parts.append("Tu prioridad hoy es conservar energía y no añadir fatiga innecesaria.")
    parts.append("")

    parts.append("Movimiento recomendado hoy")
    if payload["wocl_tomorrow"] in ("CRITICAL", "MODERATE") or payload["fatigue_level"] in ("🔴 HIGH", "🔥 CRITICAL"):
        parts.append("- Caminata suave 20–30 min")
        parts.append("- Movilidad 10–15 min")
        parts.append("- Evitar HIIT o cargas pesadas")
    elif payload["fatigue_level"] == "🟡 MODERATE":
        parts.append("- Fuerza ligera o moderada")
        parts.append("- Caminata")
        parts.append("- Core o movilidad")
    else:
        parts.append("- Buen día para entrenamiento moderado")
        parts.append("- Si mañana exige temprano, no te sobrecargues")
    parts.append("")

    parts.append("Plan práctico del día")
    for b in payload["time_blocking"]:
        parts.append(f"- {b}")
    parts.append("")

    if payload.get("tomorrow_assignment"):
        tm = payload["tomorrow_assignment"]
        parts.append("Asignación de mañana")
        if tm.get("route"):
            parts.append(f"- Ruta: {tm['route']}")
        if tm.get("check_in"):
            parts.append(f"- Check-in: {tm['check_in']}")
        if tm.get("total_hours") is not None:
            parts.append(f"- Block total: {tm['total_hours']}h")
        parts.append("")

    parts.append("Cierre")
    parts.append("Hoy no toca exprimir el día; toca prepararte bien.")
    parts.append("")
    parts.append(f"🪶 {quotes['stoic']}")
    parts.append(f"📖 {quotes['wisdom']}")

    return "\n".join(parts)


def generate_ai_plan(payload: Dict[str, Any]) -> str:
    quotes = get_daily_quotes(payload["date"])

    if not client:
        return generate_fallback_plan(payload, quotes)

    system_prompt = """
Eres un coach elite de rendimiento para pilotos y experto en FRMS tipo AIMS.

Tu respuesta debe sentirse como un briefing personal:
- humano
- claro
- ejecutivo
- útil

Estructura la respuesta así:
1. Resumen de hoy
2. Fatiga y WOCL
3. Lo más importante
4. Movimiento recomendado hoy
5. Plan práctico del día
6. Asignación de mañana (solo si aplica)
7. Cierre
8. Frases finales

Reglas:
- Mantén explícitos los datos de FATIGA y WOCL.
- Explícalos en lenguaje humano.
- Incluye siempre una recomendación concreta de movimiento/ejercicio.
- Si mañana hay check-in temprano o WOCL crítico, evita recomendar entrenamiento intenso.
- No inventes datos del roster.
- Si no hay asignación mañana, no la menciones.
- No suenes como checklist robótico.
"""

    user_prompt = f"""
Analiza este contexto y genera el briefing del día:

{json.dumps(payload, ensure_ascii=False, indent=2)}

Hazlo:
- amigable
- útil
- directo
- sobrio

Y usa estas frases al final:
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
            temperature=0.85,
        )
        return res.choices[0].message.content.strip()
    except Exception:
        return generate_fallback_plan(payload, quotes)


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = user_key(update)
    USER_DATA.setdefault(u, {})
    has_roster = "roster_parsed" in USER_DATA[u]

    if has_roster:
        await update.message.reply_text(
            "✈️ Bot listo\n\n"
            "Roster cargado ✅\n"
            "Puedes usar /plan cuando quieras."
        )
    else:
        await update.message.reply_text(
            "✈️ Bot listo\n\n"
            "1. Súbeme tu roster PDF\n"
            "2. Usa /plan para tu análisis diario"
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

    visible_period = "N/D"
    if summary["visible_start"] and summary["visible_end"]:
        visible_period = f"{summary['visible_start']} → {summary['visible_end']}"

    top3_text = "\n".join([f"- {x['date']} ({x['hours']}h)" for x in summary["top3"]]) if summary["top3"] else "Ninguno"
    heavy_text = "\n".join([f"- {x['date']} ({x['hours']}h)" for x in summary["heavy_days"]]) if summary["heavy_days"] else "Ninguno"
    alerts_text = "\n".join(summary["alerts"]) if summary["alerts"] else "Sin riesgo"

    msg = (
        "Roster cargado ✅\n\n"
        f"Periodo visible: {visible_period}\n"
        f"Horas roster (incluye pasivos): {summary['total_roster_hours']}h\n"
        f"Horas activas: {summary['total_active_hours']}h\n"
        f"Horas pasivas: {summary['total_passive_hours']}h\n\n"
        f"Top 3 días con más block:\n{top3_text}\n\n"
        f"Días pesados (>6h):\n{heavy_text}\n\n"
        f"Alertas 30h / 7 días:\n{alerts_text}\n\n"
        "Listo para /plan 🧠"
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

    await update.message.reply_text("Dame tu VFC de 7 días")


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
# CAPTURE
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
            await update.message.reply_text("Pásame solo el número de VFC. Ejemplo: 52")
            return

        USER_DATA[u]["pending_plan"]["vfc"] = vfc
        USER_DATA[u]["conversation_state"] = "awaiting_sleep"
        save_data()
        await update.message.reply_text("Horas de sueño hh:mm")
        return

    if state == "awaiting_sleep":
        if not re.fullmatch(r"\d{1,2}:\d{2}", text):
            await update.message.reply_text("Formato inválido. Usa hh:mm, por ejemplo 08:06")
            return

        USER_DATA[u]["pending_plan"]["sleep_hhmm"] = text
        USER_DATA[u]["pending_plan"]["sleep_hours"] = hhmm_to_hours(text)
        USER_DATA[u]["conversation_state"] = "awaiting_score"
        save_data()
        await update.message.reply_text("Score de sueño")
        return

    if state == "awaiting_score":
        try:
            score = int(text)
        except ValueError:
            await update.message.reply_text("Pásame solo el número del score. Ejemplo: 98")
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
        score_num = fatigue_score(pending["vfc"], pending["sleep_hours"], pending["score"], tr, wocl)
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

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Falta TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
