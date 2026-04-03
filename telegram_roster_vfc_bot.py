import os
import re
import io
import json
from datetime import datetime, timedelta
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
# UTILIDADES GENERALES
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


def parse_hhmm_safe(value: str | None):
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
    """
    Extrae asignaciones agrupadas por fecha.
    Soporta:
    - líneas de vuelo con o sin check-in
    - HTL / hotel
    - agrupación por fecha
    """
    lines = normalize_lines(text)
    assignments_by_date = {}
    current_date = None

    date_pattern = re.compile(
        r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d{2})([A-Z]{3})\s+(.*)$"
    )

    # Y45722 04:32 GDL 05:52 LAX 08:15 03:23 03:23
    flight_with_ci = re.compile(
        r"^(Y\d+)\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})"
    )

    # Y45723 LAX 09:45 GDL 13:52 14:22 03:07 03:07
    flight_no_ci = re.compile(
        r"^(Y\d+)\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+([A-Z]{3})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})"
    )

    # HTL SEA 20:19
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
    """
    WOCL simple:
    - vuelo/llegada dentro de 02:00–05:59
    - o check-in muy temprano que claramente invade madrugada
    """
    if not assignment:
        return False

    windows = []
    for key in ["check_in", "first_departure", "last_arrival", "last_check_out"]:
        value = assignment.get(key)
        if value:
            windows.append(hhmm_to_minutes(value))

    for m in windows:
        if hhmm_to_minutes("02:00") <= m <= hhmm_to_minutes("05:59"):
            return True

    return False


def is_long_duty(assignment):
    if not assignment or not assignment.get("duty_time_minutes"):
        return False
    return assignment["duty_time_minutes"] >= 10 * 60


def suggest_nap_window(assignment):
    if not assignment:
        return None

    check_in = assignment.get("check_in")
    duty_type = assignment.get("duty_type")

    if duty_type == "temprano" and check_in:
        # siesta previa ideal el día anterior realmente, pero damos ventana útil
        return "Si puedes, siesta breve de 20–30 min al terminar la jornada o bloque de recuperación temprana."
    if duty_type == "nocturno":
        return "Si puedes, siesta previa de 60–90 min antes del duty."
    if duty_type == "tarde":
        return "Si te notas bajo, siesta breve de 20–30 min al mediodía puede ayudar."
    return None


def build_time_blocks(assignment, state_label):
    """
    Time blocking aproximado por tipo de jornada y check-in.
    """
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

    if state_label == "🔴 ROJO":
        blocks.append("Hoy evita ultraprocesado, grasa pesada y exceso de azúcar.")
    elif state_label == "🟢 VERDE":
        blocks.append("Hoy toleras mejor carbs complejos si entrenas o el día exige más.")

    return blocks


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

    duty_type = classify_duty_type(first_check_in, first_std, last_sta)
    has_pernocta = hotel is not None

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
        "duty_type": duty_type,
        "pernocta": has_pernocta,
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
# ANÁLISIS Y RECOMENDACIONES
# =========================

def frms(vfc, sleep, assignment=None):
    risk = "🟢 LOW"
    drivers = []

    if sleep < 5:
        risk = "🔴 HIGH"
        drivers.append("sueño muy corto")
    elif sleep < 6:
        risk = "🟡 MODERATE"
        drivers.append("sueño insuficiente")

    if assignment:
        if assignment.get("duty_type") == "temprano":
            drivers.append("inicio temprano")
        if assignment.get("duty_type") == "nocturno":
            drivers.append("jornada nocturna")
        if assignment.get("wocl"):
            drivers.append("WOCL")
        if assignment.get("sectors", 0) >= 4:
            drivers.append("múltiples sectores")
        if assignment.get("long_duty"):
            drivers.append("jornada larga")

        if risk == "🟢 LOW" and (assignment.get("wocl") or assignment.get("long_duty")):
            risk = "🟡 MODERATE"

    lines = [
        "⚠️ FRMS",
        f"Riesgo: {risk}",
    ]

    if drivers:
        lines.append(f"Factores: {', '.join(drivers)}")

    if risk.startswith("🔴"):
        lines.append("Opera en modo conservador, evita carga extra y prioriza recuperación agresiva hoy.")
    elif risk.startswith("🟡"):
        lines.append("Controla la carga, cuida el ritmo mental y no te exijas fuera del duty.")
    else:
        lines.append("Jornada razonablemente controlable si mantienes buena energía, hidratación y cierre de sueño.")

    return "\n".join(lines)


def operational_recommendations(state, assignment=None):
    lines = ["🛫 Recomendaciones operativas"]

    if state == "🔴 ROJO":
        lines.append("Checklist más consciente, sin automatismos.")
        lines.append("Verbaliza confirmaciones clave.")
        lines.append("Evita multitarea innecesaria.")
        lines.append("Reduce decisiones no esenciales fuera del duty.")
    elif state == "🟡 AMARILLO":
        lines.append("Opera con margen conservador.")
        lines.append("Cuida el ritmo mental y evita sobrecargarte después del duty.")
    else:
        lines.append("Buen día para operar con normalidad.")
        lines.append("Aun así, protege sueño e hidratación para sostener el rendimiento.")

    if assignment:
        duty_type = assignment.get("duty_type")
        if duty_type == "temprano":
            lines.append("Por inicio temprano: activación ligera antes del duty y no gastar energía de más al despertar.")
        elif duty_type == "nocturno":
            lines.append("Por jornada nocturna: prioriza ritmo estable, digestión ligera y manejo estratégico de alerta.")
        elif duty_type == "tarde":
            lines.append("Por jornada de tarde: no te cargues mental ni físicamente antes de reportar.")

        if assignment.get("sectors", 0) >= 4:
            lines.append("Por múltiples sectores: simplifica foco, comida y administración de energía.")

    return "\n".join(lines)


