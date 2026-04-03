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
from openai import OpenAI

DATA_FILE = "user_metrics.json"
USER_DATA = {}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# FRASES DIARIAS
# =========================

# Puedes pegar aquí tus 365 frases completas.
# Te dejo una versión base con algunas y el sistema ya listo.
STOIC_QUOTES = [
    "Haz hoy lo que está en tus manos.",
    "La calma también es valentía.",
    "Domina la primera reacción.",
    "Tu carácter se forja en lo incómodo.",
    "La disciplina es amor al futuro.",
    "No controles el mundo; gobierna tu respuesta.",
    "Lo que repites te construye.",
    "La serenidad es poder bien dirigido.",
    "Acepta la realidad y trabaja dentro de ella.",
    "La constancia vale más que el entusiasmo aislado.",
]

ECCLESIASTICUS_QUOTES = [
    "La sabiduría acompaña al que camina con humildad.",
    "La lengua prudente evita heridas innecesarias.",
    "Quien escucha antes de hablar ya va delante.",
    "El corazón paciente cosecha más que la prisa.",
    "El hombre prudente guarda silencio a tiempo.",
    "La mansedumbre sostiene más que la fuerza bruta.",
    "No desprecies el consejo que corrige.",
    "El sabio prefiere aprender antes que presumir.",
    "La sabiduría entra mejor en un corazón dócil.",
    "La buena palabra llega en el momento justo.",
]


def get_daily_quotes(date_str: str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    day_index = dt.timetuple().tm_yday - 1

    stoic = STOIC_QUOTES[day_index % len(STOIC_QUOTES)]
    ecc = ECCLESIASTICUS_QUOTES[day_index % len(ECCLESIASTICUS_QUOTES)]

    return {
        "stoic": stoic,
        "ecclesiasticus": ecc,
    }


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
# FECHA / HORA
# =========================

def now_local():
    return datetime.now(ZoneInfo("America/Mexico_City"))


def today_local_str():
    return now_local().strftime("%Y-%m-%d")


def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def month_abbr_for_date(date_str: str) -> str:
    return parse_date(date_str).strftime("%b").upper()


def day_for_date(date_str: str) -> str:
    return parse_date(date_str).strftime("%d")


# =========================
# UTILIDADES
# =========================

def clean_line(line: str) -> str:
    return " ".join(line.split()).strip()


def hhmm_to_hours(hhmm: str) -> float:
    h, m = hhmm.split(":")
    return int(h) + int(m) / 60


def hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h}:{m:02d}"


def add_minutes_to_hhmm(hhmm: str, delta: int) -> str:
    total = hhmm_to_minutes(hhmm) + delta
    total = total % (24 * 60)
    return minutes_to_hhmm(total)


def parse_hhmm_safe(value):
    if not value:
        return None
    if re.fullmatch(r"\d{2}:\d{2}", value):
        return value
    return None


def classify_day(vfc: int, sleep: float) -> str:
    if vfc >= 53 and sleep >= 7:
        return "🟢 VERDE"
    elif vfc <= 49 or sleep < 6:
        return "🔴 ROJO"
    return "🟡 AMARILLO"


# =========================
# PDF / ROSTER
# =========================

def extract_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text


def normalize_lines(text: str):
    return [clean_line(line) for line in text.split("\n") if clean_line(line)]


def parse_roster_assignments(text: str):
    lines = normalize_lines(text)
    assignments_by_date = {}
    current_date = None

    date_pattern = re.compile(
        r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d{2})([A-Z]{3})\s+(.*)$"
    )

    flight_with_ci = re.compile(
        r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})"
    )

    flight_no_ci = re.compile(
        r"^(Y\d+)\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})"
    )

    hotel_pattern = re.compile(r"^(HTL)\s+([A-Z]{3})\s+(\d{2}:\d{2})?$")

    for line in lines:
        if line.startswith("Crew Onboard") or line.startswith("Hotel information") or line.startswith("Document expire"):
            break

        date_match = date_pattern.match(line)
        content = line

        if date_match:
            month, day, weekday, rest = date_match.groups()
            current_date = f"{month} {day}"
            assignments_by_date.setdefault(current_date, {
                "flights": [],
                "hotel": None,
            })
            content = rest

        if not current_date:
            continue

        hotel_match = hotel_pattern.match(content)
        if hotel_match:
            _, hotel_station, hotel_time = hotel_match.groups()
            assignments_by_date[current_date]["hotel"] = {
                "station": hotel_station,
                "time": hotel_time,
            }
            continue

        m1 = flight_with_ci.match(content)
        if m1:
            flt, ci, orig, std, dest, sta, co, block = m1.groups()
            assignments_by_date[current_date]["flights"].append({
                "flight": flt,
                "check_in": parse_hhmm_safe(ci),
                "origin": orig,
                "std": parse_hhmm_safe(std),
                "dest": dest,
                "sta": parse_hhmm_safe(sta),
                "check_out": parse_hhmm_safe(co),
                "block": parse_hhmm_safe(block),
            })
            continue

        m2 = flight_no_ci.match(content)
        if m2:
            flt, orig, std, dest, sta, co, block = m2.groups()
            assignments_by_date[current_date]["flights"].append({
                "flight": flt,
                "check_in": None,
                "origin": orig,
                "std": parse_hhmm_safe(std),
                "dest": dest,
                "sta": parse_hhmm_safe(sta),
                "check_out": parse_hhmm_safe(co),
                "block": parse_hhmm_safe(block),
            })
            continue

    return assignments_by_date


