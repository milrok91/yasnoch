# -*- coding: utf-8 -*-
"""
Providers for astro weather bot.

Includes:
- OpenMeteoProvider       (без ключа)
- OpenWeatherProvider     (использует /data/2.5/forecast 5-day/3h и разворачивает в почасовые)
- WindyProvider           (Point Forecast v2, ключ в body: {"key": ...}, параметры l/m/h clouds + precip)
- VisualCrossingProvider  (опционально, если есть ключ)
"""

import datetime as dt
from typing import Dict, List, Optional
import httpx
import math

HourPoint = Dict[str, float]  # {'ts': epoch_sec, 'cloud': 0..100, 'precip_prob': 0..100}

class WeatherProvider:
    name: str
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        raise NotImplementedError

def _round_hour(t: dt.datetime) -> dt.datetime:
    return t.replace(minute=0, second=0, microsecond=0)

# ---------------- Open-Meteo ----------------
class OpenMeteoProvider(WeatherProvider):
    name = "Open-Meteo"
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "cloudcover,precipitation_probability",
            "timeformat": "unixtime"
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
            j = r.json()
        if "hourly" not in j:
            return []
        times = j["hourly"].get("time", [])
        clouds = j["hourly"].get("cloudcover", [])
        pr = j["hourly"].get("precipitation_probability", [])
        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp()); end_ts = int(_round_hour(end).timestamp())
        for idx, t in enumerate(times):
            try:
                ts = int(t)
            except Exception:
                continue
            if start_ts <= ts <= end_ts:
                c = float(clouds[idx]) if idx < len(clouds) and clouds[idx] is not None else float("nan")
                p = float(pr[idx]) if idx < len(pr) and pr[idx] is not None else 0.0
                out.append({"ts": ts, "cloud": c, "precip_prob": p})
        return out

# ---------------- OpenWeather (5 day / 3h) ----------------
class OpenWeatherProvider(WeatherProvider):
    """
    Используем бесплатный endpoint /data/2.5/forecast (5 day / 3-hour).
    Каждый 3‑часовой блок разворачиваем в 3 «почасовые» точки.
    """
    name = "OpenWeather"
    def __init__(self, api_key: Optional[str]): self.api_key = api_key

    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key:
            return []
        params = {"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.openweathermap.org/data/2.5/forecast", params=params)
            r.raise_for_status()
            j = r.json()
        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp())
        end_ts = int(_round_hour(end).timestamp())
        for item in j.get("list", []):
            ts3 = int(item.get("dt", 0))
            cloud = float(item.get("clouds", {}).get("all", 0.0))
            pop = float(item.get("pop", 0.0)) * 100.0  # вероятность осадков 0..100
            # Разворачиваем на 3 часа вперёд (по часу)
            for k in range(3):
                ts = ts3 + k*3600
                if start_ts <= ts <= end_ts:
                    out.append({"ts": ts, "cloud": cloud, "precip_prob": pop})
        return out

# ---------------- Windy ----------------
class WindyProvider(WeatherProvider):
    """
    Windy Point Forecast v2: ключ должен быть в JSON body (поле "key").
    Параметры облачности: lclouds/mclouds/hclouds, плюс past3hprecip.
    Возвращает ts в миллисекундах → переводим в секунды.
    """
    name = "Windy"
    def __init__(self, api_key: Optional[str]): self.api_key = api_key

    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key:
            return []
        payload = {
            "lat": lat,
            "lon": lon,
            "model": "gfs",  # можно попробовать "iconEu" при желании
            "parameters": ["lclouds", "mclouds", "hclouds", "precip"],
            "levels": ["surface"],
            "key": self.api_key
        }
        url = "https://api.windy.com/api/point-forecast/v2"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            j = r.json()

        ts_list = j.get("ts", []) or []
        # массивы значений
        l = j.get("lclouds-surface", []) or []
        m = j.get("mclouds-surface", []) or []
        h = j.get("hclouds-surface", []) or []
        p3 = j.get("past3hprecip-surface", []) or []  # накопленные осадки за 3 часа

        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp()); end_ts = int(_round_hour(end).timestamp())
        for i, tms in enumerate(ts_list):
            try:
                ts = int(int(tms) / 1000)  # миллисекунды → секунды
            except Exception:
                continue
            if ts < start_ts or ts > end_ts:
                continue
            vals = []
            if i < len(l) and l[i] is not None: vals.append(float(l[i]))
            if i < len(m) and m[i] is not None: vals.append(float(m[i]))
            if i < len(h) and h[i] is not None: vals.append(float(h[i]))
            cloud = sum(vals)/len(vals) if vals else float("nan")
            # простая эвристика вероятности осадков: наличие past3hprecip → 60%
            precip_prob = 60.0 if (i < len(p3) and (p3[i] or 0) > 0) else 0.0
            out.append({"ts": ts, "cloud": cloud, "precip_prob": precip_prob})
        return out

# ---------------- Visual Crossing (опционально) ----------------
class VisualCrossingProvider(WeatherProvider):
    name = "VisualCrossing"
    def __init__(self, api_key: Optional[str]): self.api_key = api_key
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key:
            return []
        url = f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{lat},{lon}"
        params = {
            "unitGroup": "metric",
            "include": "hours",
            "key": self.api_key,
            "contentType": "json"
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            j = r.json()
        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp()); end_ts = int(_round_hour(end).timestamp())
        for day in j.get("days", []):
            for h in day.get("hours", []):
                tstr = h.get("datetime")  # "2025-10-22T23:00:00+03:00"
                if not tstr: continue
                try:
                    ts = int(dt.datetime.fromisoformat(tstr).timestamp())
                except Exception:
                    continue
                if start_ts <= ts <= end_ts:
                    cloud = float(h.get("cloudcover", 100.0))
                    pr = float(h.get("precipprob", 0.0))
                    out.append({"ts": ts, "cloud": cloud, "precip_prob": pr})
        return out
