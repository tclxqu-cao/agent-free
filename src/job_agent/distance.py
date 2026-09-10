"""距离计算：haversine + geocode 链（高德 → Nominatim → 内置区表）。

坐标均为 [lat, lng]。内置区表坐标为近似值，用于离线兜底估计。
"""

from __future__ import annotations

import json
from importlib import resources
from math import asin, cos, radians, sin, sqrt

import httpx

from .db import DB
from .models import Job


def _load_districts() -> dict:
    with resources.open_text("job_agent.data", "districts.json", encoding="utf-8") as f:
        return json.load(f)


DISTRICTS = _load_districts()


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return round(6371.0 * 2 * asin(sqrt(a)), 1)


class Geocoder:
    """geocode 链：amap（配 key 时）→ Nominatim → 内置区/市坐标。结果缓存进 DB。"""

    def __init__(self, db: DB, amap_key: str = "", home_city: str | None = None,
                 offline: bool = False):
        self.db = db
        self.amap_key = amap_key or ""
        self.home_city = home_city
        self.offline = offline  # True: 只用内置区表（demo/测试用，不发网络请求）

    def geocode(self, address: str, city: str | None = None) -> tuple[float, float] | None:
        if not address:
            return None
        key = f"{city or ''}|{address}"
        cached = self.db.geocode_get(key)
        if cached:
            return cached
        providers = ((self._local,) if self.offline
                     else (self._amap, self._nominatim, self._local))
        for provider in providers:
            try:
                result = provider(address, city)
            except Exception:
                result = None
            if result:
                self.db.geocode_put(key, result[0], result[1])
                return result
        return None

    def _amap(self, address: str, city: str | None) -> tuple[float, float] | None:
        if not self.amap_key:
            return None
        r = httpx.get(
            "https://restapi.amap.com/v3/geocode/geo",
            params={"address": address, "city": city or "", "key": self.amap_key},
            timeout=10,
        )
        geos = (r.json() or {}).get("geocodes") or []
        if not geos:
            return None
        lng, lat = (geos[0].get("location") or "").split(",")
        return float(lat), float(lng)

    def _nominatim(self, address: str, city: str | None) -> tuple[float, float] | None:
        q = f"{city or ''}{address}".strip()
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1},
            headers={"User-Agent": "job-agent/0.1 (personal job search)"},
            timeout=10,
        )
        items = r.json() or []
        if not items:
            return None
        return float(items[0]["lat"]), float(items[0]["lon"])

    def _local(self, address: str, city: str | None) -> tuple[float, float] | None:
        """离线兜底：区名 → 区中心；否则市名 → 市中心。"""
        for cname, districts in DISTRICTS.items():
            if city and cname not in city and city not in cname:
                continue
            for dname, coords in districts.items():
                if dname != "__city__" and dname in address:
                    return tuple(coords)  # type: ignore[return-value]
            if cname in address or (city and cname in city):
                return tuple(districts["__city__"])  # type: ignore[return-value]
        if city:
            for cname, districts in DISTRICTS.items():
                if cname in city:
                    return tuple(districts["__city__"])  # type: ignore[return-value]
        return None


def compute_distance(job: Job, geocoder: Geocoder, home: dict) -> float | None:
    """计算岗位距家距离（km），写入 job.raw['distance_km'] 并返回。

    优先精确地址 → 区中心（粗略）→ 市中心（最粗略）→ None。
    """
    home_lat, home_lng = home.get("lat"), home.get("lng")
    if home_lat is None or home_lng is None:
        return None
    if "distance_km" in job.raw and job.raw["distance_km"] is not None:
        return job.raw["distance_km"]

    coord = geocoder.geocode(job.address or "", job.city) if job.address else None
    if coord is None and job.district:
        coord = geocoder._local(job.district, job.city)  # 区中心粗略估计
    if coord is None and job.city:
        coord = geocoder._local(job.city, job.city)
    if coord is None:
        return None
    dist = haversine_km(home_lat, home_lng, coord[0], coord[1])
    job.raw["distance_km"] = dist
    job.raw["distance_basis"] = "address" if job.address else (
        f"district:{job.district}" if job.district else f"city:{job.city}")
    return dist
