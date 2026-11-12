"""
toonplay/scraper.py — High-performance client, stream resolver, and proxy engine for ToonPlay (Anime & Cartoons).
Completely decoupled from TamilMV / Isaimini scapers.
Hides upstream URLs and third-party gateways from the frontend.
"""
import base64
import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from toonplay import db

logger = logging.getLogger("toonplay.scraper")

DEFAULT_API_HOST = "https://animesalt.streamindia.co.in/api"
DEFAULT_EXTRACT_HOST = "https://anime.streamindia.co.in/api/extract"
DEFAULT_ABYSS_PROXY = "https://animesalt.streamindia.co.in/abyss"
BASE_API = DEFAULT_API_HOST
EXTRACT_API = DEFAULT_EXTRACT_HOST
ABYSS_PROXY = DEFAULT_ABYSS_PROXY

# In-memory fast cache (key -> (timestamp, data))
_MEM_CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes

_SESSION = requests.Session()
_adapter = HTTPAdapter(
    max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]),
    pool_connections=15,
    pool_maxsize=25,
)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://toonplay.in/",
})

_orig_session_get = _SESSION.get


def _wrapped_session_get(url, *args, **kwargs):
    try:
        r = _orig_session_get(url, *args, **kwargs)
        return r
    except (requests.ConnectionError, requests.Timeout) as exc:
        try:
            import domain_manager
            resolved = domain_manager.auto_resolve_source("toonplay")
            if resolved.get("online") and resolved.get("active_host"):
                new_base = resolved.get("active_host")
                from urllib.parse import urlparse
                p = urlparse(url)
                new_url = f"{new_base.rstrip('/')}{p.path}"
                return _orig_session_get(new_url, *args, **kwargs)
        except Exception:
            pass
        raise exc


_SESSION.get = _wrapped_session_get


def get_api_host() -> str:
    return BASE_API


def set_api_host(new_host: str) -> str:
    global BASE_API
    clean = new_host.strip().rstrip("/")
    if not clean.startswith("http"):
        clean = f"https://{clean}"
    BASE_API = clean
    return BASE_API


