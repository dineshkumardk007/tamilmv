"""
cinestream/scraper.py — High-performance client, metadata fetcher, and embed resolver for CineStream.
Features multi-server embed resolution, resilient TMDB proxy fallback, and full catalog scraping.
Decoupled from TamilMV / Isaimini / ToonPlay scrapers.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cinestream import db

logger = logging.getLogger("cinestream.scraper")

DEFAULT_HOST = "https://onstream.vin"
_ACTIVE_HOST = DEFAULT_HOST

_SESSION = requests.Session()
_adapter = HTTPAdapter(
    max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]),
    pool_connections=20,
    pool_maxsize=30,
)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://onstream.vin/",
})

# In-memory fast cache (key -> (timestamp, data))
_MEM_CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes


def get_api_host() -> str:
    """Get active host from domain_manager or default."""
    global _ACTIVE_HOST
    try:
        import domain_manager
        dm_host = domain_manager.get_active_domain("cinestream")
        if dm_host:
            if not dm_host.startswith("http"):
                dm_host = f"https://{dm_host}"
            _ACTIVE_HOST = dm_host.rstrip("/")
    except Exception:
        pass
    return _ACTIVE_HOST


def set_api_host(new_host: str) -> str:
    global _ACTIVE_HOST
    clean = new_host.strip().rstrip("/")
    if not clean.startswith("http"):
        clean = f"https://{clean}"
    _ACTIVE_HOST = clean
    return _ACTIVE_HOST


def ping_service() -> dict[str, Any]:
    """Test latency and reachability to CineStream API."""
    host = get_api_host()
    start = time.perf_counter()
    try:
        r = _SESSION.get(f"{host}/api/tmdb/trending/all/day", timeout=8)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {
            "status": "online" if r.status_code == 200 else "error",
            "status_code": r.status_code,
            "latency_ms": elapsed_ms,
            "api_host": host,
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {
            "status": "offline",
            "error": str(exc),
            "latency_ms": elapsed_ms,
            "api_host": host,
        }


def _tmdb_get(endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
    """Execute GET request against the active host's /api/tmdb endpoint with memory caching."""
    host = get_api_host()
    clean_ep = endpoint.lstrip("/")
    cache_key = f"{clean_ep}_{json.dumps(params or {}, sort_keys=True)}"

    if cache_key in _MEM_CACHE:
        ts, data = _MEM_CACHE[cache_key]
        if time.time() - ts < CACHE_TTL:
            return data

    url = f"{host}/api/tmdb/{clean_ep}"
    try:
        r = _SESSION.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            _MEM_CACHE[cache_key] = (time.time(), data)
            return data
    except Exception as e:
        logger.warning(f"CineStream TMDB GET failed for {endpoint}: {e}")
    return None


