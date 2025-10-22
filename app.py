# -*- coding: utf-8 -*-
import os, json, math, asyncio, threading
import datetime as dt
from typing import List, Dict, Tuple

from http.server import BaseHTTPRequestHandler, HTTPServer

def _start_keepalive_server():
    port = int(os.getenv("PORT", "8000"))
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        def log_message(self, *args): return
    srv = HTTPServer(("0.0.0.0", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

_start_keepalive_server()

from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from astral import LocationInfo
from astral.sun import sun
from astral import moon as amoon

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from providers import OpenMeteoProvider, OpenWeatherProvider, WindyProvider, VisualCrossingProvider, WeatherProvider

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENWEATHER_API_KEY = (os.getenv("OPENWEATHER_API_KEY", "") or "").strip() or None
WINDY_API_KEY = (os.getenv("WINDY_API_KEY", "") or "").strip() or None
VISUALCROSSING_API_KEY = (os.getenv("VISUALCROSSING_API_KEY", "") or "").strip() or None

LAT = float(os.getenv("LAT", "55.85"))
LON = float(os.getenv("LON", "38.45"))
CLOUD_THRESHOLD = float(os.getenv("CLOUD_THRESHOLD", "35"))
PRECIP_THRESHOLD = float(os.getenv("PRECIP_THRESHOLD", "20"))
MIN_WINDOW_HOURS = float(os.getenv("MIN_WINDOW_HOURS", "1.0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
DAILY_NOTIFY_HOUR = int(os.getenv("DAILY_NOTIFY_HOUR", "15"))
DAILY_NOTIFY_MINUTE = int(os.getenv("DAILY_NOTIFY_MINUTE", "0"))
CLEAR_NIGHT_THRESHOLD = float(os.getenv("CLEAR_NIGHT_THRESHOLD", "60"))
SHOW_SOURCES = os.getenv("SHOW_SOURCES", "0") == "1"

USE_MOON_FILTER = os.getenv("USE_MOON_FILTER", "0") == "1"
MOON_MAX_ILLUM = float(os.getenv("MOON_MAX_ILLUM", "40"))

CHAT_DB = os.getenv("CHAT_DB", "chat_ids.json")
CHAT_PATH = os.path.join(os.path.dirname(__file__), CHAT_DB)

def load_chats() -> List[int]:
    if os.path.exists(CHAT_PATH):
        with open(CHAT_PATH, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except Exception: return []
    return []

def save_chat(chat_id: int):
    chats = load_chats()
    if chat_id not in chats:
        chats.append(chat_id)
        with open(CHAT_PATH, "w", encoding="utf-8") as f:
            json.dump(chats, f)

def build_active_providers():
    providers = [OpenMeteoProvider()]; names = ["Open-Meteo"]
    if OPENWEATHER_API_KEY:
        providers.append(OpenWeatherProvider(OPENWEATHER_API_KEY)); names.append("OpenWeather")
    if WINDY_API_KEY:
        providers.append(WindyProvider(WINDY_API_KEY)); names.append("Windy")
    if VISUALCROSSING_API_KEY:
        providers.append(VisualCrossingProvider(VISUALCROSSING_API_KEY)); names.append("VisualCrossing")
    return providers, names

def night_window(date_local: dt.date, tz: ZoneInfo) -> Tuple[dt.datetime, dt.datetime]:
    loc = LocationInfo(latitude=LAT, longitude=LON)
    s1 = sun(loc.observer, date=date_local, tzinfo=tz)
    s2 = sun(loc.observer, date=(date_local + dt.timedelta(days=1)), tzinfo=tz)
    sunset = s1["sunset"]; sunrise = s2["sunrise"]
    dusk_astro = sunset + dt.timedelta(minutes=90)
    dawn_astro = sunrise - dt.timedelta(minutes=90)
    if dawn_astro <= dusk_astro:
        dusk_astro = sunset; dawn_astro = sunrise
    return dusk_astro, dawn_astro

def moon_info(date_local: dt.date, tz: ZoneInfo):
    loc = LocationInfo(latitude=LAT, longitude=LON)
    age = float(amoon.phase(date_local))
    frac = (1.0 - math.cos(2*math.pi*age/29.530588)) / 2.0
    illum = max(0.0, min(1.0, frac)) * 100.0
    # Try computing rise/set for date and next day to cover overnight crossing
    def _safe(func, day):
        try:
            return func(loc.observer, date=day, tzinfo=tz)
        except Exception:
            return None
    rise0 = _safe(amoon.moonrise, date_local)
    set0  = _safe(amoon.moonset,  date_local)
    rise1 = _safe(amoon.moonrise, date_local + dt.timedelta(days=1))
    set1  = _safe(amoon.moonset,  date_local + dt.timedelta(days=1))

    intervals = []
    # Build up to two intervals when the Moon is above horizon spanning the night
    if rise0 and set0:
        if rise0 < set0:
            intervals.append((rise0, set0))
        else:
            intervals.append((rise0, date_local + dt.timedelta(days=1)))
            if set1:
                intervals.append((date_local + dt.timedelta(days=1), set1))
    elif rise0 and not set0:
        intervals.append((rise0, date_local + dt.timedelta(days=1)))
    elif set0 and not rise0:
        intervals.append((date_local, set0))
    elif not rise0 and not set0 and rise1 and set1:
        # handle case when both events fall on next day only (rare)
        if rise1 < set1:
            intervals.append((date_local + dt.timedelta(days=1), set1))

    return illum, age, intervals

def classify_moon_vs_night(dusk: dt.datetime, dawn: dt.datetime, tz: ZoneInfo):
    """Return text label and list of overlaps within night window."""
    illum, age, intervals = moon_info(dusk.date(), tz)
    overlaps = []
    for a,b in intervals:
        s = max(a, dusk); e = min(b, dawn)
        if s < e:
            overlaps.append((s,e))
    if intervals and not overlaps:
        status = "вне ночного окна"
    elif overlaps:
        status = "над горизонтом"
    else:
        status = "нет данных о восх./заходе"
    return illum, age, intervals, overlaps, status

def filter_by_moon(averaged: Dict[int, Dict[str, float]], dusk: dt.datetime, dawn: dt.datetime, tz: ZoneInfo) -> Dict[int, Dict[str, float]]:
    if not USE_MOON_FILTER or not averaged:
        return averaged
    illum, age, intervals, overlaps, status = classify_moon_vs_night(dusk, dawn, tz)
    if illum <= MOON_MAX_ILLUM or not overlaps:
        return averaged
    blocks = [(int(a.timestamp()), int(b.timestamp())) for a,b in overlaps]
    filtered = {ts:v for ts,v in averaged.items() if not any(a <= ts < b for a,b in blocks)}
    return filtered

async def fetch_all_providers(start: dt.datetime, end: dt.datetime):
    providers, names = build_active_providers()
    results: Dict[int, Dict[str, List[float]]] = {}
    contrib = {n: 0 for n in names}
    tasks = [p.fetch_hours(LAT, LON, start, end) for p in providers]
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    for name, batch in zip(names, batches):
        if isinstance(batch, Exception):
            continue
        for point in batch:
            ts = int(point["ts"])
            cell = results.setdefault(ts, {"cloud": [], "precip_prob": []})
            cloud = point.get("cloud", float("nan"))
            if not math.isnan(cloud):
                cell["cloud"].append(cloud); contrib[name] += 1
            if "precip_prob" in point:
                cell["precip_prob"].append(point["precip_prob"])
    averaged: Dict[int, Dict[str, float]] = {}
    for ts, vals in results.items():
        if not vals["cloud"] and not vals["precip_prob"]:
            continue
        avg_cloud = sum(vals["cloud"])/len(vals["cloud"]) if vals["cloud"] else 100.0
        avg_precip = sum(vals["precip_prob"])/len(vals["precip_prob"]) if vals["precip_prob"] else 0.0
        averaged[ts] = {"cloud": avg_cloud, "precip_prob": avg_precip}
    averaged = dict(sorted(averaged.items()))
    return averaged, contrib, names

def summarize_windows(averaged: Dict[int, Dict[str, float]]) -> List[Tuple[int,int]]:
    allowed_ts = [ts for ts, v in averaged.items() if v["cloud"] <= CLOUD_THRESHOLD and v["precip_prob"] <= PRECIP_THRESHOLD]
    if not allowed_ts: return []
    allowed_ts.sort()
    windows: List[Tuple[int,int]] = []
    start = allowed_ts[0]; prev = start
    for ts in allowed_ts[1:]:
        if ts - prev == 3600: prev = ts
        else:
            windows.append((start, prev+3600)); start = ts; prev = ts
    windows.append((start, prev+3600))
    out = []
    for a,b in windows:
        if (b-a)/3600.0 >= MIN_WINDOW_HOURS: out.append((a,b))
    return out

def compute_clear_fraction(averaged: Dict[int, Dict[str, float]], dusk: dt.datetime, dawn: dt.datetime) -> float:
    if not averaged: return 0.0
    hrs = [ts for ts in averaged.keys() if int(dusk.timestamp()) <= ts < int(dawn.timestamp())]
    if not hrs: return 0.0
    good = [ts for ts in hrs if averaged[ts]["cloud"] <= CLOUD_THRESHOLD and averaged[ts]["precip_prob"] <= PRECIP_THRESHOLD]
    return 100.0 * len(good) / len(hrs)

def make_summary_line(clear_pct: float, windows: List[Tuple[int,int]], tz: ZoneInfo) -> str:
    if clear_pct >= CLEAR_NIGHT_THRESHOLD and windows:
        return f"🌙 Сегодня почти вся ночь ясная — отличные условия для съёмки! (ясных часов: {clear_pct:.0f}%)"
    if windows:
        longest = max(windows, key=lambda w: w[1]-w[0])
        a, b = longest
        return f"🌥 Просветы возможны с {dt.datetime.fromtimestamp(a, tz).strftime('%H:%M')} до {dt.datetime.fromtimestamp(b, tz).strftime('%H:%M')} — можно попробовать."
    return "☁️ Всё небо затянуто — съёмку отменяем."

def fmt_report(date_local: dt.date, dusk: dt.datetime, dawn: dt.datetime,
               averaged: Dict[int, Dict[str, float]], windows: List[Tuple[int,int]],
               tz: ZoneInfo, contrib: Dict[str, int]) -> str:
    clear_pct = compute_clear_fraction(averaged, dusk, dawn)
    header = make_summary_line(clear_pct, windows, tz)

    illum, age, intervals, overlaps, status = classify_moon_vs_night(dusk, dawn, tz)
    moon_line = f"Луна: {illum:.0f}% (возраст {age:.1f} д)"
    if status == "над горизонтом" and overlaps:
        spans = [f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}" for a,b in overlaps]
        moon_line += " • над горизонтом: " + ", ".join(spans)
    elif status == "вне ночного окна" and intervals:
        spans = [f"{a.strftime('%d.%m %H:%M')}–{b.strftime('%d.%m %H:%M')}" for a,b in intervals]
        moon_line += " • над горизонтом (вне ночного окна): " + ", ".join(spans)
    else:
        moon_line += " • данные о восходе/заходе недоступны"

    if not averaged:
        base = "Нет данных с погодных сервисов на выбранный интервал."
    elif not windows:
        base = (f"Сегодня ({date_local.strftime('%d.%m.%Y')}) ночью над Ногинским районом облачно или осадки.\n"
                f"Окно ночи: {dusk.strftime('%H:%M')}–{dawn.strftime('%H:%M')} МСК.\n" + moon_line)
    else:
        parts = [
            f"Прогноз на ночь {date_local.strftime('%d.%m.%Y')} (Ногинский район):",
            f"Окно ночи: {dusk.strftime('%H:%M')}–{dawn.strftime('%H:%M')} МСК.",
            moon_line,
            "Окна для съёмки (средняя облачность/осадки по источникам):"
        ]
        for a,b in windows:
            hours = [averaged[ts]["cloud"] for ts in sorted(averaged) if a <= ts < b]
            avgc = sum(hours)/len(hours) if hours else 0.0
            parts.append(f"• {dt.datetime.fromtimestamp(a, tz).strftime('%H:%M')}–{dt.datetime.fromtimestamp(b, tz).strftime('%H:%M')}  (ср. облачн.: {avgc:.0f}%)")
        base = "\n".join(parts)

    if os.getenv("SHOW_SOURCES","0")=="1":
        used = [f"{k}:{v}" for k,v in contrib.items() if v > 0]
        base += "\n\nИсточник(и): " + (", ".join(used) if used else "нет данных")
    return header + "\n\n" + base

async def fetch_all_providers(start: dt.datetime, end: dt.datetime):
    providers, names = build_active_providers()
    results: Dict[int, Dict[str, List[float]]] = {}
    contrib = {n: 0 for n in names}
    tasks = [p.fetch_hours(LAT, LON, start, end) for p in providers]
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    for name, batch in zip(names, batches):
        if isinstance(batch, Exception):
            continue
        for point in batch:
            ts = int(point["ts"])
            cell = results.setdefault(ts, {"cloud": [], "precip_prob": []})
            cloud = point.get("cloud", float("nan"))
            if not math.isnan(cloud):
                cell["cloud"].append(cloud); contrib[name] += 1
            if "precip_prob" in point:
                cell["precip_prob"].append(point["precip_prob"])
    averaged: Dict[int, Dict[str, float]] = {}
    for ts, vals in results.items():
        if not vals["cloud"] and not vals["precip_prob"]:
            continue
        avg_cloud = sum(vals["cloud"])/len(vals["cloud"]) if vals["cloud"] else 100.0
        avg_precip = sum(vals["precip_prob"])/len(vals["precip_prob"]) if vals["precip_prob"] else 0.0
        averaged[ts] = {"cloud": avg_cloud, "precip_prob": avg_precip}
    averaged = dict(sorted(averaged.items()))
    return averaged, contrib, names

async def build_message(date_local: dt.date, tz: ZoneInfo) -> str:
    dusk, dawn = night_window(date_local, tz)
    start_utc = dusk.astimezone(dt.timezone.utc); end_utc = dawn.astimezone(dt.timezone.utc)
    averaged, contrib, names = await fetch_all_providers(start_utc, end_utc)
    averaged2 = filter_by_moon(averaged, dusk, dawn, tz)
    windows = summarize_windows(averaged2)
    return fmt_report(date_local, dusk, dawn, averaged2, windows, tz, contrib)

# --- Telegram handlers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Привет! Команды:\n"
        "/now — прогноз на ближайшую ночь\n"
        "/tomorrow — прогноз на завтрашнюю ночь\n"
        "/setthresholds <облачн%> <осадки%>\n"
        "/setclear <процент> — порог «ясной ночи» (по умолчанию 60)\n"
        "/moonfilter <0|1> — выкл/вкл учёт Луны\n"
        "/moon — отладка Луны (освещённость, интервалы, пересечение с ночью)\n"
    )

async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()
    await update.message.reply_text(await build_message(today, tz))

async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    nxt = (dt.datetime.now(tz).date() + dt.timedelta(days=1))
    await update.message.reply_text(await build_message(nxt, tz))

async def setthresholds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CLOUD_THRESHOLD, PRECIP_THRESHOLD
    try:
        c = float(context.args[0]); p = float(context.args[1])
        CLOUD_THRESHOLD = c; PRECIP_THRESHOLD = p
        await update.message.reply_text(f"Ок! Пороги: облачность ≤ {c}%, осадки ≤ {p}%.")
    except Exception:
        await update.message.reply_text("Используйте формат: /setthresholds 40 20")

async def setclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CLEAR_NIGHT_THRESHOLD
    try:
        v = float(context.args[0])
        CLEAR_NIGHT_THRESHOLD = v
        await update.message.reply_text(f"Ок! Порог «ясной ночи» теперь {v:.0f}%.")
    except Exception:
        await update.message.reply_text("Используйте: /setclear 60")

async def moonfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global USE_MOON_FILTER
    try:
        val = int(context.args[0])
        USE_MOON_FILTER = (val == 1)
        await update.message.reply_text("Учёт Луны: " + ("включён" if USE_MOON_FILTER else "выключен"))
    except Exception:
        await update.message.reply_text("Используйте: /moonfilter 1  (или 0)")

async def moon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()
    dusk, dawn = night_window(today, tz)
    illum, age, intervals, overlaps, status = classify_moon_vs_night(dusk, dawn, tz)
    def _fmt_spans(spans):
        return ", ".join([f"{a.strftime('%d.%m %H:%M')}–{b.strftime('%d.%m %H:%M')}" for a,b in spans]) or "—"
    await update.message.reply_text(
        "Луна (отладка):\n"
        f"Освещённость: {illum:.0f}% (возраст {age:.1f} д)\n"
        f"Промежутки над горизонтом (24ч): {_fmt_spans(intervals)}\n"
        f"Пересечение с ночным окном: {_fmt_spans(overlaps)}\n"
        f"Статус: {status}"
    )

async def daily_job(app: Application):
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()
    msg = await build_message(today, tz)
    for chat_id in load_chats():
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg)
        except Exception:
            pass

def setup_scheduler(app: Application):
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    trigger = CronTrigger(hour=DAILY_NOTIFY_HOUR, minute=DAILY_NOTIFY_MINUTE)
    scheduler.add_job(lambda: daily_job(app), trigger)
    scheduler.start()

def main():
    if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN is not set")
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("now", now))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("setthresholds", setthresholds))
    application.add_handler(CommandHandler("setclear", setclear))
    application.add_handler(CommandHandler("moonfilter", moonfilter))
    application.add_handler(CommandHandler("moon", moon))
    setup_scheduler(application)
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