def classify_duty_type(check_in, first_std, last_sta):
    def in_range(value, start, end):
        if value is None:
            return False
        m = hhmm_to_minutes(value)
        return hhmm_to_minutes(start) <= m <= hhmm_to_minutes(end)

    if check_in and hhmm_to_minutes(check_in) < hhmm_to_minutes("06:00"):
        return "temprano"
    if in_range(first_std, "22:00", "23:59") or in_range(last_sta, "00:00", "05:59"):
        return "nocturno"
    if check_in and hhmm_to_minutes(check_in) >= hhmm_to_minutes("14:00"):
        return "tarde"
    return "diurno"


def is_wocl_affected(assignment):
    if not assignment:
        return False

    for key in ["check_in", "first_departure", "last_arrival", "last_check_out"]:
        value = assignment.get(key)
        if value and hhmm_to_minutes("02:00") <= hhmm_to_minutes(value) <= hhmm_to_minutes("05:59"):
            return True
    return False


def is_long_duty(assignment):
    if not assignment or not assignment.get("duty_time_minutes"):
        return False
    return assignment["duty_time_minutes"] >= 10 * 60


def suggest_nap_window(assignment):
    if not assignment:
        return None
    duty_type = assignment.get("duty_type")
    if duty_type == "temprano":
        return "Si puedes, siesta breve de 20–30 min después del duty o bloque de recuperación temprano."
    if duty_type == "nocturno":
        return "Si puedes, siesta previa de 60–90 min antes del duty."
    if duty_type == "tarde":
        return "Si te notas bajo, siesta breve de 20–30 min al mediodía puede ayudar."
    return None


def find_assignment_for_today(assignments_by_date, today_str):
    key = f"{month_abbr_for_date(today_str)} {day_for_date(today_str)}"
    day_data = assignments_by_date.get(key)
    if not day_data:
        return None

    flights = day_data.get("flights", [])
    hotel = day_data.get("hotel")

    if not flights and not hotel:
        return None

    route = []
    total_flight_minutes = 0
    first_check_in = None
    first_std = None
    last_sta = None
    last_check_out = None
    sectors = len(flights)

    for i, flight in enumerate(flights):
        if i == 0 and flight.get("origin"):
            route.append(flight["origin"])
        if flight.get("dest"):
            route.append(flight["dest"])

        if flight.get("block"):
            total_flight_minutes += hhmm_to_minutes(flight["block"])

        if not first_check_in and flight.get("check_in"):
            first_check_in = flight["check_in"]

        if not first_std and flight.get("std"):
            first_std = flight["std"]

        if flight.get("sta"):
            last_sta = flight["sta"]

        if flight.get("check_out"):
            last_check_out = flight["check_out"]

    duty_minutes = None
    if first_check_in and last_check_out:
        duty_minutes = hhmm_to_minutes(last_check_out) - hhmm_to_minutes(first_check_in)
        if duty_minutes < 0:
            duty_minutes += 24 * 60

    assignment = {
        "date": today_str,
        "check_in": first_check_in,
        "first_departure": first_std,
        "last_arrival": last_sta,
        "last_check_out": last_check_out,
        "route": " → ".join(route) if route else None,
        "flight_time_minutes": total_flight_minutes,
        "flight_time_hhmm": minutes_to_hhmm(total_flight_minutes),
        "duty_time_minutes": duty_minutes,
        "duty_time_hhmm": minutes_to_hhmm(duty_minutes) if duty_minutes is not None else None,
        "sectors": sectors,
        "duty_type": classify_duty_type(first_check_in, first_std, last_sta),
        "pernocta": hotel is not None,
        "hotel": hotel,
        "flights": flights,
    }
    assignment["wocl"] = is_wocl_affected(assignment)
    assignment["long_duty"] = is_long_duty(assignment)
    assignment["nap_window"] = suggest_nap_window(assignment)
    return assignment