def format_card(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize item into a consistent card format for UI consumption."""
    item_id = str(item.get("id") or item.get("tmdb_id") or "")
    media_type = item.get("media_type") or item.get("content_type")
    if not media_type:
        media_type = "tv" if (item.get("first_air_date") or item.get("name")) else "movie"

    title = item.get("title") or item.get("name") or "Untitled"
    release_date = item.get("release_date") or item.get("first_air_date") or ""
    year = release_date[:4] if len(release_date) >= 4 else ""

    poster = item.get("poster_path") or item.get("poster") or ""
    if poster and not poster.startswith("http"):
        poster = f"https://image.tmdb.org/t/p/w500{poster}"

    backdrop = item.get("backdrop_path") or item.get("backdrop") or ""
    if backdrop and not backdrop.startswith("http"):
        backdrop = f"https://image.tmdb.org/t/p/original{backdrop}"

    rating = round(float(item.get("vote_average") or item.get("rating") or 0.0), 1)

    genres = item.get("genres") or []
    if genres and isinstance(genres[0], dict):
        genre_list = [g.get("name", "") for g in genres if g.get("name")]
    elif isinstance(genres, str):
        genre_list = [g.strip() for g in genres.split(",") if g.strip()]
    else:
        genre_list = [str(g) for g in genres]

    return {
        "id": item_id,
        "tmdb_id": int(item_id) if item_id.isdigit() else 0,
        "imdb_id": item.get("imdb_id") or "",
        "title": title,
        "type": media_type,
        "year": year,
        "poster": poster,
        "backdrop": backdrop,
        "rating": rating,
        "vote_count": item.get("vote_count") or 0,
        "overview": item.get("overview") or "",
        "genres": genre_list,
        "source": "cinestream",
        "is_cinestream": True,
        "topic_url": f"/api/cinestream/info?id={item_id}&type={media_type}"
    }


def fetch_home(force_refresh: bool = False) -> dict[str, Any]:
    """
    Fetch hero, trending, popular movies, popular TV shows, and top rated items.
    Caches results in SQLite db for instant subsequent access.
    """
    if not force_refresh:
        cached = db.get_shelf("cinestream_home", max_age_seconds=600)
        if cached:
            return cached

    endpoints = {
        "hero": "trending/all/day",
        "trending": "trending/all/day",
        "popular_movies": "movie/popular",
        "popular_tv": "tv/popular",
        "top_rated": "movie/top_rated",
        "now_playing": "movie/now_playing"
    }

    results = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {name: executor.submit(_tmdb_get, ep) for name, ep in endpoints.items()}
        for name, fut in futures.items():
            try:
                res = fut.result()
                raw_items = res.get("results", []) if res else []
                cards = [format_card(item) for item in raw_items]
                results[name] = cards
                # Upsert to database in background
                db.upsert_items(raw_items)
            except Exception as e:
                logger.error(f"Error fetching CineStream {name}: {e}")
                results[name] = []

    # Ensure hero has backdrops
    hero_items = [item for item in results.get("hero", []) if item.get("backdrop")]
    if not hero_items:
        hero_items = results.get("hero", [])[:6]
    results["hero"] = hero_items[:8]

    # Save to SQLite shelf
    if results.get("trending") or results.get("popular_movies"):
        db.save_shelf("cinestream_home", results)

    return results


def search(query: str, page: int = 1) -> list[dict[str, Any]]:
    """Live search across CineStream catalog with local SQLite fallback."""
    clean_q = query.strip()
    if not clean_q:
        return []

    res = _tmdb_get("search/multi", {"query": clean_q, "page": page})
    if res and res.get("results"):
        raw_items = res["results"]
        # Filter out person media_type
        filtered = [item for item in raw_items if item.get("media_type") in ("movie", "tv", None) and (item.get("poster_path") or item.get("backdrop_path"))]
        db.upsert_items(filtered)
        return [format_card(item) for item in filtered]

    # Fallback to local database FTS
    local_items = db.search_db(clean_q, limit=30)
    return [format_card(item) for item in local_items]


def fetch_genre(genre_id: str, content_type: str = "movie", page: int = 1) -> list[dict[str, Any]]:
    """Fetch movies or TV shows by genre ID."""
    ctype = "tv" if content_type == "tv" else "movie"
    res = _tmdb_get(f"discover/{ctype}", {"with_genres": genre_id, "page": page})
    if res and res.get("results"):
        raw_items = res["results"]
        db.upsert_items(raw_items)
        return [format_card(item) for item in raw_items]
    return []


def fetch_genres_list(content_type: str = "movie") -> list[dict[str, Any]]:
    """Return all available genres for movie or tv."""
    ctype = "tv" if content_type == "tv" else "movie"
    res = _tmdb_get(f"genre/{ctype}/list")
    if res and res.get("genres"):
        return res["genres"]
    if ctype == "tv":
        return [
            {"id": 10759, "name": "Action & Adventure"},
            {"id": 16, "name": "Animation"},
            {"id": 35, "name": "Comedy"},
            {"id": 80, "name": "Crime"},
            {"id": 99, "name": "Documentary"},
            {"id": 18, "name": "Drama"},
            {"id": 10751, "name": "Family"},
            {"id": 10762, "name": "Kids"},
            {"id": 9648, "name": "Mystery"},
            {"id": 10763, "name": "News"},
            {"id": 10764, "name": "Reality"},
            {"id": 10765, "name": "Sci-Fi & Fantasy"},
            {"id": 10766, "name": "Soap"},
            {"id": 10767, "name": "Talk"},
            {"id": 10768, "name": "War & Politics"},
            {"id": 37, "name": "Western"}
        ]
    return [
        {"id": 28, "name": "Action"},
        {"id": 12, "name": "Adventure"},
        {"id": 16, "name": "Animation"},
        {"id": 35, "name": "Comedy"},
        {"id": 80, "name": "Crime"},
        {"id": 99, "name": "Documentary"},
        {"id": 18, "name": "Drama"},
        {"id": 10751, "name": "Family"},
        {"id": 14, "name": "Fantasy"},
        {"id": 36, "name": "History"},
        {"id": 27, "name": "Horror"},
        {"id": 10402, "name": "Music"},
        {"id": 9648, "name": "Mystery"},
        {"id": 10749, "name": "Romance"},
        {"id": 878, "name": "Sci-Fi"},
        {"id": 10770, "name": "TV Movie"},
        {"id": 53, "name": "Thriller"},
        {"id": 10752, "name": "War"},
        {"id": 37, "name": "Western"}
    ]


def discover_filter(
    content_type: str = "movie",
    query: Optional[str] = None,
    genre_id: Optional[str] = None,
    sort_by: Optional[str] = None,
    year: Optional[str] = None,
    min_rating: Optional[float] = None,
    page: int = 1
) -> dict[str, Any]:
    """
    Advanced discovery & filter engine matching the onstream.vin website filters:
    - Search query
    - All Genres
    - Popularity / Newest / Top Rated sorting
    - Year
    - Minimum Rating (1+ to 9+)
    """
    if content_type in ("all", "multi", "home"):
        ctype = "all"
    else:
        ctype = "tv" if content_type == "tv" else "movie"
    clean_q = (query or "").strip()

    if clean_q:
        params = {"query": clean_q, "page": page}
        endpoint = "search/multi" if ctype == "all" else f"search/{ctype}"
    else:
        if ctype == "all":
            ctype = "movie"
        endpoint = f"discover/{ctype}"
        sort_val = sort_by or "popularity.desc"
        if sort_val == "newest":
            sort_val = "first_air_date.desc" if ctype == "tv" else "release_date.desc"
        elif sort_val == "top_rated":
            sort_val = "vote_average.desc"
        elif sort_val == "popular":
            sort_val = "popularity.desc"

        params = {
            "page": page,
            "sort_by": sort_val,
        }
        if sort_val == "vote_average.desc":
            params["vote_count.gte"] = 50

        if genre_id and str(genre_id).strip():
            params["with_genres"] = str(genre_id).strip()

        if year and str(year).strip():
            yr_str = str(year).strip()
            if ctype == "tv":
                params["first_air_date_year"] = yr_str
            else:
                params["primary_release_year"] = yr_str

        if min_rating is not None and float(min_rating) > 0:
            params["vote_average.gte"] = float(min_rating)

    res = _tmdb_get(endpoint, params)
    if res and "results" in res:
        raw_items = res.get("results", [])
        valid_items = [it for it in raw_items if it.get("poster_path") or it.get("backdrop_path")]
        db.upsert_items(valid_items)
        cards = [format_card(it) for it in valid_items]
        return {
            "results": cards,
            "page": res.get("page", page),
            "total_pages": res.get("total_pages", 1),
            "total_results": res.get("total_results", len(cards))
        }

    return {"results": [], "page": page, "total_pages": 0, "total_results": 0}


def fetch_info(item_id: str, content_type: str = "movie") -> Optional[dict[str, Any]]:
    """Fetch complete metadata, cast, similar items, and TV seasons."""
    ctype = "tv" if content_type == "tv" else "movie"
    res = _tmdb_get(f"{ctype}/{item_id}", {"append_to_response": "credits,similar,recommendations,videos"})
    if not res:
        # Check SQLite db cache
        local = db.get_item(item_id)
        if local:
            return format_card(local)
        return None

    res["content_type"] = ctype
    db.upsert_item(res)
    card = format_card(res)

    # Add cast
    cast_raw = res.get("credits", {}).get("cast", [])
    card["cast"] = [
        {
            "name": c.get("name", ""),
            "character": c.get("character", ""),
            "profile": f"https://image.tmdb.org/t/p/w185{c.get('profile_path')}" if c.get("profile_path") else ""
        }
        for c in cast_raw[:15]
    ]

    # Add trailer key if available
    videos = res.get("videos", {}).get("results", [])
    trailers = [v for v in videos if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser")]
    if trailers:
        card["trailer_key"] = trailers[0].get("key", "")

    # For TV shows, attach seasons summary
    if ctype == "tv":
        seasons = res.get("seasons", [])
        clean_seasons = []
        for s in seasons:
            s_num = s.get("season_number", 0)
            if s_num > 0:  # Exclude specials (season 0) by default
                clean_seasons.append({
                    "season_number": s_num,
                    "name": s.get("name") or f"Season {s_num}",
                    "episode_count": s.get("episode_count", 0),
                    "poster": f"https://image.tmdb.org/t/p/w300{s.get('poster_path')}" if s.get("poster_path") else ""
                })
        card["seasons"] = sorted(clean_seasons, key=lambda x: x["season_number"])
        card["number_of_seasons"] = res.get("number_of_seasons", len(clean_seasons))
        card["number_of_episodes"] = res.get("number_of_episodes", 0)

    # Similar recommendations
    sim = res.get("recommendations", {}).get("results") or res.get("similar", {}).get("results", [])
    card["similar"] = [format_card(s) for s in sim[:10]]

    return card


def fetch_season(item_id: str, season_number: int) -> list[dict[str, Any]]:
    """Fetch episode list for a TV season with guaranteed visual thumbnails."""
    cached_eps = db.get_season_episodes(item_id, season_number)
    if cached_eps and len(cached_eps) > 0:
        return cached_eps

    res = _tmdb_get(f"tv/{item_id}/season/{season_number}")
    if res and res.get("episodes"):
        episodes_raw = res["episodes"]
        season_poster = f"https://image.tmdb.org/t/p/w500{res.get('poster_path')}" if res.get("poster_path") else ""
        parent = db.get_item(item_id)
        parent_img = (parent.get("backdrop") if parent else "") or (parent.get("poster") if parent else "")
        fallback_img = season_poster or parent_img

        db.save_season_episodes(item_id, season_number, episodes_raw, fallback_img=fallback_img)
        return db.get_season_episodes(item_id, season_number)

    return []


def get_stream_servers(
    item_id: str,
    content_type: str = "movie",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    imdb_id: Optional[str] = None
) -> list[dict[str, Any]]:
    """
    Generate the full list of ad-free streaming embed servers for playback.
    u = tmdb_id, o = imdb_id, d = season, r = episode
    """
    u = str(item_id)
    o = str(imdb_id or "")
    is_tv = (content_type == "tv") and (season is not None) and (episode is not None)
    d = season if is_tv else None
    r = episode if is_tv else None

    # Complete 30-server mirror network from onstream.vin (movies.codes/server.js)
    # Ordered by reliability, ad-freedom, and stream speed
    raw_servers = [
        ("vidlink", "VidLink (1080p Ultra HD)", "🛡️ Zero Ads • Multi-Audio",
         f"https://vidlink.pro/tv/{u}/{d}/{r}?primaryColor=dcfc35&secondaryColor=141414&iconColor=dcfc35&icons=default&player=default&title=true&poster=true&autoplay=false&nextbutton=true" if is_tv else f"https://vidlink.pro/movie/{u}?primaryColor=dcfc35&secondaryColor=141414&iconColor=dcfc35&icons=default&player=default&title=true&poster=true&autoplay=false"),
        ("flaxmovies", "FlaxMovies (Shield Player)", "🛡️ Clean Stream • No Ads",
         f"https://flaxmovies.xyz/embed/tv/{u}/{d}/{r}" if is_tv else f"https://flaxmovies.xyz/embed/movie/{u}"),
        ("moviesapi", "MoviesAPI (Direct Play)", "🛡️ Fast • Zero Popups",
         f"https://moviesapi.to/tv/{u}-{d}-{r}" if is_tv else f"https://moviesapi.to/movie/{u}"),
        ("autoembed", "AutoEmbed (Cloud Player)", "⚡ Cloud Stream",
         f"https://player.autoembed.cc/embed/tv/{u}/{d}/{r}" if is_tv else f"https://player.autoembed.cc/embed/movie/{u}"),
        ("primesrc", "PrimeSrc (Direct Mirror)", "🛡️ Clean Stream",
         f"https://primesrc.me/embed/tv?imdb={(o or u)}&season={d}&episode={r}" if is_tv else f"https://primesrc.me/embed/movie?tmdb={u}"),
        ("vidsrc-embed", "VidSrc Embed", "HD Mirror",
         f"https://vidsrc-embed.ru/embed/tv/{(o or u)}-{d}-{r}" if is_tv else f"https://vidsrc-embed.ru/embed/movie/{(o or u)}"),
        ("cinesrc", "CineSrc (Fast Stream)", "⚡ Fast Mirror",
         f"https://cinesrc.st/embed/tv/{u}?s={d}&e={r}" if is_tv else f"https://cinesrc.st/embed/movie/{u}"),
        ("2embed", "2Embed", "HD Stream",
         f"https://www.2embed.cc/embedtv/{u}&s={d}&e={r}" if is_tv else f"https://www.2embed.cc/embed/{u}"),
        ("vidcore", "VidCore (Neon 1080p)", "1080p Stream",
         f"https://vidcore.net/tv/{u}/{d}/{r}?theme=%23DCFC35" if is_tv else f"https://vidcore.net/movie/{u}?theme=%23DCFC35"),
        ("peestream", "PeeStream (Mirror 10)", "Stream Mirror",
         f"https://peestream.in/embed/?tmdbId={u}&type=show&season={d}&episode={r}" if is_tv else f"https://peestream.in/embed/?tmdbId={u}&type=movie"),
        ("cinezo", "Cinezo (Mirror 11)", "Stream Mirror",
         f"https://player.cinezo.live/embed/tv/{u}/{d}/{r}" if is_tv else f"https://player.cinezo.live/embed/movie/{u}"),
        ("videasy", "VidEasy", "Fast Mirror",
         f"https://player.videasy.net/tv/{u}/{d}/{r}" if is_tv else f"https://player.videasy.net/movie/{u}"),
        ("111movies", "111Movies", "Server 13",
         f"https://111movies.com/tv/{u}/{d}/{r}" if is_tv else f"https://111movies.com/movie/{u}"),
        ("vidzee", "VidZee", "Server 14",
         f"https://player.vidzee.wtf/embed/tv/{u}/{d}/{r}" if is_tv else f"https://player.vidzee.wtf/embed/movie/{u}"),
        ("vidsrc", "VidSrc", "Server 15",
         f"https://vidsrc.to/embed/tv/{u}/{d}/{r}" if is_tv else f"https://vidsrc.to/embed/movie/{u}"),
        ("mapple", "Mapple", "Server 16",
         f"https://mapple.uk/watch/tv/{u}-{d}-{r}" if is_tv else f"https://mapple.uk/watch/movie/{u}"),
        ("multiembed", "MultiEmbed", "Server 17",
         f"https://multiembed.mov/?video_id={u}&tmdb=1&s={d}&e={r}" if is_tv else f"https://multiembed.mov/?video_id={u}&tmdb=1"),
        ("vixsrc", "VixSrc", "Server 18",
         f"https://vixsrc.to/tv/{u}/{d}/{r}" if is_tv else f"https://vixsrc.to/movie/{u}"),
        ("vidlove", "VidLove", "Server 19",
         f"https://player.vidlove.cc/embed/tv/{u}/{d}/{r}" if is_tv else f"https://player.vidlove.cc/embed/movie/{u}"),
        ("vidfast", "VidFast", "Server 20",
         f"https://vidfast.vc/tv/{u}/{d}/{r}" if is_tv else f"https://vidfast.vc/movie/{u}"),
        ("filmu", "Filmu", "Server 21",
         f"https://embed.filmu.in/tv/{u}/{d}/{r}" if is_tv else f"https://embed.filmu.in/movie/{u}"),
        ("vaplayer", "VAPlayer", "Server 22",
         f"https://vaplayer.ru/embed/tv/{u}/{d}/{r}?skin=netflix&color=dcfc35" if is_tv else f"https://vaplayer.ru/embed/movie/{u}?skin=netflix&color=dcfc35"),
        ("vares", "Vares", "Server 23",
         f"https://vares.app/tv/{u}/{d}/{r}" if is_tv else f"https://vares.app/movie/{u}"),
        ("vidking", "VidKing", "Server 24",
         f"https://www.vidking.net/embed/tv/{u}/{d}/{r}" if is_tv else f"https://www.vidking.net/embed/movie/{u}"),
        ("vidbolt", "VidBolt", "Server 25",
         f"https://vidbolt.xyz/tv/{u}/{d}/{r}" if is_tv else f"https://vidbolt.xyz/movie/{u}"),
        ("vidnest", "VidNest", "Server 26",
         f"https://vidnest.fun/tv/{u}/{d}/{r}" if is_tv else f"https://vidnest.fun/movie/{u}"),
        ("vidsrcsu", "VidSrc.su", "Server 27",
         f"https://vidsrc.su/tv/{u}/{d}/{r}?autoplay=false&colour=dcfc35" if is_tv else f"https://vidsrc.su/movie/{u}?autoplay=false&colour=dcfc35"),
        ("vidgod", "VidGod", "Server 28",
         f"https://vidgod.net/tv/{u}/{d}/{r}" if is_tv else f"https://vidgod.net/movie/{u}"),
        ("vidsrcme", "VidSrc.me", "Server 29",
         f"https://vidsrc.me/embed/tv?tmdb={u}&season={d}&episode={r}" if is_tv else f"https://vidsrc.me/embed/movie?tmdb={u}"),
        ("1embed", "1Embed", "Server 30",
         f"https://1embed.cc/embed/tv/{u}/{d}/{r}" if is_tv else f"https://1embed.cc/embed/movie/{u}")
    ]

    return [{"id": sid, "name": sname, "badge": sbadge, "url": surl} for sid, sname, sbadge, surl in raw_servers]