def caffeine_recommendations(state, assignment=None):
    lines = ["☕ Cafeína"]

    if state == "🔴 ROJO":
        lines.append("Úsala de forma estratégica, no impulsiva.")
        lines.append("Mejor 1–2 tomas moderadas que una carga grande.")
    elif state == "🟡 AMARILLO":
        lines.append("Úsala para sostener rendimiento, no para tapar agotamiento.")
    else:
        lines.append("Mantén uso moderado; no necesitas sobrecarga.")

    if assignment:
        duty_type = assignment.get("duty_type")
        if duty_type == "temprano":
            lines.append("Primer consumo cerca del despertar o inicio del duty.")
        elif duty_type == "nocturno":
            lines.append("Úsala en la primera mitad de la jornada; no te pases si quieres dormir al terminar.")
        else:
            lines.append("Evita consumirla muy tarde si compromete el sueño de la noche.")

    return "\n".join(lines)


def hydration_recommendations(assignment=None):
    lines = ["💧 Hidratación"]
    lines.append("Empieza el día hidratado, no esperes a sentir sed.")
    lines.append("Mantén agua disponible durante la jornada.")
    lines.append("Electrolitos pueden ayudar si dormiste poco o vienes drenado.")

    if assignment:
        if assignment.get("flight_time_minutes", 0) >= 4 * 60:
            lines.append("Por el tiempo de vuelo acumulado, sube un poco la atención a hidratación.")
        if assignment.get("pernocta"):
            lines.append("Si hay pernocta, evita llegar deshidratado al hotel.")

    return "\n".join(lines)


def training_recommendations(state, assignment=None):
    lines = ["🏋️ Entrenamiento"]

    if state == "🔴 ROJO":
        lines.append("Recovery: caminar, movilidad o nada.")
    elif state == "🟡 AMARILLO":
        lines.append("Moderado: técnica, fuerza ligera o caminata.")
    else:
        lines.append("Buen día para HIIT / Freeletics o sesión fuerte.")

    if assignment:
        if assignment.get("duty_type") in ("temprano", "nocturno"):
            lines.append("Con este tipo de jornada, recuperación suele valer más que volumen.")
        if assignment.get("long_duty"):
            lines.append("Por la jornada larga, evita meter un segundo gran estrés físico si acabas cansado.")

    return "\n".join(lines)


def meal_time_blocking(state_label, assignment=None):
    lines = ["🍽️ Time blocking nutricional"]

    blocks = build_time_blocks(assignment, state_label)
    for block in blocks:
        lines.append(block)

    return "\n".join(lines)


def sleep_recommendations(state, assignment=None):
    lines = ["😴 Sueño y recuperación"]

    if state == "🔴 ROJO":
        lines.append("Objetivo: 7.5–8 h hoy sí o sí.")
        lines.append("Baja estímulo al final del día.")
    elif state == "🟡 AMARILLO":
        lines.append("Objetivo: 7–8 h para no acumular fatiga.")
    else:
        lines.append("Objetivo: mantener 7–8 h y conservar buen estado.")

    if assignment:
        duty_type = assignment.get("duty_type")
        if duty_type == "temprano":
            lines.append("Acuéstate más temprano de lo normal.")
        elif duty_type == "nocturno":
            lines.append("Si puedes, mete descanso previo antes del duty.")
        if assignment.get("pernocta"):
            lines.append("En hotel: oscuridad, temperatura fresca y rutina simple para dormir.")

        if assignment.get("nap_window"):
            lines.append(assignment["nap_window"])

    return "\n".join(lines)


def extra_recommendations(state, assignment=None):
    lines = ["📌 Recomendaciones extra"]

    if state == "🔴 ROJO":
        lines.append("Hoy la meta no es rendir al máximo, es no empeorar el sistema.")
    elif state == "🟡 AMARILLO":
        lines.append("Buen día para cumplir bien, sin forzarte de más.")
    else:
        lines.append("Día útil para avanzar con más intensidad si el contexto acompaña.")

    if assignment:
        if assignment.get("sectors", 0) >= 4:
            lines.append("Simplifica comida, foco y ritmo mental por el número de sectores.")
        if assignment.get("flight_time_minutes", 0) >= 5 * 60:
            lines.append("Vigila postura, tensión e hidratación por las horas de vuelo.")
        if assignment.get("long_duty"):
            lines.append("Evita compromisos fuertes después del duty.")
        if assignment.get("wocl"):
            lines.append("Si toca WOCL, protege con más fuerza el sueño posterior.")

    return "\n".join(lines)


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
        state_label = classify_day(vfc, sleep_hours)

        parts = [
            f"🧠 {state_label}",
            f"Fecha: {date_str}",
            f"VFC: {vfc}",
            f"Sueño: {sleep_hhmm} ({sleep_hours:.1f}h)",
            f"Score: {score}",
            "",
        ]

        assignment_text = format_assignment(assignment)
        if assignment_text:
            parts.append(assignment_text)
            parts.append("")

        parts.append(frms(vfc, sleep_hours, assignment))
        parts.append("")
        parts.append(operational_recommendations(state_label, assignment))
        parts.append("")
        parts.append(caffeine_recommendations(state_label, assignment))
        parts.append("")
        parts.append(hydration_recommendations(assignment))
        parts.append("")
        parts.append(training_recommendations(state_label, assignment))
        parts.append("")
        parts.append(meal_time_blocking(state_label, assignment))
        parts.append("")
        parts.append(sleep_recommendations(state_label, assignment))
        parts.append("")
        parts.append(extra_recommendations(state_label, assignment))
        parts.append("")
        parts.append("💾 Métricas guardadas para esta fecha.")

        await update.message.reply_text("\n".join(parts))
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
