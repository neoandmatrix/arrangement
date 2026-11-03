"""Finding vendors near the venue, beyond our own directory.

Sources:
- OpenStreetMap (Nominatim search in a box around the pin): free, no key. Its usage policy
  allows at most one request a second with an identifying User-Agent, so searches run one at
  a time and are cached.
- Web search (Brave Search API), only when BRAVE_SEARCH_API_KEY is set.

The contest allows synthetic data only, so discovered businesses are shown to the family as
"found nearby" and are never called, and no phone numbers are collected. The vendors Bandobast
actually calls come from the synthetic directory (data/vendors.json).
"""

import asyncio
import os
import time
from typing import Any

import httpx
from loguru import logger

import catalog
import rules

USER_AGENT = "Bandobast/0.1 (student project for the Great Indian AI Internship Challenge)"
SEARCH_RADIUS_DEG = 0.05  # about 5 km either way at Bengaluru's latitude
MAX_PER_SOURCE = 5

# What to search for, per vendor type.
OSM_QUERIES = {
    "priest": ["temple"],
    "caterer": ["caterers"],
    "decorator": ["florist", "decorators"],
    "cake": ["bakery", "cake shop"],
    "photographer": ["photo studio"],
    "tent_sound": ["tent house", "shamiana"],
}
WEB_QUERIES = {
    "priest": "pandit for {function} puja in {area} Bengaluru",
    "caterer": "caterers for {function} in {area} Bengaluru",
    "decorator": "{function} decorators in {area} Bengaluru",
    "cake": "cake shop in {area} Bengaluru",
    "photographer": "event photographer in {area} Bengaluru",
    "tent_sound": "tent house and sound system rental in {area} Bengaluru",
}

_nominatim_lock = asyncio.Lock()
_last_nominatim = 0.0
_cache: dict[tuple, list[dict[str, Any]]] = {}


async def discover(category: str, brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Businesses near the venue for one vendor type: [{name, source, lat, lon, distance_km, url}]."""
    venue = rules.venue_location(brief)
    if not venue:
        return []
    results = await _openstreetmap(category, venue)
    if os.environ.get("BRAVE_SEARCH_API_KEY"):
        results += await _web_search(category, brief)
    return results


async def _openstreetmap(category: str, venue: tuple[float, float]) -> list[dict[str, Any]]:
    lat, lon = venue
    key = ("osm", category, round(lat, 3), round(lon, 3))
    if key in _cache:
        return _cache[key]
    found: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as client:
        for query in OSM_QUERIES[category]:
            for place in await _nominatim(client, query, lat, lon):
                name = place.get("name") or ""
                # Unnamed map points ("Temple", "Bakery") tell the family nothing.
                if not name or name.lower() in (query.lower(), place.get("type", "").replace("_", " ")):
                    continue
                p_lat, p_lon = float(place["lat"]), float(place["lon"])
                found.setdefault(
                    name.lower(),
                    {
                        "name": name,
                        "source": "openstreetmap",
                        "lat": p_lat,
                        "lon": p_lon,
                        "distance_km": round(rules.distance_km(lat, lon, p_lat, p_lon), 1),
                        "address": ", ".join(place.get("display_name", "").split(", ")[1:4]),
                        "url": f"https://www.openstreetmap.org/{place['osm_type']}/{place['osm_id']}",
                    },
                )
    results = sorted(found.values(), key=lambda p: p["distance_km"])[:MAX_PER_SOURCE]
    _cache[key] = results
    return results


async def _nominatim(client: httpx.AsyncClient, query: str, lat: float, lon: float) -> list[dict[str, Any]]:
    global _last_nominatim
    r = SEARCH_RADIUS_DEG
    async with _nominatim_lock:
        wait = 1.1 - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "viewbox": f"{lon - r},{lat + r},{lon + r},{lat - r}",
                    "bounded": 1,
                    "format": "jsonv2",
                    "limit": 10,
                },
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"OpenStreetMap search '{query}' failed: {e}")
            return []
        finally:
            _last_nominatim = time.monotonic()


async def _web_search(category: str, brief: dict[str, Any]) -> list[dict[str, Any]]:
    query = WEB_QUERIES[category].format(
        function=catalog.function_label(brief).lower(), area=brief.get("area") or ""
    )
    key = ("web", query)
    if key in _cache:
        return _cache[key]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": MAX_PER_SOURCE, "country": "IN"},
                headers={"X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"], "Accept": "application/json"},
            )
            response.raise_for_status()
            hits = response.json().get("web", {}).get("results", [])
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"Web search '{query}' failed: {e}")
        return []
    results = [
        {"name": h.get("title", ""), "source": "web", "url": h.get("url"), "snippet": h.get("description", "")[:200]}
        for h in hits[:MAX_PER_SOURCE]
    ]
    _cache[key] = results
    return results
