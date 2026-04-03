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
DATA_FILE = "data.json"
USER_DATA = {}

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

# =========================
# UTILS
# =========================
def now():
    return datetime.now(ZoneInfo("America/Mexico_City"))

def today():
    return now().strftime("%Y-%m-%d")

def hhmm_to_hours(h):
    h,m = map(int,h.split(":"))
    return h + m/60

def extract_times(text):
    return re.findall(r"\d{2}:\d{2}", text)

def extract_time(text):
    t = extract_times(text)
    return t[0] if t else None

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
# ROSTER
# =========================
def parse_roster(text):
    roster = {}
    current = None

    for line in text.split("\n"):
        if re.match(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)", line):
            parts = line.split()
            current = f"{parts[0]} {parts[1]}"
            roster[current] = {"flights":[]}

        elif current and "Y4" in line:
            roster[current]["flights"].append(line)

    return roster

def key_today():
    return now().strftime("%b %d").upper()

def key_tomorrow():
    return (now()+timedelta(days=1)).strftime("%b %d").upper()

# =========================
# HORAS
# =========================
def calc_hours(flights):
    total=0
    for f in flights:
        t=extract_times(f)
        if len(t)>=2:
            h1,m1=map(int,t[-2].split(":"))
            h2,m2=map(int,t[-1].split(":"))

            t1=h1*60+m1
            t2=h2*60+m2
            total += (t2-t1)%1440
    return total/60

# =========================
# ANALISIS ROSTER
# =========================
def roster_analysis(roster):
    days=[]
    for d,info in roster.items():
        h=calc_hours(info["flights"])
        days.append((d,h))

    total=sum(h for _,h in days)

    alerts=[]
    heavy=[d for d,h in days if h>7]

    for i in range(len(days)):
        w=days[i:i+7]
        if len(w)<7: continue

        h=sum(x[1] for x in w)

        if h>30:
            alerts.append(f"🔥 Exceso {w[0][0]}→{w[-1][0]} ({round(h,1)}h)")
        elif h>27:
            alerts.append(f"⚠️ Límite {w[0][0]}→{w[-1][0]} ({round(h,1)}h)")

    return {
        "total":round(total,1),
        "heavy":heavy,
        "alerts":alerts
    }

# =========================
# FATIGA
# =========================
def trend(user):
    m=user.get("metrics",{})
    d=sorted(m.keys())[-7:]

    if len(d)<3: return "sin datos"

    v=[m[x]["vfc"] for x in d]
    s=[m[x]["sleep"] for x in d]

    if v[-1]<v[0]-3: return "fatiga creciente"
    if sum(s)/len(s)<6: return "deuda sueño"

    return "estable"

def wocl(checkin):
    if not checkin: return "LOW"
    h=int(checkin.split(":")[0])

    if 2<=h<6: return "CRITICAL"
    if h<8: return "MODERATE"
    return "LOW"

def fatigue_score(vfc,sleep,trend,w):
    score=100

    if sleep<5: score-=40
    elif sleep<6: score-=25
    elif sleep<7: score-=10

    if vfc<48: score-=20
    elif vfc<50: score-=10

    if trend=="fatiga creciente": score-=15

    if w=="CRITICAL": score-=25
    elif w=="MODERATE": score-=10

    return max(score,0)

def level(score):
    if score<40: return "🔥 CRITICAL"
    if score<60: return "🔴 HIGH"
    if score<80: return "🟡 MODERATE"
    return "🟢 LOW"

# =========================
# SLEEP PLAN
# =========================
def sleep_plan(next):
    if not next: return None

    t=extract_time(next.get("raw",""))
    if not t: return None

    h,m=map(int,t.split(":"))
    c=h*60+m

    wake=c-120
    sleep=wake-(8*60)

    return {
        "checkin":t,
        "wake":f"{wake//60:02d}:{wake%60:02d}",
        "sleep":f"{sleep//60:02d}:{sleep%60:02d}"
    }

# =========================
# IA
# =========================
def ai(payload):
    system="""
Eres coach elite + FRMS tipo AIMS.

Haz briefing personal:
- estado
- riesgo
- impacto operativo
- decisión clave
- time blocking
- sueño para mañana

Habla humano, no robot.
"""

    user=f"""
{json.dumps(payload,indent=2)}
"""

    r=client.chat.completions.create(
        model=MODEL,
        messages=[{"role":"system","content":system},
                  {"role":"user","content":user}],
        temperature=0.9
    )

    return r.choices[0].message.content

# =========================
# TELEGRAM
# =========================
async def pdf(update,context):
    u=str(update.effective_user.id)

    f=await context.bot.get_file(update.message.document.file_id)
    b=await f.download_as_bytearray()

    t=""
    for p in PdfReader(io.BytesIO(b)).pages:
        t+=p.extract_text() or ""

    r=parse_roster(t)
    a=roster_analysis(r)

    USER_DATA.setdefault(u,{})
    USER_DATA[u]["roster"]=r
    USER_DATA[u]["analysis"]=a

    save()

    await update.message.reply_text(f"""
Roster cargado ✅

✈️ {a['total']}h
⚠️ {len(a['heavy'])} días pesados
🚨 {len(a['alerts'])} alertas
""")

async def plan(update,context):
    u=str(update.effective_user.id)
    USER_DATA.setdefault(u,{})

    USER_DATA[u]["state"]="vfc"
    USER_DATA[u]["temp"]={"date":today()}

    await update.message.reply_text("VFC?")

async def capture(update,context):
    u=str(update.effective_user.id)
    s=USER_DATA.get(u,{}).get("state")

    if not s: return

    txt=update.message.text

    if s=="vfc":
        USER_DATA[u]["temp"]["vfc"]=int(txt)
        USER_DATA[u]["state"]="sleep"
        await update.message.reply_text("Sueño hh:mm?")
        return

    if s=="sleep":
        USER_DATA[u]["temp"]["sleep"]=hhmm_to_hours(txt)
        USER_DATA[u]["state"]="score"
        await update.message.reply_text("Score?")
        return

    if s=="score":
        temp=USER_DATA[u]["temp"]
        temp["score"]=int(txt)

        USER_DATA.setdefault(u,{}).setdefault("metrics",{})
        USER_DATA[u]["metrics"][temp["date"]]=temp

        r=USER_DATA[u].get("roster",{})
        today_r=r.get(key_today())
        tomorrow_r=r.get(key_tomorrow())

        tr=trend(USER_DATA[u])
        ck=extract_time(tomorrow_r["flights"][0]) if tomorrow_r and tomorrow_r["flights"] else None

        w=wocl(ck)
        sc=fatigue_score(temp["vfc"],temp["sleep"],tr,w)

        payload={
            **temp,
            "trend":tr,
            "fatigue_score":sc,
            "fatigue_level":level(sc),
            "wocl":w,
            "today":today_r,
            "tomorrow":tomorrow_r,
            "sleep_plan":sleep_plan(tomorrow_r)
        }

        txt=ai(payload)

        USER_DATA[u]["state"]=None
        save()

        await update.message.reply_text(txt)

# =========================
# MAIN
# =========================
def main():
    load()

    app=Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    app.add_handler(MessageHandler(filters.Document.PDF,pdf))
    app.add_handler(CommandHandler("plan",plan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,capture))

    app.run_polling()

if __name__=="__main__":
    main()
