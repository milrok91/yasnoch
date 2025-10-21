# -*- coding: utf-8 -*-
import datetime as dt
from typing import Dict, List, Optional, Tuple
import httpx
import math

# Types
HourPoint = Dict[str, float]  # {'ts': epoch_sec, 'cloud': 0..100, 'precip_prob': 0..100}

class WeatherProvider:
    name: str
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        raise NotImplementedError

def _round_hour(t: dt.datetime) -> dt.datetime:
    # Round down to the hour
    return t.replace(minute=0, second=0, microsecond=0)

class OpenMeteoProvider(WeatherProvider):
    name = "Open-Meteo"
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        # Open-Meteo free, no API key. Cloud cover %, precip prob %
        # timezone use UTC to simplify, then we'll interpret timestamps as UTC
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "cloudcover,precipitation_probability",
            "timeformat": "unixtime",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
            j = r.json()
        if "hourly" not in j:
            return []
        times = j["hourly"]["time"]
        clouds = j["hourly"].get("cloudcover", [None]*len(times))
        pr = j["hourly"].get("precipitation_probability", [None]*len(times))
        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp())
        end_ts = int(_round_hour(end).timestamp())
        for t, c, p in zip(times, clouds, pr):
            ts = int(t)
            if start_ts <= ts <= end_ts:
                out.append({"ts": ts, "cloud": float(c if c is not None else math.nan), "precip_prob": float(p if p is not None else 0.0)})
        return out

class OpenWeatherProvider(WeatherProvider):
    name = "OpenWeather"
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key:
            return []
        params = {
            "lat": lat, "lon": lon,
            "appid": self.api_key,
            "units": "metric",
            "exclude": "minutely,daily,alerts,current",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.openweathermap.org/data/2.5/onecall", params=params)
            r.raise_for_status()
            j = r.json()
        hours = j.get("hourly", [])
        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp())
        end_ts = int(_round_hour(end).timestamp())
        for h in hours:
            ts = int(h["dt"])
            if start_ts <= ts <= end_ts:
                # OpenWeather doesn't have cloud probability, only cloudiness % and precip probability pop (0..1) maybe
                cloud = float(h.get("clouds", 0.0))
                pop = float(h.get("pop", 0.0)) * 100.0
                out.append({"ts": ts, "cloud": cloud, "precip_prob": pop})
        return out


class WindyProvider(WeatherProvider):
    """
    Windy point-forecast API (requires API key). We query for 'clouds' (%) and 'precip' probability if present.
    Docs: https://api.windy.com/
    """
    name = "Windy"
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key:
            return []
        params = {
            "lat": lat,
            "lon": lon,
            "model": "gfs",   # or "iconEu"/"ecmwf" depending on your plan
            "parameters": ["clouds", "precip"],
        }
        headers = {"Accept": "application/json", "X-Windy-Key": self.api_key}
        url = "https://api.windy.com/api/point-forecast/v2"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=params)
            r.raise_for_status()
            j = r.json()
        out: List[HourPoint] = []
        # Windy returns time array and parameter arrays; we map to hourly timeline if available
        ts_list = j.get("ts", []) or j.get("time", [])
        clouds = j.get("clouds") or {}
        precip = j.get("precip") or {}
        # Some responses may nest under "forecast"
        if not ts_list and "forecast" in j:
            ts_list = j["forecast"].get("ts", [])
            clouds = j["forecast"].get("clouds", {})
            precip = j["forecast"].get("precip", {})
        # flatten: values may be under "surface" key or default list
        def take_vals(obj):
            if isinstance(obj, dict):
                for k,v in obj.items():
                    if isinstance(v, list):
                        return v
                return []
            if isinstance(obj, list):
                return obj
            return []
        c_vals = take_vals(clouds)
        p_vals = take_vals(precip)
        start_ts = int(_round_hour(start).timestamp())
        end_ts = int(_round_hour(end).timestamp())
        for i, ts in enumerate(ts_list):
            ts = int(ts)
            if start_ts <= ts <= end_ts:
                c = float(c_vals[i]) if i < len(c_vals) else float("nan")
                pr = float(p_vals[i])*100.0 if i < len(p_vals) else 0.0
                # Some Windy precip arrays already in mm or prob; clamp to 0..100 if looks like prob
                if pr > 100.0:
                    pr = 100.0
                out.append({"ts": ts, "cloud": c, "precip_prob": pr})
        return out


class YandexWeatherProvider(WeatherProvider):
    """
    Yandex Weather API v2 (requires API key). We read hourly cloudness and precipitation probability (if available).
    Docs: https://yandex.com/dev/weather/
    """
    name = "YandexWeather"
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key:
            return []
        params = {
            "lat": lat,
            "lon": lon,
            "lang": "ru_RU",
            "hours": "true",
            "limit": 2,  # today + tomorrow
        }
        headers = {"X-Yandex-API-Key": self.api_key}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.weather.yandex.ru/v2/forecast", params=params, headers=headers)
            r.raise_for_status()
            j = r.json()
        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp())
        end_ts = int(_round_hour(end).timestamp())
        forecasts = j.get("forecasts", [])
        for f in forecasts:
            hours = f.get("hours", [])
            for h in hours:
                # hour as "HH" local; build timestamp from "date" + "hour"
                date_str = f.get("date")
                hh = int(h.get("hour", 0))
                # Yandex gives local time based on location; interpret as naive local Moscow time and convert to UTC-like epoch
                # Simpler: parse date and hour as naive and treat as UTC+3 (Moscow, no DST)
                dt_local = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hh)
                # Moscow fixed offset +3 for our purpose
                ts = int((dt_local - dt.timedelta(hours=3)).timestamp())
                if ts < start_ts or ts > end_ts:
                    continue
                # cloudness 0..1 (0 ясно, 1 пасмурно), convert to %
                cloud = float(h.get("cloudness", 1.0)) * 100.0
                # precipitation probability might be "prec_prob" 0..100 or "prec_prob" 0..1 in some outputs
                pr = h.get("prec_prob")
                if pr is None:
                    pr = h.get("precipitation_probability")
                if pr is None:
                    pr = 0.0
                pr = float(pr)
                if pr <= 1.0:
                    pr *= 100.0
                out.append({"ts": ts, "cloud": cloud, "precip_prob": pr})
        return out