def format_assignment(assignment):
    if not assignment:
        return ""

    lines = ["✈️ Asignación de hoy"]
    if assignment.get("check_in"):
        lines.append(f"Check-in: {assignment['date']} {assignment['check_in']}")
    else:
        lines.append(f"Fecha: {assignment['date']}")

    if assignment.get("route"):
        lines.append(f"Ruta: {assignment['route']}")

    lines.append(f"Sectores: {assignment.get('sectors', 0)}")

    if assignment.get("flight_time_hhmm"):
        lines.append(f"Horas de vuelo: {assignment['flight_time_hhmm']}")

    if assignment.get("duty_time_hhmm"):
        lines.append(f"Horas de jornada: {assignment['duty_time_hhmm']}")

    if assignment.get("duty_type"):
        lines.append(f"Tipo de jornada: {assignment['duty_type']}")

    if assignment.get("wocl"):
        lines.append("WOCL: sí")

    if assignment.get("long_duty"):
        lines.append("Jornada larga: sí")

    if assignment.get("pernocta"):
        hotel = assignment.get("hotel") or {}
        station = hotel.get("station")
        if station:
            lines.append(f"Pernocta: sí ({station})")
        else:
            lines.append("Pernocta: sí")

    return "\n".join(lines)


# =========================
# PROMPT + OPENAI
# =========================

def get_recent_metrics(user_block):
    metrics = user_block.get("metrics_by_day", {})
    dates = sorted(metrics.keys())[-7:]
    recent = []
    for d in dates:
        item = metrics[d]
        recent.append({
            "date": d,
            "vfc": item["vfc"],
            "sleep_hhmm": item["sleep_hhmm"],
            "sleep_hours": item["sleep_hours"],
            "score": item["score"],
        })
    return recent


def build_time_blocks(assignment, state_label):
    if not assignment:
        if state_label == "🔴 ROJO":
            return [
                "Desayuno: proteína + grasa ligera",
                "Comida: proteína + verduras + carbs moderados",
                "Snack: fruta + proteína",
                "Cena: ligera y fácil de digerir",
            ]
        elif state_label == "🟡 AMARILLO":
            return [
                "Desayuno: limpio y suficiente",
                "Comida: balanceada",
                "Snack: ligero",
                "Cena: ligera",
            ]
        else:
            return [
                "Desayuno: completo con proteína + carbs",
                "Comida: fuerte y limpia",
                "Snack: ligero si hace falta",
                "Cena: ligera",
            ]

    duty_type = assignment.get("duty_type")
    ci = assignment.get("check_in")
    blocks = []

    if duty_type == "temprano" and ci:
        blocks.append(f"{add_minutes_to_hhmm(ci, -60)} Pre duty: snack pequeño y fácil de digerir")
        blocks.append(f"{add_minutes_to_hhmm(ci, 180)} Post primer bloque: proteína + carbs moderados")
        blocks.append(f"{add_minutes_to_hhmm(ci, 300)} Snack ligero")
        blocks.append(f"{add_minutes_to_hhmm(ci, 480)} Post duty/comida principal: completa sin exceso")
        blocks.append("Noche: cena ligera")
    elif duty_type == "nocturno" and ci:
        blocks.append(f"{add_minutes_to_hhmm(ci, -240)} Comida principal limpia")
        blocks.append(f"{add_minutes_to_hhmm(ci, -90)} Snack ligero pre duty")
        blocks.append(f"{add_minutes_to_hhmm(ci, 120)} Bocadillo fácil de digerir si hace falta")
        blocks.append("Post duty: proteína ligera + hidratación")
        blocks.append("Antes de dormir: nada pesado")
    elif duty_type == "tarde" and ci:
        blocks.append("Desayuno: completo")
        blocks.append(f"{add_minutes_to_hhmm(ci, -240)} Comida fuerte y limpia")
        blocks.append(f"{add_minutes_to_hhmm(ci, -60)} Snack ligero pre duty")
        blocks.append("Post duty: cena ligera")
    else:
        blocks.append("Desayuno: limpio y suficiente")
        blocks.append("Comida principal: antes o después del bloque central")
        blocks.append("Snack: ligero")
        blocks.append("Cena: ligera")

    return blocks