def ping_service() -> dict[str, Any]:
    """Test latency and reachability to ToonPlay API."""
    start = time.perf_counter()
    try:
        r = _SESSION.get(f"{BASE_API}/home", timeout=8)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {
            "status": "online" if r.status_code == 200 else "error",
            "status_code": r.status_code,
            "latency_ms": elapsed_ms,
            "api_host": BASE_API,
            "cached": r.json().get("_fromCache", False) if r.status_code == 200 else False
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {
            "status": "offline",
            "error": str(exc),
            "latency_ms": elapsed_ms,
            "api_host": BASE_API
        }


def _cache_get(key: str) -> Any | None:
    if key in _MEM_CACHE:
        ts, data = _MEM_CACHE[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def _cache_set(key: str, data: Any):
    _MEM_CACHE[key] = (time.time(), data)


def fetch_home(force_refresh: bool = False) -> dict[str, Any]:
    """Fetch home catalog sections: latest_releases, popular_series, popular_movies."""
    cache_key = "toon_home"
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached:
            return cached

    url = f"{BASE_API}/home"
    try:
        r = _SESSION.get(url, timeout=12)
        if r.status_code != 200:
            logger.warning(f"Failed to fetch ToonPlay home: HTTP {r.status_code}")
            return _load_home_from_db()
        data = r.json().get("data", {})
        
        # Save discovered items to SQLite database
        for sec in ("latest_releases", "popular_series", "popular_movies", "hero"):
            for item in data.get(sec, []):
                item["category"] = sec
                db.upsert_item(item)

        res = {
            "hero": data.get("hero", []),
            "latest_releases": data.get("latest_releases", []),
            "popular_series": data.get("popular_series", []),
            "popular_movies": data.get("popular_movies", [])
        }
        _cache_set(cache_key, res)
        return res
    except Exception as exc:
        logger.error(f"Error fetching ToonPlay home: {exc}")
        return _load_home_from_db()


def _load_home_from_db() -> dict[str, Any]:
    """Fallback to local SQLite database when offline."""
    latest = db.get_recent_items(limit=24, category="latest_releases")
    if not latest:
        latest = db.get_recent_items(limit=24)
    series = db.get_recent_items(limit=24, content_type="series")
    movies = db.get_recent_items(limit=24, content_type="movie")
    return {
        "hero": series[:5],
        "latest_releases": latest,
        "popular_series": series,
        "popular_movies": movies
    }


def search(query: str, page: int = 1) -> list[dict[str, Any]]:
    """Search ToonPlay catalog via live API and save results to local DB."""
    q = query.strip()
    if not q:
        return []

    cache_key = f"toon_search_{q.lower()}_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    url = f"{BASE_API}/search?q={quote(q)}&page={page}"
    results = []
    try:
        r = _SESSION.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            raw_items = data.get("data") or data.get("results") or []
            for it in raw_items:
                db.upsert_item(it)
                results.append(it)
    except Exception as exc:
        logger.warning(f"ToonPlay live search error: {exc}")

    if not results:
        results = db.search_items(q, limit=40)

    _cache_set(cache_key, results)
    return results


def fetch_language_catalog(language: str = "tamil", content_type: str = "all", page: int = 1) -> list[dict[str, Any]]:
    """Fetch localized language catalog (Tamil, Telugu, Hindi, English, Japanese)."""
    lang = language.lower().strip()
    cache_key = f"toon_lang_{lang}_{content_type}_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    items = []
    types_to_fetch = ["series", "movies"] if content_type == "all" else [content_type]
    for ctype in types_to_fetch:
        try:
            url = f"{BASE_API}/{lang}/{ctype}?page={page}"
            r = _SESSION.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", [])
                for it in data:
                    it["content_type"] = "movie" if ctype == "movies" else "series"
                    it["languages"] = [lang.title()]
                    db.upsert_item(it)
                    items.append(it)
        except Exception as exc:
            logger.warning(f"Error fetching ToonPlay language catalog ({lang}/{ctype}): {exc}")

    _cache_set(cache_key, items)
    return items


def fetch_category_catalog(category: str = "anime", content_type: str = "all", page: int = 1) -> list[dict[str, Any]]:
    """Fetch categorized catalog (anime, cartoon)."""
    cat = category.lower().strip()
    cache_key = f"toon_cat_{cat}_{content_type}_{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    items = []
    types_to_fetch = ["series", "movies"] if content_type == "all" else [content_type]
    for ctype in types_to_fetch:
        try:
            url = f"{BASE_API}/{cat}/{ctype}?page={page}"
            r = _SESSION.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", [])
                for it in data:
                    it["content_type"] = "movie" if ctype == "movies" else "series"
                    it["category"] = cat
                    db.upsert_item(it)
                    items.append(it)
        except Exception as exc:
            logger.warning(f"Error fetching ToonPlay category catalog ({cat}/{ctype}): {exc}")

    _cache_set(cache_key, items)
    return items


def fetch_download_links(item_id: str, season: int = 1, episode: int = 1) -> dict[str, Any]:
    """
    Fetch download mirrors (Mega, Google Drive, 480p, 720p, 1080p).
    Implements ToonPlay's wn(id, ep) query with zero external domain leaks.
    """
    clean_id = item_id.strip()
    cache_key = f"toon_dl_{clean_id}_{season}_{episode}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    candidates = [f"{clean_id}-s{season}", clean_id]
    downloads = []
    error_msg = "No download mirrors available for this episode."

    for cid in candidates:
        url = f"{BASE_API}/download?id={quote(cid)}&ep={episode}"
        try:
            r = _SESSION.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                raw_dls = data.get("downloads") or []
                if raw_dls:
                    for d in raw_dls:
                        direct = d.get("link") or ""
                        downloads.append({
                            "server": d.get("server") or "Download Server",
                            "quality": d.get("quality") or "HD",
                            "lang": d.get("lang") or "Multi-Audio",
                            "direct_link": direct,
                            "url": f"/api/toonplay/download/redirect?url={quote(direct)}"
                        })
                    break
        except Exception as exc:
            logger.warning(f"Error fetching download links ({cid}): {exc}")

    result = {
        "success": bool(downloads),
        "downloads": downloads,
        "error": None if downloads else error_msg
    }
    _cache_set(cache_key, result)
    return result


def fetch_info(item_id: str, force_refresh: bool = False) -> dict[str, Any] | None:
    """Fetch full item details, seasons, episodes, and stream servers."""
    if not item_id:
        return None

    cache_key = f"toon_info_{item_id}"
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached:
            return cached

    url = f"{BASE_API}/info?id={quote(item_id)}"
    try:
        r = _SESSION.get(url, timeout=12)
        if r.status_code == 200:
            data = r.json()
            anime = data.get("anime", {})
            if anime:
                db.upsert_item(anime)
                seasons_list = anime.get("seasonsList") or []
                for s_data in seasons_list:
                    s_num = int(s_data.get("season") or 1) if str(s_data.get("season", "")).isdigit() else 1
                    episodes = s_data.get("episodes") or []
                    if episodes:
                        db.upsert_episodes(item_id, s_num, episodes)

                _cache_set(cache_key, anime)
                return anime
    except Exception as exc:
        logger.error(f"Error fetching ToonPlay item info ({item_id}): {exc}")

    # Fallback to local DB
    local_item = db.get_item_by_id(item_id)
    if local_item:
        local_item["episodes"] = db.get_episodes(item_id)
        return local_item
    return None


def _build_episode_url(season_link: str, ep_id: str, season_num: int, ep_num: int) -> str:
    """Replicates ToonPlay's pr() helper to build the episode link for stream extraction."""
    if ep_id:
        try:
            parsed = urlparse(season_link)
            clean_ep = ep_id.lstrip("/")
            return f"{parsed.scheme}://{parsed.netloc}/{clean_ep}/"
        except Exception:
            pass
    if season_link:
        if ep_num == 1:
            return season_link
        return re.sub(rf"-{season_num}x1/?$", f"-{season_num}x{ep_num}/", season_link)
    return ""


def resolve_stream(item_id: str, season: int = 1, episode: int = 1) -> dict[str, Any]:
    """
    Resolve playable video stream / watchServers without exposing upstream domains.
    Decodes multi-audio options (Tamil, Telugu, Hindi, English, Japanese).
    """
    info = fetch_info(item_id)
    if not info:
        return {"success": False, "error": "Item not found"}

    is_movie = (info.get("type") == "movie")
    watch_servers = []

    if is_movie:
        watch_servers = info.get("watchServers") or []
    else:
        # Locate season and episode
        seasons_list = info.get("seasonsList") or []
        target_season = next(
            (s for s in seasons_list if str(s.get("season")) == str(season)),
            seasons_list[0] if seasons_list else None
        )
        if target_season:
            episodes = target_season.get("episodes") or []
            target_ep = next(
                (e for e in episodes if e.get("number") == episode),
                episodes[0] if episodes else None
            )
            if target_ep and target_season.get("link"):
                ep_link = _build_episode_url(
                    target_season["link"],
                    target_ep.get("id", ""),
                    season,
                    episode
                )
                if ep_link:
                    try:
                        extract_url = f"{EXTRACT_API}?url={quote(ep_link)}"
                        r = _SESSION.get(extract_url, timeout=10)
                        if r.status_code == 200:
                            ex_data = r.json()
                            watch_servers = ex_data.get("data", {}).get("watchServers", [])
                    except Exception as exc:
                        logger.warning(f"ToonPlay extract error: {exc}")

    # Parse and normalize servers & audio language tracks
    normalized_servers = []
    for srv in watch_servers:
        srv_name = srv.get("name") or "Server"
        raw_url = srv.get("url") or ""

        # Check for encoded multi-audio player data in plyr/player.php?data=...
        if "data=" in raw_url:
            try:
                parsed = urlparse(raw_url)
                qs = parse_qs(parsed.query)
                data_b64 = qs.get("data", [""])[0]
                if data_b64:
                    decoded_json = base64.b64decode(unquote(data_b64)).decode("utf-8", errors="ignore")
                    tracks = json.loads(decoded_json)
                    for tr in tracks:
                        lang = tr.get("language") or "Default"
                        tr_link = tr.get("link") or ""
                        normalized_servers.append({
                            "name": f"{srv_name} ({lang})",
                            "language": lang,
                            "type": "embed",
                            "url": f"/api/toonplay/embed?url={quote(tr_link)}"
                        })
                    continue
            except Exception:
                pass

        # Check for ravok multi-audio player
        if "/video/" in raw_url:
            normalized_servers.append({
                "name": f"⚡ Multi 1 (Tamil / Hindi / Telugu Dub)",
                "language": "Multi-Audio",
                "type": "embed",
                "is_ravok": True,
                "url": f"/api/toonplay/embed?url={quote(raw_url)}"
            })
            continue

        # If it's an abyss or direct player link
        track_lang = "Original"
        if "tam" in srv_name.lower():
            track_lang = "Tamil"
        elif "tel" in srv_name.lower():
            track_lang = "Telugu"
        elif "hin" in srv_name.lower():
            track_lang = "Hindi"
        elif "dub" in srv_name.lower() or "dub" in raw_url.lower():
            track_lang = "Dubbed"
        elif "sub" in srv_name.lower() or "sub" in raw_url.lower():
            track_lang = "Japanese / Sub"

        display_name = srv_name
        if "options-0" in srv_name.lower():
            display_name = f"⚡ Multi 1 ({track_lang})"
        elif "options-1" in srv_name.lower():
            display_name = f"⚡ Multi 2 ({track_lang})"
        elif "options-2" in srv_name.lower():
            display_name = f"⚡ Multi 3 ({track_lang})"

        normalized_servers.append({
            "name": display_name,
            "language": track_lang,
            "type": "embed",
            "url": f"/api/toonplay/embed?url={quote(raw_url)}"
        })

    if not normalized_servers:
        return {
            "success": False,
            "error": "No stream mirrors currently available for this title."
        }

    # Prioritize Tamil audio tracks and multi-audio servers as default #1 priority
    def _server_rank(srv: dict) -> int:
        nm = (srv.get("name") or "").lower()
        lg = (srv.get("language") or "").lower()
        if "tamil" in nm or "tamil" in lg or "tam" in nm:
            return 100
        if "multi" in nm or srv.get("is_ravok") or "multi" in lg:
            return 90
        if "telugu" in nm or "hindi" in nm:
            return 70
        if "english" in nm or "eng" in nm:
            return 60
        return 10

    normalized_servers.sort(key=_server_rank, reverse=True)

    return {
        "success": True,
        "title": info.get("title"),
        "type": info.get("type", "series"),
        "season": season if not is_movie else None,
        "episode": episode if not is_movie else None,
        "servers": normalized_servers,
        "default_stream_url": normalized_servers[0]["url"]
    }

