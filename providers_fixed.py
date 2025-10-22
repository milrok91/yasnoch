# -*- coding: utf-8 -*-
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

class OpenMeteoProvider(WeatherProvider):
    name = "Open-Meteo"
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        params = {"latitude": lat, "longitude": lon, "hourly": "cloudcover,precipitation_probability", "timeformat": "unixtime"}
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
        start_ts = int(_round_hour(start).timestamp()); end_ts = int(_round_hour(end).timestamp())
        for t, c, p in zip(times, clouds, pr):
            ts = int(t)
            if start_ts <= ts <= end_ts:
                out.append({"ts": ts, "cloud": float(c if c is not None else math.nan), "precip_prob": float(p if p is not None else 0.0)})
        return out

class OpenWeatherProvider(WeatherProvider):
    """Fallbacks to /data/2.5/forecast (5 day / 3-hour) to avoid One Call 3.0 auth issues.
    We expand each 3-hour slot into three hourly points for merging.
    """
    name = "OpenWeather"
    def __init__(self, api_key: Optional[str]): self.api_key = api_key
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key: return []
        params = {"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.openweathermap.org/data/2.5/forecast", params=params)
            r.raise_for_status(); j = r.json()
        out: List[HourPoint] = []
        start_ts = int(_round_hour(start).timestamp()); end_ts = int(_round_hour(end).timestamp())
        for item in j.get("list", []):
            ts3 = int(item.get("dt", 0))
            # Extract cloudiness and precipitation probability
            cloud = float(item.get("clouds", {}).get("all", 0.0))
            pop = float(item.get("pop", 0.0)) * 100.0
            # Expand this 3-hour block into 3 hourly points
            for k in range(3):
                ts = ts3 + k*3600
                if start_ts <= ts <= end_ts:
                    out.append({"ts": ts, "cloud": cloud, "precip_prob": pop})
        return out

class WindyProvider(WeatherProvider):
    name = "Windy"
    def __init__(self, api_key: Optional[str]): self.api_key = api_key
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key: return []
        # Payload shaped to avoid 400 errors on v2 endpoint
        payload = {
            "lat": lat,
            "lon": lon,
            "model": "gfs",
            "parameters": ["clouds", "precip"],
            "levels": ["surface"],
            "timeformat": "unix"
        }
        headers = {"Accept": "application/json", "X-Windy-Key": self.api_key}
        url = "https://api.windy.com/api/point-forecast/v2"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            j = r.json()
        out: List[HourPoint] = []
        ts_list = j.get("ts", []) or j.get("time", [])
        clouds = j.get("clouds") or {}
        precip = j.get("precip") or {}
        if not ts_list and "forecast" in j:
            ts_list = j["forecast"].get("ts", [])
            clouds = j["forecast"].get("clouds", {})
            precip = j["forecast"].get("precip", {})
        def take_vals(obj):
            if isinstance(obj, dict):
                for _,v in obj.items():
                    if isinstance(v, list): return v
                return []
            return obj if isinstance(obj, list) else []
        c_vals = take_vals(clouds); p_vals = take_vals(precip)
        start_ts = int(_round_hour(start).timestamp()); end_ts = int(_round_hour(end).timestamp())
        for i, ts in enumerate(ts_list):
            ts = int(ts)
            if start_ts <= ts <= end_ts:
                c = float(c_vals[i]) if i < len(c_vals) else float("nan")
                pr = float(p_vals[i])*100.0 if i < len(p_vals) else 0.0
                if pr > 100.0: pr = 100.0
                out.append({"ts": ts, "cloud": c, "precip_prob": pr})
        return out

class VisualCrossingProvider(WeatherProvider):
    name = "VisualCrossing"
    def __init__(self, api_key: Optional[str]): self.api_key = api_key
    async def fetch_hours(self, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> List[HourPoint]:
        if not self.api_key: return []
        # Timeline API with hourly include
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
                # VisualCrossing time like "2025-10-22T23:00:00+03:00"
                tstr = h.get("datetime")
                if not tstr: continue
                # parse naive: split by '+' and take left
                try:
                    base = tstr.split("+")[0]
                    ts_local = dt.datetime.fromisoformat(base)
                    # Assume time is local zone offset present; compute epoch via fromisoformat with tz if available
                    if "T" in tstr and ("+" in tstr or "Z" in tstr):
                        ts = int(dt.datetime.fromisoformat(tstr).timestamp())
                    else:
                        ts = int(ts_local.timestamp())
                except Exception:
                    continue
                if start_ts <= ts <= end_ts:
                    cloud = float(h.get("cloudcover", 100.0))  # 0..100
                    pr = float(h.get("precipprob", 0.0))       # 0..100
                    out.append({"ts": ts, "cloud": cloud, "precip_prob": pr})
        return out