def build_ai_payload(user_block, date_str, vfc, sleep_hhmm, sleep_hours, score, assignment):
    quotes = get_daily_quotes(date_str)
    recent = get_recent_metrics(user_block)
    state_label = classify_day(vfc, sleep_hours)

    payload = {
        "date": date_str,
        "state_label": state_label,
        "metrics_today": {
            "vfc_7d": vfc,
            "sleep_hhmm": sleep_hhmm,
            "sleep_hours": sleep_hours,
            "sleep_score": score,
        },
        "recent_metrics": recent,
        "assignment": assignment,
        "time_blocks_base": build_time_blocks(assignment, state_label),
        "quotes": quotes,
    }
    return payload


def generate_ai_plan(payload):
    if not client:
        raise RuntimeError("Falta OPENAI_API_KEY")

    system_prompt = """
Eres un asistente experto en rendimiento para un piloto comercial.
Tu trabajo es generar un plan diario en español, claro, útil y accionable.

Reglas:
- Usa solo la información recibida.
- No inventes datos del roster.
- Si no hay asignación, no menciones vuelos.
- Integra análisis fisiológico, FRMS práctico, recomendaciones operativas, cafeína, hidratación, entrenamiento, nutrición y sueño.
- El tono debe ser profesional, claro y humano.
- Incluye una frase estoica y una frase inspirada en Eclesiástico al final.
- No uses tablas.
- Mantén formato limpio con secciones cortas.
"""

    user_prompt = f"""
Genera el plan diario con este contexto JSON:

{json.dumps(payload, ensure_ascii=False, indent=2)}

Formato de salida exacto:

🧠 Estado del día
...
✈️ Asignación de hoy
...
⚠️ FRMS
...
🛫 Recomendaciones operativas
...
☕ Cafeína
...
💧 Hidratación
...
🏋️ Entrenamiento
...
🍽️ Time blocking nutricional
...
😴 Sueño y recuperación
...
📌 Recomendaciones extra
...
🪶 Frase estoica
...
📖 Frase del día
...

Hazlo concreto, útil y orientado a decisiones.
"""

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
    )
    return response.output_text.strip()


def generate_fallback_plan(payload):
    state_label = payload["state_label"]
    assignment = payload.get("assignment")
    quotes = payload["quotes"]

    parts = [
        f"🧠 Estado del día\n{state_label}",
        "",
        f"VFC: {payload['metrics_today']['vfc_7d']}",
        f"Sueño: {payload['metrics_today']['sleep_hhmm']} ({payload['metrics_today']['sleep_hours']:.1f}h)",
        f"Score: {payload['metrics_today']['sleep_score']}",
        "",
    ]

    assignment_text = format_assignment(assignment)
    if assignment_text:
        parts.append(assignment_text)
        parts.append("")

    parts.append("⚠️ FRMS")
    if state_label == "🔴 ROJO":
        parts.append("Riesgo aumentado. Hoy toca modo conservador y recuperación prioritaria.")
    elif state_label == "🟡 AMARILLO":
        parts.append("Riesgo intermedio. Controla carga y evita sobre exigirte.")
    else:
        parts.append("Riesgo bajo si mantienes orden, hidratación y buen cierre de sueño.")
    parts.append("")

    parts.append("🛫 Recomendaciones operativas")
    parts.append("Checklist consciente, foco en prioridades y evita multitarea innecesaria.")
    parts.append("")

    parts.append("☕ Cafeína")
    parts.append("Úsala de forma estratégica según la carga del día.")
    parts.append("")

    parts.append("💧 Hidratación")
    parts.append("Empieza hidratado y mantén agua disponible durante la jornada.")
    parts.append("")

    parts.append("🏋️ Entrenamiento")
    if state_label == "🔴 ROJO":
        parts.append("Recovery, caminar o movilidad.")
    elif state_label == "🟡 AMARILLO":
        parts.append("Moderado, técnica o fuerza ligera.")
    else:
        parts.append("Buen día para sesión fuerte.")
    parts.append("")

    parts.append("🍽️ Time blocking nutricional")
    for b in payload["time_blocks_base"]:
        parts.append(f"- {b}")
    parts.append("")

    parts.append("😴 Sueño y recuperación")
    parts.append("Protege el sueño de esta noche y baja estímulo al final del día.")
    parts.append("")

    parts.append("📌 Recomendaciones extra")
    parts.append("Hoy prioriza claridad, orden y consistencia.")
    parts.append("")

    parts.append("🪶 Frase estoica")
    parts.append(quotes["stoic"])
    parts.append("")
    parts.append("📖 Frase del día")
    parts.append(quotes["ecclesiasticus"])

    return "\n".join(parts)


