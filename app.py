# -*- coding: utf-8 -*-
import os, json, math, asyncio, dt as _dt_import_block, threading
import datetime as dt
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass

# Tiny HTTP server to keep Render's required port open (keeps service "healthy" in Web Service mode)
from http.server import BaseHTTPRequestHandler, HTTPServer

def _start_keepalive_server():
    try:
        port = int(os.getenv("PORT", "8000"))
    except Exception:
        port = 8000
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            return
    try:
        srv = HTTPServer(("0.0.0.0", port), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[keepalive] HTTP keepalive server started on port {port}")
    except Exception as e:
        print(f"[keepalive] Failed to start keepalive server on port {port}: {e}")

# Start keepalive server BEFORE importing blocking stuff
_start_keepalive_server()

# --- imports used by bot logic ---
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from astral import LocationInfo
from astral.sun import sun

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Providers are expected to be in providers.py next to this file
try:
    from providers import OpenMeteoProvider, OpenWeatherProvider, WindyProvider, YandexWeatherProvider, WeatherProvider
except Exception as e:
    print("[warn] could not import providers.py:", e)
    # Fallback minimal stub to avoid crash if providers.py missing
    class WeatherProvider: pass
    class OpenMeteoProvider(WeatherProvider):
        async def fetch_hours(self, *args, **kwargs): return {}
    class OpenWeatherProvider(WeatherProvider):
        def __init__(self, key): pass
        async def fetch_hours(self, *args, **kwargs): return {}
    class WindyProvider(WeatherProvider):
        def __init__(self, key): pass
        async def fetch_hours(self, *args, **kwargs): return {}
    class YandexWeatherProvider(WeatherProvider):
        def __init__(self, key): pass
        async def fetch_hours(self, *args, **kwargs): return {}

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip() or None
WINDY_API_KEY = os.getenv("WINDY_API_KEY", "").strip() or None
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip() or None
LAT = float(os.getenv("LAT", "55.85"))
LON = float(os.getenv("LON", "38.45"))
CLOUD_THRESHOLD = float(os.getenv("CLOUD_THRESHOLD", "35"))
PRECIP_THRESHOLD = float(os.getenv("PRECIP_THRESHOLD", "20"))
MIN_WINDOW_HOURS = float(os.getenv("MIN_WINDOW_HOURS", "1.0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
DAILY_NOTIFY_HOUR = int(os.getenv("DAILY_NOTIFY_HOUR", "15"))
DAILY_NOTIFY_MINUTE = int(os.getenv("DAILY_NOTIFY_MINUTE", "0"))
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

def human_time(ts: int, tz: ZoneInfo) -> str:
    return dt.datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M")

async def fetch_all_providers(start: dt.datetime, end: dt.datetime) -> Dict[int, Dict[str, float]]:
    providers: List[WeatherProvider] = [
        OpenMeteoProvider(),
        OpenWeatherProvider(OPENWEATHER_API_KEY),
        WindyProvider(WINDY_API_KEY),
        YandexWeatherProvider(YANDEX_API_KEY),
    ]
    results: Dict[int, Dict[str, List[float]]] = {}
    tasks = [p.fetch_hours(LAT, LON, start, end) for p in providers]
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    for p, batch in zip(providers, batches):
        if isinstance(batch, Exception): continue
        for point in batch:
            ts = int(point["ts"])
            cell = results.setdefault(ts, {"cloud": [], "precip_prob": []})
            try:
                if not math.isnan(point.get("cloud", float("nan"))):
                    cell["cloud"].append(point.get("cloud", 100.0))
            except Exception:
                pass
            cell["precip_prob"].append(point.get("precip_prob", 0.0))
    averaged: Dict[int, Dict[str, float]] = {}
    for ts, vals in results.items():
        if not vals["cloud"] and not vals["precip_prob"]: continue
        avg_cloud = sum(vals["cloud"])/len(vals["cloud"]) if vals["cloud"] else 100.0
        avg_precip = sum(vals["precip_prob"])/len(vals["precip_prob"]) if vals["precip_prob"] else 0.0
        averaged[ts] = {"cloud": avg_cloud, "precip_prob": avg_precip}
    return dict(sorted(averaged.items()))

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

def summarize_windows(averaged: Dict[int, Dict[str, float]], tz: ZoneInfo) -> List[Tuple[int,int]]:
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

def fmt_report(date_local: dt.date, dusk: dt.datetime, dawn: dt.datetime, averaged: Dict[int, Dict[str, float]], windows: List[Tuple[int,int]], tz: ZoneInfo) -> str:
    if not averaged:
        return "Нет данных с погодных сервисов на сегодня."
    if not windows:
        return f"Сегодня ({date_local.strftime('%d.%m.%Y')}) ночью над Ногинским районом облачно или осадки. Съёмку не планируем.\nОкно ночи: {dusk.strftime('%H:%M')}–{dawn.strftime('%H:%M')} МСК."
    parts = [f"Прогноз на ночь {date_local.strftime('%d.%m.%Y')} (Ногинский район):",
             f"Окно ночи: {dusk.strftime('%H:%M')}–{dawn.strftime('%H:%M')} МСК.",
             "Окна для съёмки (средняя облачность/осадки по источникам):"]
    for a,b in windows:
        hours = [averaged[ts]["cloud"] for ts in sorted(averaged) if a <= ts < b]
        avgc = sum(hours)/len(hours) if hours else 0.0
        parts.append(f"• {dt.datetime.fromtimestamp(a, tz).strftime('%H:%M')}–{dt.datetime.fromtimestamp(b, tz).strftime('%H:%M')}  (ср. облачн.: {avgc:.0f}%)")
    return "\n".join(parts)

async def build_message(date_local: dt.date, tz: ZoneInfo) -> str:
    dusk, dawn = night_window(date_local, tz)
    start_utc = dusk.astimezone(dt.timezone.utc); end_utc = dawn.astimezone(dt.timezone.utc)
    averaged = await fetch_all_providers(start_utc, end_utc)
    windows = summarize_windows(averaged, tz)
    return fmt_report(date_local, dusk, dawn, averaged, windows, tz)

# Telegram handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat(chat_id)
    await update.message.reply_text(
        "Привет! Я буду присылать дневной отчёт о видимости на предстоящую ночь по Ногинскому району.\n"
        "Команды:\n"
        "/now — получить прогноз сейчас\n"
        "/tomorrow — прогноз на завтрашнюю ночь\n"
        "/setthresholds <облачн%> <осадки%> — поменять пороги (например, /setthresholds 40 20)\n"
    )

async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()
    msg = await build_message(today, tz)
    await update.message.reply_text(msg)

async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()
    nxt = today + dt.timedelta(days=1)
    msg = await build_message(nxt, tz)
    await update.message.reply_text(msg)

async def setthresholds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CLOUD_THRESHOLD, PRECIP_THRESHOLD
    try:
        c = float(context.args[0]); p = float(context.args[1])
        CLOUD_THRESHOLD = c; PRECIP_THRESHOLD = p
        await update.message.reply_text(f"Ок! Пороги обновлены: облачность ≤ {c}%, осадки ≤ {p}%.")
    except Exception:
        await update.message.reply_text("Используйте формат: /setthresholds 40 20")

async def daily_job(app: Application):
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()
    msg = await build_message(today, tz)
    for chat_id in load_chats():
        try: await app.bot.send_message(chat_id=chat_id, text=msg)
        except Exception: pass

def setup_scheduler(app: Application):
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    trigger = CronTrigger(hour=DAILY_NOTIFY_HOUR, minute=DAILY_NOTIFY_MINUTE)
    scheduler.add_job(lambda: daily_job(app), trigger)
    scheduler.start()

def main():
    if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN is not set")
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("now", now))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("setthresholds", setthresholds))
    setup_scheduler(application)
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
