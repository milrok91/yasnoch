# -*- coding: utf-8 -*-
import os, json, math, asyncio, threading
import datetime as dt
from typing import List, Dict, Tuple

# --- tiny HTTP server for Render Web Service ---
from http.server import BaseHTTPRequestHandler, HTTPServer

def _start_keepalive_server():
    port = int(os.getenv("PORT", "8000"))
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args): return
    srv = HTTPServer(("0.0.0.0", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

_start_keepalive_server()

from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from astral import LocationInfo
from astral.sun import sun

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from providers import OpenMeteoProvider, OpenWeatherProvider, WindyProvider, YandexWeatherProvider, WeatherProvider

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENWEATHER_API_KEY = (os.getenv("OPENWEATHER_API_KEY", "") or "").strip() or None
WINDY_API_KEY = (os.getenv("WINDY_API_KEY", "") or "").strip() or None
YANDEX_API_KEY = (os.getenv("YANDEX_API_KEY", "") or "").strip() or None
LAT = float(os.getenv("LAT", "55.85"))
LON = float(os.getenv("LON", "38.45"))
CLOUD_THRESHOLD = float(os.getenv("CLOUD_THRESHOLD", "35"))
PRECIP_THRESHOLD = float(os.getenv("PRECIP_THRESHOLD", "20"))
MIN_WINDOW_HOURS = float(os.getenv("MIN_WINDOW_HOURS", "1.0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
DAILY_NOTIFY_HOUR = int(os.getenv("DAILY_NOTIFY_HOUR", "15"))
DAILY_NOTIFY_MINUTE = int(os.getenv("DAILY_NOTIFY_MINUTE", "0"))
CHAT_DB = os.getenv("CHAT_DB", "chat_ids.json")
SHOW_SOURCES = os.getenv("SHOW_SOURCES", "0") == "1"  # add footer with providers used

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

def night_window(date_local: dt.date, tz: ZoneInfo) -> Tuple[dt.datetime, dt.datetime]:
    loc = LocationInfo(latitude=LAT, longitude=LON)
    s1 = sun(loc.observer, date=date_local, tzinfo=tz)
    s2 = sun(loc.observer, date=(date_local + dt.timedelta(days=1)), tzinfo=tz)
    sunset = s1["sunset"]; sunrise = s2["sunrise"]
    dusk_astro = sunset + dt.timedelta(minutes=90)
    dawn_astro = sunrise - dt.timedelta(minutes=90)
    if dawn_astro <= dusk_astro: dusk_astro = sunset; dawn_astro = sunrise
    return dusk_astro, dawn_astro

def build_active_providers():
    providers = []
    names = []
    # Always include Open-Meteo (без ключа)
    providers.append(OpenMeteoProvider()); names.append("Open-Meteo")
    # Conditionally include OpenWeather
    if OPENWEATHER_API_KEY:
        providers.append(OpenWeatherProvider(OPENWEATHER_API_KEY)); names.append("OpenWeather")
    # Conditionally include Windy
    if WINDY_API_KEY:
        providers.append(WindyProvider(WINDY_API_KEY)); names.append("Windy")
    # Conditionally include Yandex
    if YANDEX_API_KEY:
        providers.append(YandexWeatherProvider(YANDEX_API_KEY)); names.append("Yandex")
    return providers, names

async def fetch_all_providers_with_stats(start: dt.datetime, end: dt.datetime):
    providers, names = build_active_providers()
    results: Dict[int, Dict[str, List[float]]] = {}
    contrib = {n: 0 for n in names}
    errors = {n: "" for n in names}

    tasks = [p.fetch_hours(LAT, LON, start, end) for p in providers]
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    for name, batch in zip(names, batches):
        if isinstance(batch, Exception):
            errors[name] = f"{type(batch).__name__}: {batch}"
            continue
        try:
            for point in batch:
                ts = int(point["ts"])
                cell = results.setdefault(ts, {"cloud": [], "precip_prob": []})
                cloud = point.get("cloud", float("nan"))
                if not math.isnan(cloud):
                    cell["cloud"].append(cloud); contrib[name] += 1
                if "precip_prob" in point:
                    cell["precip_prob"].append(point["precip_prob"])
        except Exception as e:
            errors[name] = f"ParseError: {e}"

    averaged: Dict[int, Dict[str, float]] = {}
    for ts, vals in results.items():
        if not vals["cloud"] and not vals["precip_prob"]: continue
        avg_cloud = sum(vals["cloud"])/len(vals["cloud"]) if vals["cloud"] else 100.0
        avg_precip = sum(vals["precip_prob"])/len(vals["precip_prob"]) if vals["precip_prob"] else 0.0
        averaged[ts] = {"cloud": avg_cloud, "precip_prob": avg_precip}
    averaged = dict(sorted(averaged.items()))
    return averaged, contrib, errors, names

def summarize_windows(averaged: Dict[int, Dict[str, float]], tz: ZoneInfo) -> List[Tuple[int,int]]:
    allowed_ts = [ts for ts, v in averaged.items() if v["cloud"] <= CLOUD_THRESHOLD and v["precip_prob"] <= PRECIP_THRESHOLD]
    if not allowed_ts: return []
    allowed_ts.sort()
    windows: List[Tuple[int,int]] = []; start = allowed_ts[0]; prev = start
    for ts in allowed_ts[1:]:
        if ts - prev == 3600: prev = ts
        else: windows.append((start, prev+3600)); start = ts; prev = ts
    windows.append((start, prev+3600))
    out = []
    for a,b in windows:
        if (b-a)/3600.0 >= MIN_WINDOW_HOURS: out.append((a,b))
    return out

def fmt_report(date_local: dt.date, dusk: dt.datetime, dawn: dt.datetime,
               averaged: Dict[int, Dict[str, float]], windows: List[Tuple[int,int]], tz: ZoneInfo,
               contrib: Dict[str, int], names: List[str]) -> str:
    if not averaged:
        base = "Нет данных с погодных сервисов на выбранный интервал."
    elif not windows:
        base = (f"Сегодня ({date_local.strftime('%d.%m.%Y')}) ночью над Ногинским районом облачно или осадки. "
                f"Съёмку не планируем.\nОкно ночи: {dusk.strftime('%H:%M')}–{dawn.strftime('%H:%M')} МСК.")
    else:
        parts = [
            f"Прогноз на ночь {date_local.strftime('%d.%m.%Y')} (Ногинский район):",
            f"Окно ночи: {dusk.strftime('%H:%M')}–{dawn.strftime('%H:%M')} МСК.",
            "Окна для съёмки (средняя облачность/осадки по источникам):"
        ]
        for a,b in windows:
            hours = [averaged[ts]["cloud"] for ts in sorted(averaged) if a <= ts < b]
            avgc = sum(hours)/len(hours) if hours else 0.0
            parts.append(f"• {dt.datetime.fromtimestamp(a, tz).strftime('%H:%M')}–{dt.datetime.fromtimestamp(b, tz).strftime('%H:%M')}  (ср. облачн.: {avgc:.0f}%)")
        base = "\n".join(parts)

    if SHOW_SOURCES:
        used = [f"{k}:{v}" for k,v in contrib.items() if v > 0]
        if not used:
            base += "\n\nИсточник(и): нет данных"
        else:
            base += "\n\nИсточник(и): " + ", ".join(used)
    return base

async def build_message(date_local: dt.date, tz: ZoneInfo) -> str:
    dusk, dawn = night_window(date_local, tz)
    start_utc = dusk.astimezone(dt.timezone.utc); end_utc = dawn.astimezone(dt.timezone.utc)
    averaged, contrib, errors, names = await fetch_all_providers_with_stats(start_utc, end_utc)
    windows = summarize_windows(averaged, tz)
    return fmt_report(date_local, dusk, dawn, averaged, windows, tz, contrib, names)

# --- Telegram handlers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Привет! Команды:\n"
        "/now — прогноз на ближайшую ночь\n"
        "/tomorrow — прогноз на завтрашнюю ночь\n"
        "/sources — какие источники дали данные\n"
        "/diag — диагностика провайдеров\n"
        "/setthresholds <облачн%> <осадки%>"
    )

async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()
    await update.message.reply_text(await build_message(today, tz))

async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    nxt = (dt.datetime.now(tz).date() + dt.timedelta(days=1))
    await update.message.reply_text(await build_message(nxt, tz))

async def sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    dusk, dawn = night_window(dt.datetime.now(tz).date(), tz)
    _, contrib, errors, names = await fetch_all_providers_with_stats(dusk.astimezone(dt.timezone.utc), dawn.astimezone(dt.timezone.utc))
    lines = []
    for n in names:
        lines.append(f"{n}: {contrib.get(n,0)} точек" + (f" (ошибка: {errors[n]})" if errors.get(n) else ""))
    await update.message.reply_text("Источники за ночь:\n" + "\n".join(lines))

async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    dusk, dawn = night_window(dt.datetime.now(tz).date(), tz)
    _, contrib, errors, names = await fetch_all_providers_with_stats(dusk.astimezone(dt.timezone.utc), dawn.astimezone(dt.timezone.utc))
    parts = []
    for n in names:
        err = errors.get(n, "")
        cnt = contrib.get(n, 0)
        parts.append(f"• {n}: {cnt} точек; " + ("ошибок нет" if not err else f"ошибка: {err}"))
    await update.message.reply_text("Диагностика провайдеров:\n" + "\n".join(parts))

async def setthresholds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CLOUD_THRESHOLD, PRECIP_THRESHOLD
    try:
        c = float(context.args[0]); p = float(context.args[1])
        CLOUD_THRESHOLD = c; PRECIP_THRESHOLD = p
        await update.message.reply_text(f"Ок! Пороги: облачность ≤ {c}%, осадки ≤ {p}%.")
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
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("now", now))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("sources", sources))
    application.add_handler(CommandHandler("diag", diag))
    application.add_handler(CommandHandler("setthresholds", setthresholds))
    setup_scheduler(application)
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