# =========================
# COMANDOS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot listo 🚀\n\n"
        "1. Súbeme tu roster PDF una sola vez\n"
        "2. Usa /plan cada día\n"
        "3. Si subes otro roster, reemplazo el anterior automáticamente"
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    tg_file = await context.bot.get_file(update.message.document.file_id)
    pdf_bytes = await tg_file.download_as_bytearray()

    text = extract_text(pdf_bytes)
    assignments_by_date = parse_roster_assignments(text)

    USER_DATA.setdefault(user_id, {})
    USER_DATA[user_id]["roster_text"] = text
    USER_DATA[user_id]["assignments_by_date"] = assignments_by_date
    USER_DATA[user_id].setdefault("metrics_by_day", {})
    USER_DATA[user_id]["conversation_state"] = None

    save_data()

    await update.message.reply_text(
        "Roster cargado ✅\nLo usaré automáticamente hasta que subas uno nuevo."
    )


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    if user_id not in USER_DATA or "roster_text" not in USER_DATA[user_id]:
        await update.message.reply_text("Primero súbeme tu roster PDF.")
        return

    USER_DATA[user_id]["conversation_state"] = "awaiting_vfc"
    USER_DATA[user_id]["pending_plan"] = {"date": today_local_str()}
    save_data()

    await update.message.reply_text("¿Cuál es tu VFC de 7 días?")


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


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)
    if user_id in USER_DATA:
        USER_DATA[user_id]["conversation_state"] = None
        USER_DATA[user_id].pop("pending_plan", None)
        save_data()

    await update.message.reply_text("Estado de conversación reiniciado.")


# =========================
# FLUJO CONVERSACIONAL
# =========================

async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = get_user_key(update.effective_user.id)

    if user_id not in USER_DATA:
        return

    state = USER_DATA[user_id].get("conversation_state")
    if not state:
        return

    text = update.message.text.strip()

    if state == "awaiting_vfc":
        try:
            vfc = int(text)
        except ValueError:
            await update.message.reply_text("Pásame solo el número de VFC. Ejemplo: 50")
            return

        USER_DATA[user_id]["pending_plan"]["vfc"] = vfc
        USER_DATA[user_id]["conversation_state"] = "awaiting_sleep"
        save_data()

        await update.message.reply_text("¿Cuántas horas dormiste? Escríbelo en formato hh:mm, por ejemplo 04:05")
        return

    if state == "awaiting_sleep":
        if not re.fullmatch(r"\d{1,2}:\d{2}", text):
            await update.message.reply_text("Formato inválido. Escríbelo como hh:mm, por ejemplo 04:05")
            return

        USER_DATA[user_id]["pending_plan"]["sleep_hhmm"] = text
        USER_DATA[user_id]["pending_plan"]["sleep_hours"] = round(hhmm_to_hours(text), 2)
        USER_DATA[user_id]["conversation_state"] = "awaiting_score"
        save_data()

        await update.message.reply_text("¿Cuál fue tu score de sueño?")
        return

    if state == "awaiting_score":
        try:
            score = int(text)
        except ValueError:
            await update.message.reply_text("Pásame solo el número del score. Ejemplo: 53")
            return

        pending = USER_DATA[user_id].get("pending_plan", {})
        date_str = pending.get("date", today_local_str())
        vfc = pending["vfc"]
        sleep_hhmm = pending["sleep_hhmm"]
        sleep_hours = pending["sleep_hours"]

        USER_DATA[user_id].setdefault("metrics_by_day", {})
        USER_DATA[user_id]["metrics_by_day"][date_str] = {
            "vfc": vfc,
            "sleep_hhmm": sleep_hhmm,
            "sleep_hours": sleep_hours,
            "score": score,
            "saved_at": now_local().isoformat()
        }

        USER_DATA[user_id]["conversation_state"] = None
        USER_DATA[user_id].pop("pending_plan", None)
        save_data()

        assignments_by_date = USER_DATA[user_id].get("assignments_by_date", {})
        assignment = find_assignment_for_today(assignments_by_date, date_str)

        payload = build_ai_payload(
            USER_DATA[user_id],
            date_str,
            vfc,
            sleep_hhmm,
            sleep_hours,
            score,
            assignment,
        )

        try:
            plan_text = generate_ai_plan(payload)
        except Exception:
            plan_text = generate_fallback_plan(payload)

        plan_text += "\n\n💾 Métricas guardadas para esta fecha."
        await update.message.reply_text(plan_text)
        return


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
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    app.run_polling()


if __name__ == "__main__":
    main()
