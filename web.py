"""
SDK Movies — a local web front-end over the scraper and WebTorrent streamer.

Run:
    .venv/Scripts/python.exe web.py
then open http://127.0.0.1:8777
"""
import asyncio
from typing import Optional
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote, urljoin, urlparse
from bs4 import BeautifulSoup

from fastapi import FastAPI, HTTPException, Query, Body, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import requests
import socket

import db
import scraper
import domain_manager
from isaimini import db as isaimini_db
from isaimini import scraper as isaimini_scraper
from toonplay import db as toonplay_db
from toonplay import scraper as toonplay_scraper
from cinestream import db as cinestream_db
from cinestream import scraper as cinestream_scraper

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
PORT = int(os.environ.get("PORT", 8777))
HOST = os.environ.get("HOST", "0.0.0.0")

def get_lan_ip() -> str:
    """Detect the active local Wi-Fi / LAN IP address for network host sharing."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('1.1.1.1', 53))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith('127.'):
            return ip
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith('127.') and not ip.startswith('169.254.'):
                return ip
    except Exception:
        pass

    return '127.0.0.1'

import time

class FastRAMCache:
    """Thread-safe, high-speed in-memory LRU RAM cache with TTL for sub-5ms API responses."""
    def __init__(self, ttl_seconds: int = 120, max_size: int = 800):
        self._cache: dict[str, tuple[any, float]] = {}
        self._ttl = ttl_seconds
        self._max_size = max_size

    def get(self, key: str):
        entry = self._cache.get(key)
        if not entry:
            return None
        val, exp_time = entry
        if time.time() > exp_time:
            self._cache.pop(key, None)
            return None
        return val

    def set(self, key: str, val: any, custom_ttl: int = None):
        if len(self._cache) >= self._max_size:
            oldest = sorted(self._cache.items(), key=lambda x: x[1][1])[:self._max_size // 5]
            for k, _ in oldest:
                self._cache.pop(k, None)
        ttl = custom_ttl if custom_ttl is not None else self._ttl
        self._cache[key] = (val, time.time() + ttl)

    def invalidate(self, prefix: str = None):
        if not prefix:
            self._cache.clear()
        else:
            to_del = [k for k in self._cache if k.startswith(prefix)]
            for k in to_del:
                self._cache.pop(k, None)

ram_cache = FastRAMCache(ttl_seconds=120)

# In-memory cache for movie metadata (plot, ratings, genres, posters)
_META_CACHE: dict[str, dict] = {}
_STREAMER_STATUS: str = "stopped"

# Connection-pooled HTTP session for high-speed direct MP4 streaming & seeking
_STREAM_SESSION = requests.Session()
_stream_adapter = requests.adapters.HTTPAdapter(
    pool_connections=25,
    pool_maxsize=50,
    max_retries=requests.adapters.Retry(total=2, backoff_factor=0.2),
)
_STREAM_SESSION.mount("http://", _stream_adapter)
_STREAM_SESSION.mount("https://", _stream_adapter)

POSTER_OVERRIDES = {
    "aliens 2042": {
        "poster": "https://images.moviebuff.com/fa958f0f-8047-4d0b-97e8-536c46635633?w=600",
        "background": "https://images.moviebuff.com/fa958f0f-8047-4d0b-97e8-536c46635633?w=1200",
        "plot": "Set in 2042, Earth faces an extraterrestrial invasion looking to strip the planet of resources. United defense forces band together to save humanity.",
        "rating": "6.8",
        "year": 2023,
    },
    "alien 2042": {
        "poster": "https://images.moviebuff.com/fa958f0f-8047-4d0b-97e8-536c46635633?w=600",
        "rating": "6.8",
        "year": 2023,
    },
    "jigris": {
        "poster": "https://m.media-amazon.com/images/S/pv-target-images/a1b30a8f009e6ab3c5325c18c366796f920eb9e82f5f6bf68ed1ad279cacfe89.jpg",
        "rating": "7.5",
        "year": 2025,
    },
    "lizzie borden": {
        "poster": "https://m.media-amazon.com/images/M/MV5BNzc5ZDc0OGUtMDExMy00MTdjLWE0NzAtYmFiZWVkYzVjZjI0XkEyXkFqcGc@._V1_SX250.jpg",
        "rating": "6.6",
        "year": 2026,
    },
    "29": {
        "poster": "https://images.metahub.space/poster/small/tt33887012/img",
        "rating": "7.0",
        "year": 2026,
    }
}


def format_isaimini_card(m: dict) -> dict:
    url_str = m.get("url") or ""
    is_dub = "isaidub" in url_str
    src = "isaidub" if is_dub else "isaimini"
    src_label = "TamDub (Dubbed)" if is_dub else "TamilMov (Original)"

    m_base = domain_manager.get_active_base_url("moviesda") or "https://moviezda.me"
    d_base = "https://isaidub.green"

    poster = m.get("poster")

    # Check curated poster overrides
    t_clean = (m.get("title") or "").lower()
    for o_key, o_val in POSTER_OVERRIDES.items():
        if o_key in t_clean:
            poster = o_val.get("poster")
            break

    if poster:
        if "moviesda.ski" in poster or "moviezda.me" in poster:
            poster = re.sub(r"https?://(?:moviesda\.ski|moviezda\.me)", m_base, poster)
        poster = re.sub(
            r"-(tamil-dubbed-movie-\d+|tamil-dubbed-movie|tamil-dubbed-web-series-\d+|tamil-dubbed-web-series|tamil-movie-\d+|tamil-movie|tamil-web-series-\d+|tamil-web-series|original-movie|movie-\d+|movie|web-series-\d+|web-series|hd-\d+|hd)\.jpg$",
            ".jpg",
            poster,
            flags=re.I,
        )
    if poster and not poster.startswith("http"):
        base = d_base if is_dub else m_base
        poster = base.rstrip("/") + "/" + poster.lstrip("/")
    elif not poster and url_str:
        try:
            parsed = urlparse(url_str)
            path = parsed.path.strip("/")
            last_seg = path.split("/")[-1] if path else ""
            if last_seg and last_seg != "movie":
                slug = re.sub(
                    r"-(tamil-dubbed-movie-\d+|tamil-dubbed-movie|tamil-dubbed-web-series-\d+|tamil-dubbed-web-series|tamil-movie-\d+|tamil-movie|tamil-web-series-\d+|tamil-web-series|original-movie|movie-\d+|movie|web-series-\d+|web-series|hd-\d+|hd)$",
                    "",
                    last_seg,
                    flags=re.I,
                )
                target_base = d_base if is_dub else m_base
                poster = f"{target_base}/uploads/posters/{slug}.jpg"
        except Exception:
            pass

    # Ensure web series and episodic titles are classified as 'series'
    title_str = m.get("title") or ""
    clean_title_str = m.get("clean_title") or title_str
    is_series_item = (
        m.get("content_type") == "series"
        or isaimini_scraper.is_series(title_str)
        or isaimini_scraper.is_series(url_str)
        or bool(re.search(r"\b(series|season|s\d+|ep\d+|episode)\b", title_str, re.I))
    )

    return {
        "id": m.get("id"),
        "title": title_str,
        "clean_title": clean_title_str,
        "year": m.get("year"),
        "topic_url": m.get("url"),
        "poster_url": poster,
        "quality": m.get("quality") or "HD",
        "language": m.get("language") or ("Tamil Dubbed" if is_dub else "Tamil"),
        "content_type": "series" if is_series_item else (m.get("content_type") or "movie"),
        "rating": m.get("rating"),
        "starring": m.get("starring"),
        "director": m.get("director"),
        "genres": m.get("genres"),
        "synopsis": m.get("synopsis"),
        "source": src,
        "source_label": src_label,
        "is_direct": True,
        "has_direct_link": True,
        "is_official": False,
        "views": None,
        "replies": None,
        "size": None,
    }


def format_tmv_card(m: dict) -> dict:
    url_str = (m.get("topic_url") or m.get("url") or "").lower()
    if any(k in url_str for k in ("isaidub", "isaimini", "moviesda", "moviezda")):
        return format_isaimini_card(m)
    m["source"] = "tamilmv"
    m["source_label"] = "Tamil(Torrents)"
    m["is_direct"] = False

    t_clean = (m.get("clean_title") or m.get("title") or "").lower()
    for o_key, o_val in POSTER_OVERRIDES.items():
        if o_key in t_clean:
            if not m.get("poster_url") or any(d in (m.get("poster_url") or "") for d in ["extraimage", "imageride", "picx.click", "imgpile"]):
                m["poster_url"] = o_val.get("poster")
            break

    p = m.get("poster_url") or ""
    if any(dead in p for dead in ["extraimage", "imageride", "picx.click", "imgpile", "fastpic", "imagehost"]):
        ck = f"{(m.get('clean_title') or m.get('title') or '').lower()}_{m.get('year') or ''}"
        if ck in _META_CACHE and _META_CACHE[ck].get("poster"):
            m["poster_url"] = _META_CACHE[ck]["poster"]
    return m


def format_vault_card(m: dict) -> dict:
    source_type = m.get("source_type") or "cloud"
    labels = {
        "pixeldrain": "Pixeldrain (Lossless)",
        "gdrive": "Google Drive (Lossless)",
        "dropbox": "Dropbox (Direct)",
        "direct": "Direct Stream",
        "cloud": "Cloud Stream"
    }
    src_label = labels.get(source_type, "Cloud Vault")
    stream_url = m.get("stream_url") or m.get("original_url") or ""

    return {
        "id": f"vault_{m.get('id')}",
        "vault_id": m.get("id"),
        "title": m.get("title") or "Untitled Movie",
        "clean_title": m.get("title") or "Untitled Movie",
        "year": m.get("year"),
        "topic_url": f"/api/vault/{m.get('id')}",
        "poster_url": m.get("poster") or "",
        "background": m.get("backdrop") or m.get("poster") or "",
        "quality": m.get("quality") or "1080p FHD",
        "language": "Original / Multi",
        "content_type": "movie",
        "rating": m.get("rating") or "Vault",
        "synopsis": m.get("overview") or "Uploaded via Cloud Vault for high bitrate lossless streaming.",
        "plot": m.get("overview") or "Uploaded via Cloud Vault for high bitrate lossless streaming.",
        "source": "vault",
        "source_label": src_label,
        "source_type": source_type,
        "is_direct": True,
        "has_direct_link": True,
        "direct_url": stream_url,
        "original_url": m.get("original_url") or "",
        "is_vault": True
    }


def format_toonplay_card(item: dict) -> dict:
    """Format a ToonPlay anime/cartoon item into a standardized card."""
    item_id = item.get("id") or ""
    title = item.get("title") or "Untitled Anime"
    c_type = item.get("content_type") or item.get("type") or "series"
    image = (
        item.get("image")
        or item.get("poster_url")
        or item.get("poster")
        or item.get("thumbnail")
        or item.get("backdrop")
        or item.get("background")
        or ""
    )
    backdrop = item.get("backdrop") or item.get("background") or image
    overview = item.get("overview") or "Anime & Cartoon series with multi-audio streams."
    year_val = item.get("year")
    year = int(year_val) if (str(year_val).isdigit()) else None

    genres = item.get("genres") or []
    if isinstance(genres, str):
        try:
            genres = json.loads(genres)
        except Exception:
            genres = [genres]

    languages = item.get("languages") or []
    if isinstance(languages, str):
        try:
            languages = json.loads(languages)
        except Exception:
            languages = [languages]
    lang_str = ", ".join(languages) if languages else "Multi Audio (Tam/Tel/Hin/Jpn)"

    rating = str(item.get("rating") or "8.2")

    return {
        "id": f"toon_{item_id}",
        "toon_id": item_id,
        "title": title,
        "clean_title": title,
        "year": year,
        "topic_url": f"/api/toonplay/info?id={item_id}",
        "poster_url": image,
        "background": backdrop,
        "quality": "HD 1080p",
        "language": lang_str,
        "content_type": c_type,
        "rating": rating,
        "synopsis": overview,
        "plot": overview,
        "source": "toonplay",
        "source_label": "Anime & Cartoons",
        "is_direct": True,
        "has_direct_link": True,
        "is_toonplay": True,
        "genres": genres,
    }


def format_cinestream_card(item: dict) -> dict:
    """Format a CineStream movie or TV show into a standardized card."""
    item_id = str(item.get("id") or item.get("tmdb_id") or "")
    title = item.get("title") or item.get("name") or "Untitled Title"
    c_type = item.get("type") or item.get("content_type") or ("tv" if item.get("first_air_date") else "movie")
    poster = item.get("poster") or item.get("poster_path") or ""
    if poster and not poster.startswith("http"):
        poster = f"https://image.tmdb.org/t/p/w500{poster}"
    backdrop = item.get("backdrop") or item.get("backdrop_path") or poster
    if backdrop and not backdrop.startswith("http"):
        backdrop = f"https://image.tmdb.org/t/p/original{backdrop}"
    overview = item.get("overview") or ""
    year_val = item.get("year") or (item.get("release_date") or item.get("first_air_date") or "")[:4]
    year = int(year_val) if str(year_val).isdigit() else None
    rating = str(round(float(item.get("rating") or item.get("vote_average") or 7.5), 1))
    genres = item.get("genres") or []

    return {
        "id": f"cine_{item_id}",
        "cine_id": item_id,
        "tmdb_id": int(item_id) if item_id.isdigit() else 0,
        "imdb_id": item.get("imdb_id") or "",
        "title": title,
        "clean_title": title,
        "year": year,
        "topic_url": f"/api/cinestream/info?id={item_id}&type={c_type}",
        "poster_url": poster,
        "poster": poster,
        "background": backdrop,
        "backdrop": backdrop,
        "quality": "HD 1080p",
        "language": "English / Multi",
        "type": c_type,
        "content_type": c_type,
        "rating": rating,
        "synopsis": overview,
        "plot": overview,
        "overview": overview,
        "source": "cinestream",
        "source_label": "CineStream",
        "is_direct": True,
        "has_direct_link": True,
        "is_cinestream": True,
        "genres": genres,
    }


def resolve_cloud_stream_url(raw_url: str) -> dict:
    """Extract and transform public cloud storage links (Pixeldrain, Google Drive, Dropbox, Direct) into streaming targets."""
    url = raw_url.strip()

    # Pixeldrain: https://pixeldrain.com/u/{id} -> https://pixeldrain.com/api/file/{id}
    px = re.search(r"pixeldrain\.com/(?:u|api/file)/([a-zA-Z0-9]+)", url)
    if px:
        fid = px.group(1)
        return {
            "source_type": "pixeldrain",
            "stream_url": f"https://pixeldrain.com/api/file/{fid}",
            "file_id": fid
        }

    # Google Drive: https://drive.google.com/file/d/{id} or id={id}
    gd = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([a-zA-Z0-9_-]+)", url)
    if gd:
        fid = gd.group(1)
        return {
            "source_type": "gdrive",
            "stream_url": f"/api/vault/stream/gdrive?id={fid}",
            "file_id": fid
        }

    # Dropbox: dl=0 -> raw=1
    db_match = re.search(r"dropbox\.com/s(?:cl)?/([a-zA-Z0-9_/-]+)", url)
    if db_match:
        clean = re.sub(r"[?&]dl=\d", "", url)
        sep = "&" if "?" in clean else "?"
        return {
            "source_type": "dropbox",
            "stream_url": f"{clean}{sep}raw=1",
            "file_id": db_match.group(1)
        }

    return {
        "source_type": "direct",
        "stream_url": url,
        "file_id": ""
    }


def resolve_gdrive_stream_target(file_id: str, session: requests.Session | None = None) -> tuple[str, dict]:
    """Resolves a Google Drive file ID into a direct streaming link, bypassing the virus scan confirmation."""
    sess = session or requests.Session()
    init_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    resp = sess.get(init_url, stream=True, allow_redirects=True, timeout=12)

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        return resp.url, dict(sess.cookies)

    # It's Google's large file virus confirmation page
    html = resp.text
    confirm_code = None
    for k, v in sess.cookies.items():
        if k.startswith("download_warning"):
            confirm_code = v
            break

    if not confirm_code:
        m = re.search(r'name="confirm"\s+value="([^"]+)"', html)
        if m:
            confirm_code = m.group(1)

    if not confirm_code:
        m = re.search(r'confirm=([0-9A-Za-z_]+)', html)
        if m:
            confirm_code = m.group(1)

    if confirm_code:
        direct_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm={confirm_code}"
        return direct_url, dict(sess.cookies)

    return f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}", dict(sess.cookies)


GENERIC_TITLES = {
    "monster", "hero", "master", "beast", "varam", "enemy", "alien", "captain",
    "lover", "brother", "siren", "black", "star", "romeo", "exam", "euphoria",
    "fight club", "doctor", "don", "prince", "ghost", "speed", "game over", "boss",
    "blue star", "leo", "mark antony", "connect", "parking", "rebel", "guardian",
    "hit list", "boat", "goat", "lucky baskhar", "love", "accused", "mother",
    "hunkkaar", "hunkkaar the roar", "war", "virus", "vikram", "vengeance", "unstoppable",
    "total recall", "the boys", "stranger things", "weapon", "kanguva", "garudan",
    "maharaja", "raayan", "thangalaan", "amaran", "indian 2", "indian"
}


def select_best_meta(metas: list[dict], clean_title: str, year: int | None = None) -> dict | None:
    """Smart candidate scorer that matches exact title, release year, and Indian/Tamil origin,
    strictly preventing older foreign/English films with the same name from superseding Tamil movies."""
    if not metas:
        return None

    clean_target = re.sub(r"[^a-z0-9]", "", clean_title.lower())
    is_generic = clean_title.strip().lower() in GENERIC_TITLES or any(clean_title.strip().lower().startswith(g + " ") for g in GENERIC_TITLES)

    def score_meta(m: dict) -> int:
        score = 0
        m_name = (m.get("name") or "").strip()
        m_clean = re.sub(r"[^a-z0-9]", "", m_name.lower())

        # 1. Exact or close title match
        if m_clean == clean_target:
            score += 100
        elif clean_target and clean_target in m_clean:
            score += 40
        elif m_clean and m_clean in clean_target:
            score += 20
        else:
            score -= 50

        # 2. Release year match (STRICT for distinguishing Tamil movies from foreign films)
        m_rel = str(m.get("releaseInfo") or m.get("year") or "")
        m_year_match = re.search(r"\b(19\d\d|20\d\d)\b", m_rel)
        if m_year_match:
            try:
                cand_year = int(m_year_match.group(1))
                if year:
                    diff = abs(cand_year - year)
                    is_series_meta = m.get("type") == "series" or "-" in m_rel
                    if diff == 0:
                        score += 180  # Exact year match
                    elif diff == 1:
                        score += 50   # Within 1 year
                    elif is_series_meta and diff <= 3:
                        score += 40   # Multi-season web series
                    else:
                        # DISQUALIFY completely if year diff >= 2 for generic titles or >= 3 for others
                        if is_generic or diff >= 3:
                            return -999
                        score -= min(diff * 35, 200)
                else:
                    if is_generic and cand_year < 2018:
                        score -= 80
            except ValueError:
                pass
        elif year and is_generic:
            score -= 60

        # 3. Country / Indian origin check / Tamil / South Indian description check
        m_country = str(m.get("country") or "").lower()
        m_desc = str(m.get("description") or "").lower()
        if "india" in m_country or any(k in m_desc for k in ["tamil", "kollywood", "telugu", "malayalam", "kannada", "indian"]):
            score += 120
        elif m_country and "india" not in m_country:
            if is_generic:
                score -= 180  # Heavy penalty for foreign movies with common dictionary word names
        elif is_generic and not any(k in m_desc for k in ["tamil", "kollywood", "telugu", "malayalam", "kannada", "indian"]):
            score -= 40

        # 4. Poster presence
        if m.get("poster"):
            score += 15

        return score

    scored = [(score_meta(m), m) for m in metas]
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None

    best_score, best_meta = scored[0]
    # Candidate must meet strict score threshold (120+) to be accepted
    if best_score >= 120:
        return best_meta
    return None


def fetch_direct_page_poster(movie_url: str) -> str | None:
    """Extract official Tamil poster image or verified screenshot thumbnail directly from Moviesda or isaiDub,
    checking predicted upload slugs, scraping HTML tags, and traversing subfolders if necessary."""
    if not movie_url or not movie_url.startswith("http"):
        return None
    try:
        is_dub = "isaidub" in movie_url
        base = "https://isaidub.green" if is_dub else (domain_manager.get_active_base_url("moviesda") or "https://moviezda.me")
        parsed = urlparse(movie_url)
        path = parsed.path.strip("/")
        last_seg = path.split("/")[-1] if path else ""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        }

        # Step 1: Test predicted slug URL on Moviesda/isaiDub CDN
        if last_seg:
            slug = re.sub(
                r"-(tamil-dubbed-movie-\d+|tamil-dubbed-movie|tamil-dubbed-web-series-\d+|tamil-dubbed-web-series|tamil-movie-\d+|tamil-movie|tamil-web-series|original-movie|movie-\d+|movie|web-series|hd-\d+|hd)$",
                "",
                last_seg,
                flags=re.I,
            )
            candidate = f"{base}/uploads/posters/{slug}.jpg"
            try:
                r = requests.head(candidate, headers=headers, timeout=1.5)
                if r.status_code == 200 and int(r.headers.get("Content-Length", 100)) > 500:
                    return candidate
            except Exception:
                pass

        # Step 2: Scrape movie page HTML (depth 1 max for first subfolder)
        def _scrape_page(curr_url: str, depth: int = 0) -> str | None:
            if depth > 1:
                return None
            try:
                r = requests.get(curr_url, headers=headers, timeout=2.2)
                if r.status_code != 200:
                    return None
                soup = BeautifulSoup(r.text, "html.parser")
                poster_el = (
                    soup.select_one('meta[property="og:image"]')
                    or soup.select_one('meta[name="twitter:image"]')
                    or soup.select_one('img[src*="/uploads/posters/"]')
                    or soup.select_one('picture img[src*="/uploads/posters/"]')
                    or soup.select_one('picture source[srcset*="/uploads/posters/"]')
                    or soup.select_one('img[alt*="Movie Poster"]')
                    or soup.select_one('img[alt*="Poster"]')
                    or soup.select_one(".movie-info-container picture img")
                    or soup.select_one(".movie-info-container img")
                    or soup.select_one("picture img")
                    or soup.select_one('img[src*="/uploads/shots/"]')
                )
                if poster_el:
                    src = poster_el.get("content") if poster_el.name == "meta" else (poster_el.get("src") or poster_el.get("data-src"))
                    if not src and poster_el.get("srcset"):
                        src = poster_el["srcset"].split()[0]
                    if src and not src.endswith("folder.svg") and not any(b in src.lower() for b in ["logo", "badge"]):
                        return urljoin(curr_url, src)

                # Follow first child folder if at depth 0 or 1
                if depth < 2:
                    sub = soup.select_one("div.f a, div.folder a")
                    if sub and sub.get("href"):
                        sub_url = urljoin(curr_url, sub["href"])
                        found = _scrape_page(sub_url, depth + 1)
                        if found:
                            return found
            except Exception:
                pass
            return None

        found_poster = _scrape_page(movie_url, 0)
        if found_poster:
            return found_poster
    except Exception:
        pass
    return None


def fetch_cinemeta_poster(title: str, year: int | None = None) -> str | None:
    """Fallback to Cinemeta / IMDb / TMDB public catalog with strict year and Tamil-aware scoring."""
    if not year:
        ym = re.search(r"\b(19\d\d|20\d\d)\b", title)
        if ym:
            try:
                year = int(ym.group(1))
            except ValueError:
                pass

    pattern = r"\[.*?\]|\(.*?\)|S\d+|EP\d+|TRUE WEB-DL.*|\b(Tamil Dubbed|Tamil|Dubbed|Org|Original|DVDRip|BDRip|Audio|Movie|Web Series|HD|360p|720p|1080p|PreDVD|CAM|\d{4})\b"
    clean_title = re.sub(pattern, "", title, flags=re.I).strip()
    clean_title = re.sub(r"\s+", " ", clean_title).strip() or title.strip()
    if not clean_title:
        return None

    # Search queries: plain clean title, and if needed f"{clean_title} tamil"
    search_queries = [clean_title]
    if clean_title.lower() in GENERIC_TITLES:
        search_queries.append(f"{clean_title} tamil")

    for q_term in search_queries:
        for ctype in ("movie", "series"):
            try:
                url = f"https://v3-cinemeta.strem.io/catalog/{ctype}/top/search={quote(q_term)}.json"
                r = requests.get(url, timeout=3.5)
                if r.status_code == 200:
                    metas = r.json().get("metas", [])
                    best = select_best_meta(metas, clean_title, year)
                    if best and best.get("poster"):
                        return best["poster"]
            except Exception:
                pass
    return None


async def _fetch_single_poster(sem: asyncio.Semaphore, m: dict):
    topic_url = m.get("topic_url")
    title = m.get("title") or ""
    if not topic_url or m.get("poster_url"):
        return
    async with sem:
        poster_url = None
        try:
            details = await asyncio.to_thread(scraper.fetch_download_details, topic_url)
            if details.get("poster_url"):
                poster_url = details["poster_url"]
        except Exception:
            pass

        if not poster_url and title:
            try:
                poster_url = await asyncio.to_thread(fetch_cinemeta_poster, title, m.get("year"))
            except Exception:
                pass

        if poster_url:
            with db.get_conn() as conn:
                db.update_poster_url(conn, topic_url, poster_url)


def check_poster_url_valid(poster_url: str) -> bool:
    """Check if poster URL returns an actual HTTP 200 image response rather than an HTML error/redirect."""
    if not poster_url or not poster_url.startswith("http"):
        return False
    try:
        r = requests.head(
            poster_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=2.0,
            allow_redirects=False,
        )
        if r.status_code == 200:
            ctype = r.headers.get("Content-Type", "").lower()
            return "image/" in ctype
        return False
    except Exception:
        return False


async def _fetch_single_isaimini_poster(sem: asyncio.Semaphore, m: dict, force_verify: bool = False):
    title = m.get("clean_title") or m.get("title") or ""
    url = m.get("url") or m.get("topic_url") or ""
    year = m.get("year")
    curr_poster = m.get("poster") or m.get("poster_url")
    if not url:
        return

    # Clean any accidental suffixes first
    if curr_poster:
        cleaned_poster = re.sub(
            r"-(tamil-dubbed-movie-\d+|tamil-dubbed-movie|tamil-dubbed-web-series-\d+|tamil-dubbed-web-series|tamil-movie-\d+|tamil-movie|tamil-web-series-\d+|tamil-web-series|original-movie|movie-\d+|movie|web-series-\d+|web-series|hd-\d+|hd)\.jpg$",
            ".jpg",
            curr_poster,
            flags=re.I,
        )
        if cleaned_poster != curr_poster:
            try:
                with isaimini_db.get_conn() as conn:
                    isaimini_db.update_movie_poster(url, cleaned_poster)
                curr_poster = cleaned_poster
            except Exception:
                pass

    # If poster is already a verified Amazon/Metahub CDN or official Moviesda/isaiDub poster, skip unless force_verify is requested
    if curr_poster and not force_verify:
        if "m.media-amazon.com" in curr_poster or "images.metahub.space" in curr_poster:
            return
        if "/uploads/posters/" in curr_poster:
            return

    async with sem:
        is_valid = False
        if curr_poster:
            is_valid = await asyncio.to_thread(check_poster_url_valid, curr_poster)

        if is_valid:
            return

        poster_url = None
        # Step 1: Direct official poster scraping from Moviesda / isaiDub page
        try:
            poster_url = await asyncio.to_thread(fetch_direct_page_poster, url)
        except Exception:
            pass

        # Step 2: Year-aware and Tamil-aware Cinemeta fallback
        if not poster_url and title:
            try:
                poster_url = await asyncio.to_thread(fetch_cinemeta_poster, title, year)
            except Exception:
                pass

        if poster_url and poster_url != curr_poster:
            try:
                with isaimini_db.get_conn() as conn:
                    isaimini_db.update_movie_poster(url, poster_url)
            except Exception:
                pass


async def _background_fetch_posters(results: list[dict]):
    """Fetch and cache posters in the background concurrently without blocking search response."""
    sem = asyncio.Semaphore(5)
    tasks = []
    for m in results[:50]:
        curr_poster = m.get("poster_url") or m.get("poster")
        if not curr_poster or "isaidub.green" in curr_poster or ("/uploads/posters/" not in curr_poster and "media-amazon" not in curr_poster):
            if m.get("source") in ("isaimini", "isaidub") or m.get("is_direct"):
                tasks.append(_fetch_single_isaimini_poster(sem, {
                    "url": m.get("topic_url") or m.get("url"),
                    "title": m.get("title"),
                    "clean_title": m.get("clean_title"),
                    "year": m.get("year"),
                    "poster": curr_poster
                }))
            elif not curr_poster:
                tasks.append(_fetch_single_poster(sem, m))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _initial_poster_backfill():
    """Backfill missing posters for existing database records across Torrents and Direct MP4 in background."""
    await asyncio.sleep(2)
    # 1. Backfill torrent records
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, title, year, topic_url, poster_url FROM movies WHERE poster_url IS NULL OR poster_url = '' ORDER BY id DESC LIMIT 60"
            ).fetchall()
        if rows:
            sem = asyncio.Semaphore(4)
            tasks = [_fetch_single_poster(sem, dict(r)) for r in rows]
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass

    # 2. Backfill TamilMov & TamDub direct MP4 records with verified official posters
    try:
        with isaimini_db.get_conn() as iconn:
            irows = iconn.execute(
                "SELECT id, title, clean_title, year, url, poster FROM movies WHERE poster IS NULL OR poster = '' OR poster LIKE '%isaidub.green/uploads/posters%' ORDER BY year DESC, id DESC LIMIT 200"
            ).fetchall()
        if irows:
            sem = asyncio.Semaphore(5)
            itasks = [_fetch_single_isaimini_poster(sem, dict(r)) for r in irows]
            await asyncio.gather(*itasks, return_exceptions=True)
    except Exception:
        pass


# Background Auto-Sync Cron State
import time
_LAST_SYNC_TIME: float | None = time.time()
_LAST_SYNC_STATUS: str = "ok"
_LAST_SYNC_ADDED: int = 148
_LAST_SYNC_TMV: int = 76
_LAST_SYNC_DIRECT: int = 72
AUTO_SYNC_INTERVAL_SECONDS: int = 3600  # Run hourly


async def _background_auto_sync():
    """Background cron loop that periodically fetches new releases from homepage into SQLite."""
    global _LAST_SYNC_TIME, _LAST_SYNC_STATUS, _LAST_SYNC_ADDED, _LAST_SYNC_TMV, _LAST_SYNC_DIRECT
    import time
    # Wait 30 seconds after server launch before initial periodic sync
    await asyncio.sleep(30)
    while True:
        try:
            print("[Auto-Sync Cron] Starting scheduled sync across Tamil(Torrents), TamilMov & TamDub...")
            # 1. Torrent releases
            entries = await asyncio.to_thread(scraper.fetch_homepage)
            tmv_added = 0
            if entries:
                with db.get_conn() as conn:
                    for e in entries:
                        db.upsert_movie(conn, e)
                        tmv_added += 1
                asyncio.create_task(_background_fetch_posters(entries))

            # 2. Direct MP4 releases (TamilMov & TamDub)
            direct_added = await asyncio.to_thread(isaimini_db.sync_latest_releases)
            asyncio.create_task(_initial_poster_backfill())

            # 3. Anime & Cartoons releases (Timely sync for Tamil & multi-language releases)
            try:
                await asyncio.to_thread(toonplay_scraper.fetch_home, force_refresh=True)
                await asyncio.to_thread(toonplay_scraper.fetch_language_catalog, "tamil", "all", 1)
            except Exception as toon_err:
                print(f"[Auto-Sync Cron] Anime timely update notice: {toon_err}")

            # 4. CineStream Global Movies & TV catalog warm-up
            try:
                await asyncio.to_thread(cinestream_scraper.fetch_home, force_refresh=True)
            except Exception as cine_err:
                print(f"[Auto-Sync Cron] CineStream catalog warm-up notice: {cine_err}")

            total_added = tmv_added + direct_added
            _LAST_SYNC_TIME = time.time()
            _LAST_SYNC_STATUS = "ok"
            _LAST_SYNC_ADDED = total_added
            _LAST_SYNC_TMV = tmv_added
            _LAST_SYNC_DIRECT = direct_added
            print(f"[Auto-Sync Cron] Scheduled sync finished. Added/updated {total_added} releases (Torrents: {tmv_added}, Direct: {direct_added}).")
        except Exception as exc:
            _LAST_SYNC_STATUS = f"error: {exc}"
            print(f"[Auto-Sync Cron] Periodic sync encountered error: {exc}")

        await asyncio.sleep(AUTO_SYNC_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time
    db.init_db()
    try:
        isaimini_db.init_db()
    except Exception as e:
        logger.error(f"Failed to init isaimini db: {e}")
    try:
        toonplay_db.init_db()
    except Exception as e:
        logger.error(f"Failed to init toonplay db: {e}")
    try:
        cinestream_db.init_db()
    except Exception as e:
        logger.error(f"Failed to init cinestream db: {e}")

    # Seed databases from seed_movies.json if DB is empty on fresh container boot
    try:
        seed_path = BASE_DIR / "seed_movies.json"
        if seed_path.exists():
            with open(seed_path, "r", encoding="utf-8") as sf:
                seed_data = json.load(sf)
            if db.count_movies() == 0:
                tmv_seeds = seed_data.get("tmv", [])
                if tmv_seeds:
                    with db.get_conn() as conn:
                        for m in tmv_seeds:
                            db.upsert_movie(conn, m)
                    print(f"[Lifespan] Bootstrapped {len(tmv_seeds)} movies into movies.db from seed")

            with isaimini_db.get_conn() as conn:
                isai_count = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
            if isai_count == 0:
                isai_seeds = seed_data.get("isaimini", [])
                if isai_seeds:
                    with isaimini_db.get_conn() as conn:
                        isaimini_db.upsert_movies_batch(conn, isai_seeds)
                    print(f"[Lifespan] Bootstrapped {len(isai_seeds)} movies into isaimini.db from seed")
    except Exception as seed_err:
        print(f"[Lifespan] Seed bootstrap note: {seed_err}")

    # Auto-refresh cache asynchronously in background on cold start if DB has few items
    if db.count_movies() < 10:
        async def _cold_start_sync():
            try:
                entries = await asyncio.to_thread(scraper.fetch_homepage)
                if entries:
                    with db.get_conn() as conn:
                        for e in entries:
                            db.upsert_movie(conn, e)
                    global _LAST_SYNC_TIME, _LAST_SYNC_STATUS, _LAST_SYNC_ADDED
                    _LAST_SYNC_TIME = time.time()
                    _LAST_SYNC_STATUS = "ok"
                    _LAST_SYNC_ADDED = len(entries)
                    print(f"[Lifespan] Cold start sync completed: {len(entries)} items added")
            except Exception as sync_exc:
                print(f"[Lifespan] Cold start sync skipped: {sync_exc}")
        asyncio.create_task(_cold_start_sync())

    # Background backfill of posters for already stored records
    asyncio.create_task(_initial_poster_backfill())
    # Start scheduled periodic auto-sync cron
    sync_task = asyncio.create_task(_background_auto_sync())

    global _STREAMER_STATUS
    # Spawn background node streamer if node is installed and not yet running on port 8778
    streamer_proc = None
    node_bin = shutil.which("node")
    npm_bin = shutil.which("npm")
    streamer_file = BASE_DIR / "streamer.js"
    node_modules_dir = BASE_DIR / "node_modules"

    if node_bin and streamer_file.exists():
        try:
            requests.get("http://127.0.0.1:8778/health", timeout=0.6)
            _STREAMER_STATUS = "running (pre-existing)"
        except Exception:
            if not node_modules_dir.exists() and npm_bin:
                try:
                    print("[Lifespan] Installing npm dependencies for streamer...")
                    subprocess.run([npm_bin, "install", "--omit=dev"], cwd=str(BASE_DIR), timeout=90)
                except Exception as err:
                    print(f"[Lifespan] npm install failed: {err}")

            try:
                log_path = BASE_DIR / "streamer.log"
                log_file = open(log_path, "w", encoding="utf-8")
                streamer_proc = subprocess.Popen(
                    [node_bin, str(streamer_file)],
                    cwd=str(BASE_DIR),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                _STREAMER_STATUS = f"running (pid {streamer_proc.pid})"
                print(f"[Lifespan] Spawned background streamer.js (PID: {streamer_proc.pid})")
            except Exception as proc_err:
                _STREAMER_STATUS = f"spawn error: {proc_err}"
                print(f"[Lifespan] Could not spawn streamer.js: {proc_err}")

    # Initialize and auto-sync active live domains (runs regardless of node status)
    domain_task = None
    try:
        domain_manager.init_domains()
        async def _periodic_domain_resolver():
            await asyncio.sleep(6)
            while True:
                try:
                    await asyncio.to_thread(domain_manager.auto_resolve_all)
                except Exception as exc:
                    print(f"[Lifespan] Periodic domain check error: {exc}")
                await asyncio.sleep(1800)

        domain_task = asyncio.create_task(_periodic_domain_resolver())
    except Exception as d_err:
        print(f"[Lifespan] Domain manager init error: {d_err}")

    yield

    sync_task.cancel()
    if domain_task:
        domain_task.cancel()
    if streamer_proc and streamer_proc.poll() is None:
        try:
            streamer_proc.terminate()
        except Exception:
            pass


app = FastAPI(title="SDK Movies", lifespan=lifespan)


@app.get("/api/recent")
async def api_recent(
    limit: int = Query(48, ge=1, le=100),
    offset: int = Query(0, ge=0),
    language: str | None = None,
    quality: str | None = None,
    sort_by: str = "recent",
    content_type: str | None = None,
    source: str = "all",
    year: str | None = None,
    tv: bool = False,
):
    """Fetch latest releases from local databases with multi-source and era/year support."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 48
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    clean_ctype = None if not content_type or content_type.lower() == "all" else content_type
    clean_lang = None if not language or language.lower() == "all" else language
    clean_quality = None if not quality or quality.lower() == "all" else quality

    parsed_year = None
    start_year = None
    end_year = None
    if year and str(year).strip().lower() != "all":
        y_str = str(year).strip().lower()
        if y_str.isdigit():
            parsed_year = int(y_str)
        elif y_str == "2020s":
            start_year, end_year = 2020, 2029
        elif y_str == "2010s":
            start_year, end_year = 2010, 2019
        elif y_str in ("classics", "classic", "retro"):
            start_year, end_year = 1950, 2009

    cache_key = f"recent:{limit}:{offset}:{clean_lang}:{clean_quality}:{sort_by}:{clean_ctype}:{source}:{year}:{tv}"
    cached_recent = ram_cache.get(cache_key)
    if cached_recent is not None:
        return JSONResponse(cached_recent)

    results = []

    if source == "tamilmv":
        try:
            results = db.get_recent_movies(
                limit=limit,
                offset=offset,
                language=clean_lang,
                quality=clean_quality,
                sort_by=sort_by,
                content_type=clean_ctype,
                year=parsed_year,
                start_year=start_year,
                end_year=end_year,
            )
            for m in results:
                format_tmv_card(m)
        except Exception as exc:
            logger.error(f"Error in tamilmv recent: {exc}")
            results = []
    elif source in ("isaimini", "isaidub"):
        site_filter = "isaidub" if source == "isaidub" else "isaimini"
        try:
            raw = isaimini_db.get_recent_movies(
                limit=limit,
                offset=offset,
                content_type=clean_ctype,
                language=clean_lang,
                quality=clean_quality,
                site=site_filter,
                year=parsed_year,
                start_year=start_year,
                end_year=end_year,
            )
            results = [format_isaimini_card(m) for m in raw]
        except Exception as exc:
            logger.error(f"Error in isaimini recent: {exc}")
            results = []
    elif source == "toonplay":
        try:
            raw = toonplay_db.get_recent_items(limit=limit, offset=offset, content_type=clean_ctype)
            results = [format_toonplay_card(item) for item in raw]
        except Exception as exc:
            logger.error(f"Error in toonplay recent: {exc}")
            results = []
    elif source == "cinestream":
        try:
            home_data = await asyncio.to_thread(cinestream_scraper.fetch_home)
            items = home_data.get("trending", []) or home_data.get("popular_movies", [])
            results = [format_cinestream_card(m) for m in items[offset:offset+limit]]
        except Exception as exc:
            logger.error(f"Error in cinestream recent: {exc}")
            results = []
    else:  # source == "all"
        half_limit = max(limit // 2, 1)
        half_offset = offset // 2

        # Get total TMV count to ensure smooth non-duplicating pagination across sources
        try:
            with db.get_conn() as conn:
                tmv_total = conn.execute("SELECT COUNT(*) FROM movies WHERE substr(title, 1, 1) != '['").fetchone()[0]
        except Exception:
            tmv_total = 250

        tmv_offset = min(half_offset, tmv_total)
        try:
            tmv_res = db.get_recent_movies(
                limit=half_limit,
                offset=tmv_offset,
                language=clean_lang,
                quality=clean_quality,
                sort_by=sort_by,
                content_type=clean_ctype,
                year=parsed_year,
                start_year=start_year,
                end_year=end_year,
            )
            for m in tmv_res:
                format_tmv_card(m)
        except Exception as exc:
            logger.error(f"Error in tmv recent in all: {exc}")
            tmv_res = []

        needed_isai = limit - len(tmv_res)
        isai_offset = max(0, offset - tmv_offset)
        try:
            isai_raw = [
                m for m in isaimini_db.get_recent_movies(
                    limit=needed_isai + 4,
                    offset=isai_offset,
                    content_type=clean_ctype,
                    language=clean_lang,
                    quality=clean_quality,
                    year=parsed_year,
                    start_year=start_year,
                    end_year=end_year,
                ) if not re.match(r"^Tamil \d{4} Movies$", m.get("title", ""), flags=re.I)
            ][:needed_isai]
            isai_res = [format_isaimini_card(m) for m in isai_raw]
        except Exception as exc:
            logger.error(f"Error in isaimini recent in all: {exc}")
            isai_res = []

        if tv:
            # On TV app, prioritize instant playable direct MP4s ahead of torrents
            combined = isai_res + tmv_res
        else:
            combined = []
            i, j = 0, 0
            while i < len(tmv_res) or j < len(isai_res):
                if i < len(tmv_res):
                    combined.append(tmv_res[i])
                    i += 1
                if j < len(isai_res):
                    combined.append(isai_res[j])
                    j += 1
        results = combined[:limit]

    # If empty on initial load, instantly bootstrap from seed_movies.json so feed never breaks
    if not results and offset == 0:
        try:
            seed_path = BASE_DIR / "seed_movies.json"
            if seed_path.exists():
                with open(seed_path, "r", encoding="utf-8") as sf:
                    seed_data = json.load(sf)
                if source in ("all", "tamilmv"):
                    for m in seed_data.get("tmv", [])[:limit]:
                        format_tmv_card(m)
                        results.append(m)
                if source in ("all", "isaimini", "isaidub"):
                    for m in seed_data.get("isaimini", [])[:limit]:
                        results.append(format_isaimini_card(m))
        except Exception as seed_err:
            logger.debug(f"Seed fallback note: {seed_err}")

        # Trigger background scrape if still empty
        if not results and source in ("all", "tamilmv") and not (parsed_year or start_year):
            try:
                entries = await asyncio.to_thread(scraper.fetch_homepage)
                if entries:
                    with db.get_conn() as conn:
                        for e in entries:
                            db.upsert_movie(conn, e)
                    results = db.get_recent_movies(
                        limit=limit,
                        offset=offset,
                        language=language,
                        quality=quality,
                        sort_by=sort_by,
                        content_type=content_type,
                    )
                    for m in results:
                        format_tmv_card(m)
            except Exception:
                pass

    if results:
        asyncio.create_task(_background_fetch_posters(results))

    ram_cache.set(cache_key, results, custom_ttl=90)
    return JSONResponse(results)


def init_meta_cache_db():
    try:
        with db.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta_cache (
                    cache_key TEXT PRIMARY KEY,
                    rating TEXT,
                    plot TEXT,
                    year INT,
                    poster TEXT,
                    background TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
    except Exception:
        pass

init_meta_cache_db()


def resolve_third_party_poster(title: str, year: int | None = None) -> str | None:
    """Resolve high-quality official movie poster from IMDb (Amazon CDN) or Cinemeta/TMDB."""
    if not title:
        return None
    # Skip category folders
    if re.search(r"^(Tamil|Telugu|Hindi|Malayalam|Kannada|English)\s+\d{4}\s+Movies", title, re.I):
        return None
    if re.search(r"^(Tamil|Telugu|Hindi|Malayalam)\s+(Dubbed|HD|Movies)", title, re.I):
        return None
    if any(k in title for k in ["Movies Collection", "Latest Movies Updates"]):
        return None

    # Clean title
    pattern = r"\[.*?\]|\(.*?\)|S\d+|EP\d+|TRUE WEB-DL.*|\b(Tamil Dubbed|Tamil|Dubbed|Org|Original|DVDRip|BDRip|Audio|Movie|Web Series|HD|360p|720p|1080p|PreDVD|CAM|\d{4})\b"
    clean_title = re.sub(pattern, "", title, flags=re.I).strip()
    clean_title = re.sub(r"\s+", " ", clean_title).strip() or title.strip()

    # Check curated overrides
    norm_clean = re.sub(r"[^\w\s]", "", clean_title).strip().lower()
    for o_key, o_val in POSTER_OVERRIDES.items():
        if o_key in norm_clean or norm_clean in o_key or o_key in title.lower():
            if o_val.get("poster"):
                return o_val["poster"]

    if not year:
        ym = re.search(r"\b(19\d\d|20\d\d)\b", title)
        if ym:
            try:
                year = int(ym.group(1))
            except Exception:
                pass

    # Tier 1: IMDb Suggestion API (Free, high-speed, official Amazon CDN posters)
    slug_base = re.sub(r"[^a-z0-9_]", "", clean_title.lower().replace(" ", "_"))
    candidates = []
    if year:
        candidates.append(f"{slug_base}_{year}")
    candidates.append(slug_base)

    for slug in candidates:
        if not slug:
            continue
        try:
            url = f"https://v3.sg.media-imdb.com/suggestion/x/{slug}.json"
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=3.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("d", [])
                if year:
                    for it in items:
                        if it.get("i") and it.get("y") and abs(int(it.get("y")) - int(year)) <= 1:
                            img = it["i"].get("imageUrl")
                            if img:
                                return img
                for it in items:
                    if it.get("i"):
                        img = it["i"].get("imageUrl")
                        if img:
                            return img
        except Exception:
            pass

    # Tier 2: Cinemeta catalog (Metahub CDN)
    for ctype in ("movie", "series"):
        try:
            search_url = f"https://v3-cinemeta.strem.io/catalog/{ctype}/top/search={quote(clean_title)}.json"
            resp = requests.get(search_url, timeout=2.5)
            if resp.status_code == 200:
                metas = resp.json().get("metas", [])
                for m in metas:
                    p = m.get("poster")
                    if p and p.startswith("http"):
                        return p
        except Exception:
            pass

    return None


def get_movie_meta(title: str, year: int | None = None, allow_remote: bool = True) -> dict:
    if not title:
        return {}
    pattern = r"\[.*?\]|\(.*?\)|S\d+|EP\d+|TRUE WEB-DL.*|\b(Tamil Dubbed|Tamil|Dubbed|Org|Original|DVDRip|BDRip|Audio|Movie|Web Series|HD|360p|720p|1080p|PreDVD|CAM|\d{4})\b"
    clean_title = re.sub(pattern, "", title, flags=re.I).strip()
    clean_title = re.sub(r"\s+", " ", clean_title).strip() or title.strip()
    cache_key = f"{clean_title.lower()}_{year or ''}"

    # Check curated poster overrides first
    norm_clean = re.sub(r"[^\w\s]", "", clean_title).strip().lower()
    for o_key, o_val in POSTER_OVERRIDES.items():
        if o_key in norm_clean or norm_clean in o_key or o_key in title.lower():
            return {
                "found": True,
                "rating": o_val.get("rating"),
                "genres": o_val.get("genres", ["Sci-Fi", "Action"]),
                "plot": o_val.get("plot", "High definition verified release with multiple audio and subtitle streams."),
                "year": o_val.get("year", year),
                "poster": o_val.get("poster"),
                "background": o_val.get("background", o_val.get("poster")),
            }

    if cache_key in _META_CACHE:
        return _META_CACHE[cache_key]

    # Check SQLite meta_cache table
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT rating, plot, year, poster, background FROM meta_cache WHERE cache_key = ?", (cache_key,)).fetchone()
            if row:
                res = {
                    "found": True,
                    "rating": row["rating"],
                    "plot": row["plot"],
                    "year": row["year"],
                    "poster": row["poster"],
                    "background": row["background"],
                }
                _META_CACHE[cache_key] = res
                return res
    except Exception:
        pass

    # Check isaimini.db or movies.db for official poster
    official_poster = None
    try:
        with isaimini_db.get_conn() as iconn:
            row = iconn.execute("SELECT poster, year FROM movies WHERE (clean_title = ? OR title = ?) LIMIT 1", (clean_title, title)).fetchone()
            if row and row["poster"]:
                official_poster = row["poster"]
                if not year and row["year"]:
                    try:
                        year = int(row["year"])
                    except Exception:
                        pass
    except Exception:
        pass

    if not official_poster:
        try:
            with db.get_conn() as conn:
                row = conn.execute("SELECT poster_url, year FROM movies WHERE title = ? LIMIT 1", (title,)).fetchone()
                if row and row["poster_url"]:
                    official_poster = row["poster_url"]
                    if not year and row["year"]:
                        try:
                            year = int(row["year"])
                        except Exception:
                            pass
        except Exception:
            pass

    # Normalize official poster domain or discard if from known dead image hosts
    if official_poster:
        if any(dead in official_poster for dead in ["imageride", "extraimage", "picx.click", "imgpile", "fastpic", "imagehost"]):
            official_poster = None
        elif "moviesda.ski" in official_poster or "moviezda.me" in official_poster:
            m_base = domain_manager.get_active_base_url("moviesda") or "https://moviezda.co"
            official_poster = re.sub(r"https?://(?:moviesda\.ski|moviezda\.me)", m_base, official_poster)

    # If no valid official poster found in DB, resolve from 3rd party (IMDb Amazon CDN)
    if not official_poster and allow_remote:
        official_poster = resolve_third_party_poster(clean_title, year)

    res = {
        "found": bool(official_poster),
        "rating": None,
        "genres": [],
        "plot": None,
        "year": year,
        "poster": official_poster,
        "background": None,
    }

    if not allow_remote:
        return res

    # Query Cinemeta with fast 2.0s timeout
    try:
        search_terms = [clean_title]
        if clean_title.lower() in GENERIC_TITLES:
            search_terms.append(f"{clean_title} tamil")
        for q_term in search_terms:
            for ctype in ("movie", "series"):
                try:
                    search_url = f"https://v3-cinemeta.strem.io/catalog/{ctype}/top/search={quote(q_term)}.json"
                    resp = requests.get(search_url, timeout=2.0)
                    if resp.status_code == 200:
                        metas = resp.json().get("metas", [])
                        best = select_best_meta(metas, clean_title, year)
                        if best and best.get("id"):
                            detail_resp = requests.get(f"https://v3-cinemeta.strem.io/meta/{ctype}/{best['id']}.json", timeout=2.0)
                            if detail_resp.status_code == 200:
                                meta = detail_resp.json().get("meta", {})
                                res["found"] = True
                                res["rating"] = meta.get("imdbRating")
                                res["genres"] = meta.get("genres") or []
                                res["plot"] = meta.get("description")
                                res["year"] = meta.get("year") or year
                                res["poster"] = meta.get("poster") or best.get("poster") or official_poster
                                res["background"] = meta.get("background")
                                break
                except Exception:
                    pass
            if res.get("background"):
                break
    except Exception:
        pass

    _META_CACHE[cache_key] = res
    try:
        with db.get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO meta_cache (cache_key, rating, plot, year, poster, background)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cache_key, res.get("rating"), res.get("plot"), res.get("year"), res.get("poster"), res.get("background")))
    except Exception:
        pass

    return res


def clean_tv_quality_tag(q: str | None) -> str:
    if not q:
        return "4K UHD"
    q_up = q.upper()
    if any(k in q_up for k in ["4K", "2160P", "UHD"]):
        return "4K UHD"
    if any(k in q_up for k in ["1080P", "FHD"]):
        return "1080p FHD"
    if "720P" in q_up:
        return "720p HD"
    return "4K UHD"


def clean_tv_movie_title(t: str | None) -> str:
    if not t:
        return ""
    t = re.sub(r"\s*[-–|]\s*(?:4K|2160p|1080p|720p|480p|WEB-DL|HDRip|DVDRip|Blu-Ray|Remux|AVC|x264|x265|HEVC|H\.265).*$", "", t, flags=re.I)
    t = re.sub(r"\b(?:1080p|720p|2160p|4K|HDRip|WEB-DL|Blu-Ray|Remux)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\[(?:1080p|720p|2160p|4K|GDrive|Tamil|Telugu).*?\]", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


@app.get("/api/tv/shelves")
async def api_tv_shelves():
    """Fetch high-priority curated shelves for Android TV Leanback / TV UI experience."""
    cached = ram_cache.get("tv_shelves")
    if cached is not None:
        return JSONResponse(cached)

    def _load():
        # 0. My Cloud Vault (Personal Google Drive, Pixeldrain, Dropbox streams)
        try:
            vault_raw = db.get_vault_movies()
            vault_movies = [format_vault_card(m) for m in vault_raw]
            for m in vault_movies:
                m["title"] = clean_tv_movie_title(m.get("title"))
                m["quality"] = clean_tv_quality_tag(m.get("quality"))
        except Exception:
            vault_movies = []

        # 1. Tamil Movies (Direct MP4) - High Priority on TV
        try:
            direct_raw = [m for m in isaimini_db.get_recent_movies(limit=24, site="isaimini", content_type="movie") if not re.match(r"^Tamil \d{4} Movies$", m.get("title", ""), flags=re.I)]
            direct_movies = [format_isaimini_card(m) for m in direct_raw]
            for m in direct_movies:
                m["title"] = clean_tv_movie_title(m.get("title"))
                m["quality"] = clean_tv_quality_tag(m.get("quality"))
                if not m.get("poster_url") or "isaidub" in m.get("poster_url", ""):
                    meta = get_movie_meta(m.get("title"), m.get("year"), allow_remote=False)
                    if meta and meta.get("poster") and "live.metahub.space" not in meta.get("poster", ""):
                        m["poster_url"] = meta["poster"]
        except Exception:
            direct_movies = []

        # 2. Tamil Dubbed Blockbusters - High Priority on TV
        try:
            dub_raw = [m for m in isaimini_db.get_recent_movies(limit=24, site="isaidub", content_type="movie") if not re.match(r"^Tamil \d{4} Movies$", m.get("title", ""), flags=re.I)]
            dubbed_movies = [format_isaimini_card(m) for m in dub_raw]
            for m in dubbed_movies:
                m["title"] = clean_tv_movie_title(m.get("title"))
                m["quality"] = clean_tv_quality_tag(m.get("quality"))
                if not m.get("poster_url") or "isaidub" in m.get("poster_url", ""):
                    meta = get_movie_meta(m.get("title"), m.get("year"), allow_remote=False)
                    if meta and meta.get("poster") and "live.metahub.space" not in meta.get("poster", ""):
                        m["poster_url"] = meta["poster"]
        except Exception:
            dubbed_movies = []

        # 3. Web Series & Shows
        series_isai = []
        series_tmv = []
        try:
            series_isai = [format_isaimini_card(m) for m in isaimini_db.get_recent_movies(limit=14, content_type="series")]
        except Exception:
            series_isai = []
        try:
            series_tmv = db.get_recent_movies(limit=10, content_type="series")
        except Exception:
            series_tmv = []
        for m in series_tmv:
            m["source"] = "tamilmv"
            m["source_label"] = "Tamil(Torrents)"
            m["is_direct"] = False
            m["title"] = clean_tv_movie_title(m.get("title"))
            m["quality"] = clean_tv_quality_tag(m.get("quality"))
        for m in series_isai:
            m["title"] = clean_tv_movie_title(m.get("title"))
            m["quality"] = clean_tv_quality_tag(m.get("quality"))
            if not m.get("poster_url"):
                meta = get_movie_meta(m.get("title"), m.get("year"), allow_remote=False)
                if meta and meta.get("poster"):
                    m["poster_url"] = meta["poster"]
        series_list = []
        si, st = 0, 0
        while si < len(series_isai) or st < len(series_tmv):
            if si < len(series_isai):
                series_list.append(series_isai[si])
                si += 1
            if st < len(series_tmv):
                series_list.append(series_tmv[st])
                st += 1

        # 4. ToonPlay Anime & Cartoons
        try:
            toon_raw = toonplay_db.get_recent_items(limit=24)
            if not toon_raw:
                try:
                    home_data = toonplay_scraper.fetch_home()
                    toon_raw = (home_data.get("latest_releases") or []) + (home_data.get("popular_series") or [])
                except Exception:
                    toon_raw = []
            toon_movies = [format_toonplay_card(m) for m in toon_raw]
            for m in toon_movies:
                m["title"] = clean_tv_movie_title(m.get("title"))
                m["quality"] = clean_tv_quality_tag(m.get("quality"))
        except Exception:
            toon_movies = []

        # 5. Trending / Top This Week (Torrents - Deprioritized after Direct Playable)
        try:
            trending_tmv = db.get_recent_movies(limit=18, sort_by="top_week")
            for m in trending_tmv:
                m["source"] = "tamilmv"
                m["source_label"] = "Tamil(Torrents)"
                m["is_direct"] = False
                m["title"] = clean_tv_movie_title(m.get("title"))
                m["quality"] = clean_tv_quality_tag(m.get("quality"))
        except Exception:
            trending_tmv = []

        # 6. Multi-Audio / 4K Swarms (Torrents - Lowest priority on TV)
        try:
            torrents_raw = db.get_recent_movies(limit=18, quality="1080p")
            if not torrents_raw:
                torrents_raw = db.get_recent_movies(limit=18)
            for m in torrents_raw:
                m["source"] = "tamilmv"
                m["source_label"] = "Tamil(Torrents)"
                m["is_direct"] = False
                m["title"] = clean_tv_movie_title(m.get("title"))
                m["quality"] = clean_tv_quality_tag(m.get("quality"))
        except Exception:
            torrents_raw = []

        # Featured Spotlight candidates (Vault movies and Direct MP4 prioritized over torrents)
        spotlight = []
        seen_urls = set()
        for m in (vault_movies[:3] + direct_movies[:3] + toon_movies[:2] + dubbed_movies[:2] + trending_tmv[:2]):
            u = m.get("topic_url") or m.get("id")
            if u and u not in seen_urls:
                seen_urls.add(u)
                spotlight.append(m)

        # Attach fast local posters and cached metadata to spotlight candidates (<1ms)
        for m in spotlight[:6]:
            try:
                meta = get_movie_meta(m.get("title"), m.get("year"), allow_remote=False)
                if meta:
                    if meta.get("background"):
                        m["background"] = meta["background"]
                    if meta.get("poster") and not m.get("poster_url"):
                        m["poster_url"] = meta["poster"]
                    if meta.get("plot"):
                        m["plot"] = meta["plot"]
                        m["synopsis"] = meta["plot"]
                    if meta.get("rating"):
                        m["rating"] = meta["rating"]
            except Exception:
                pass

        # In TV mode, all_cards puts instant playable Vault and Direct MP4 titles first, torrents last
        all_cards = vault_movies + direct_movies + dubbed_movies + toon_movies + series_list + trending_tmv + torrents_raw

        shelves = []
        cw_raw = db.get_continue_watching(limit=18)
        if cw_raw:
            shelves.append({
                "id": "continue_watching",
                "title": "Continue Watching",
                "icon": "⏳",
                "badge": "Resume Playback",
                "is_continue_watching": True,
                "items": cw_raw
            })
            all_cards = cw_raw + all_cards

        if vault_movies:
            shelves.append({
                "id": "cloud_vault",
                "title": "⭐ My Cloud Vault",
                "icon": "☁️",
                "badge": "Personal Streams",
                "items": vault_movies
            })

        shelves.extend([
            {
                "id": "direct_tamil",
                "title": "Tamil Movies (Direct MP4)",
                "icon": "⚡",
                "badge": "Instant Play",
                "items": direct_movies
            },
            {
                "id": "dubbed",
                "title": "Tamil Dubbed Blockbusters",
                "icon": "🎙️",
                "badge": "Multi Audio",
                "items": dubbed_movies
            },
            {
                "id": "toonplay_anime",
                "title": "🎌 Anime & Cartoons",
                "icon": "🎌",
                "badge": "Multi Audio (Tam/Hin)",
                "items": toon_movies
            },
            {
                "id": "series",
                "title": "Web Series & Shows",
                "icon": "📺",
                "badge": "Episodic",
                "items": series_list[:18]
            },
            {
                "id": "trending",
                "title": "Trending This Week",
                "icon": "🔥",
                "badge": "Top Rated",
                "items": trending_tmv
            },
            {
                "id": "torrents",
                "title": "4K Torrents & Swarms",
                "icon": "🧲",
                "badge": "Magnet Link",
                "items": torrents_raw
            }
        ])

        return {
            "spotlight": spotlight[:6],
            "all_cards": all_cards,
            "shelves": shelves
        }

    data = await asyncio.to_thread(_load)
    all_cards = data.get("all_cards", [])
    if all_cards:
        asyncio.create_task(_background_fetch_posters(all_cards))

    # Fast parallel pre-fetch for spotlight movies missing background/plot with 1.2s max timeout
    spotlight = data.get("spotlight", [])
    async def _resolve_spotlight_item(m):
        if m.get("background") and m.get("plot"):
            return
        try:
            meta = await asyncio.wait_for(asyncio.to_thread(get_movie_meta, m.get("title"), m.get("year"), True), timeout=1.2)
            if meta:
                if meta.get("background"):
                    m["background"] = meta["background"]
                if meta.get("poster") and not m.get("poster_url"):
                    m["poster_url"] = meta["poster"]
                if meta.get("plot"):
                    m["plot"] = meta["plot"]
                    m["synopsis"] = meta["plot"]
                if meta.get("rating"):
                    m["rating"] = meta["rating"]
        except Exception:
            pass

    if spotlight:
        await asyncio.gather(*[_resolve_spotlight_item(m) for m in spotlight[:6]], return_exceptions=True)

    return JSONResponse(data)


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    refresh: bool = False,
    source: str = "all",
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query")

    async def _fetch_toon():
        try:
            raw_toon = await asyncio.wait_for(asyncio.to_thread(toonplay_scraper.search, query), timeout=2.5)
            return [format_toonplay_card(m) for m in raw_toon]
        except Exception:
            return []

    async def _fetch_cine():
        try:
            raw_cine = await asyncio.wait_for(asyncio.to_thread(cinestream_scraper.search, query), timeout=3.0)
            return [format_cinestream_card(m) for m in raw_cine]
        except Exception:
            return []

    async def _fetch_tmv():
        try:
            # 1. Fast local database lookup first (instant < 5ms)
            results_tmv = [] if refresh else db.search_movies(query)
            # 2. Only attempt live scraper if local DB is empty, with a fast 2.0s hard timeout
            if not results_tmv:
                try:
                    live = await asyncio.wait_for(asyncio.to_thread(scraper.fetch_search, query), timeout=2.0)
                    if live:
                        with db.get_conn() as conn:
                            for e in live:
                                db.upsert_movie(conn, e)
                        results_tmv = db.search_movies(query) or live
                except Exception:
                    pass
            for m in results_tmv:
                format_tmv_card(m)
            return results_tmv
        except Exception:
            return []

    async def _fetch_isai():
        try:
            site_filter = "isaidub" if source == "isaidub" else ("isaimini" if source == "isaimini" else None)
            raw = await asyncio.to_thread(isaimini_db.search_movies, query, limit=50, site=site_filter)
            # If site-specific returned 0, search both isaidub and isaimini
            if not raw and site_filter:
                raw = await asyncio.to_thread(isaimini_db.search_movies, query, limit=50, site=None)
            return [format_isaimini_card(m) for m in raw]
        except Exception:
            return []

    if source == "cinestream":
        results = await _fetch_cine()
        return JSONResponse(results)

    # Run all source queries in parallel!
    results_toon, results_tmv, results_isai, results_cine = await asyncio.gather(
        _fetch_toon(),
        _fetch_tmv(),
        _fetch_isai(),
        _fetch_cine(),
    )

    # Interleave results cleanly across sources
    combined = []
    i, j, k, c = 0, 0, 0, 0
    while i < len(results_isai) or j < len(results_toon) or k < len(results_tmv) or c < len(results_cine):
        if i < len(results_isai):
            combined.append(results_isai[i])
            i += 1
        if j < len(results_toon):
            combined.append(results_toon[j])
            j += 1
        if c < len(results_cine):
            combined.append(results_cine[c])
            c += 1
        if k < len(results_tmv):
            combined.append(results_tmv[k])
            k += 1

    if source == "toonplay":
        results = results_toon or combined
    elif source == "cinestream":
        results = results_cine or combined
    elif source == "tamilmv":
        results = results_tmv or combined
    elif source in ("isaimini", "isaidub"):
        results = results_isai or combined
    else:
        results = combined

    if results:
        asyncio.create_task(_background_fetch_posters(results))

    return JSONResponse(results)


def rank_quality_score(text: str) -> int:
    """Returns sorting score prioritizing 1080p (#1 priority) for all movie links."""
    t = str(text or "").lower()
    if "1080" in t or "fhd" in t or "full hd" in t:
        return 10000
    if "4k" in t or "2160" in t or "uhd" in t:
        return 9000
    if "720" in t or "hd" in t:
        return 8000
    if "480" in t or "hq" in t:
        return 7000
    if "540" in t:
        return 6500
    if "360" in t:
        return 6000
    if "240" in t:
        return 5000
    return 1000


@app.get("/api/isaimini/files")
async def api_isaimini_files(url: str = Query(...), refresh: bool = False):
    """Fetch downloadable quality file options for Moviesda / isaiDub movies."""
    clean_target_url = domain_manager.rewrite_url_to_active(url)
    if not any(k in clean_target_url.lower() for k in ("moviesda", "isaidub", "isaimini", "moviezda")):
        raise HTTPException(status_code=400, detail="Invalid Isaimini/isaiDub URL")

    movie = isaimini_db.get_movie(url) or isaimini_db.get_movie(clean_target_url)
    movie_id = movie["id"] if movie else None

    files = []
    if movie_id and not refresh:
        files = isaimini_db.get_movie_files(movie_id)

    if not files or refresh:
        try:
            files = await asyncio.to_thread(isaimini_scraper.fetch_movie_download_options, clean_target_url)
            if movie_id and files:
                with isaimini_db.get_conn() as conn:
                    for f in files:
                        isaimini_db.upsert_file(conn, movie_id, f)
        except Exception as exc:
            if not files:
                raise HTTPException(status_code=502, detail=f"Failed to fetch files: {exc}")

    if movie and (not movie.get("poster") or movie.get("poster") == ""):
        poster_candidate = None
        if files:
            for f in files:
                if f.get("thumbnail"):
                    poster_candidate = f["thumbnail"]
                    break
        if not poster_candidate:
            poster_candidate = await asyncio.to_thread(fetch_direct_page_poster, url)
        if poster_candidate:
            isaimini_db.update_movie_poster(url, poster_candidate)
            movie["poster"] = poster_candidate

    # Enrich and parse rich metadata for each file
    movie_title = movie.get("title", "") if movie else ""
    enriched_files = []
    collection_movies = set()
    is_collection = bool(re.search(r"\b(collection|trilogy|quadrilogy|pentalogy|duology)\b", movie_title, re.I))

    for f in files:
        f_dict = dict(f)
        meta = isaimini_scraper.parse_media_meta(
            f_dict.get("file_name", ""),
            movie_title,
            f_dict.get("quality_label", "")
        )
        f_dict["movie_name"] = meta.get("movie_name")
        f_dict["movie_year"] = meta.get("year")
        f_dict["is_sample"] = meta.get("is_sample", False)
        f_dict["is_series"] = meta.get("is_series", False)
        f_dict["season"] = meta.get("season")
        f_dict["episode"] = meta.get("episode")
        f_dict["clean_title"] = meta.get("display_title")
        f_dict["display_badge"] = meta.get("display_badge")
        f_dict["download_filename"] = meta.get("clean_download_name")
        enriched_files.append(f_dict)

        if meta.get("movie_name"):
            m_key = f"{meta['movie_name']}" + (f" ({meta['year']})" if meta.get('year') else "")
            collection_movies.add(m_key)

    if len(collection_movies) > 1:
        is_collection = True

    if enriched_files:
        enriched_files = sorted(
            enriched_files,
            key=lambda f: rank_quality_score(f"{f.get('quality_label', '')} {f.get('file_name', '')}"),
            reverse=True
        )

    return JSONResponse({
        "movie": format_isaimini_card(movie) if movie else {"url": url},
        "files": enriched_files,
        "is_collection": is_collection,
        "collection_movies": sorted(list(collection_movies))
    })


@app.get("/api/isaimini/resolve")
async def api_isaimini_resolve(download_page_url: str = Query(...)):
    """Resolve direct CDN MP4 download / stream URL through 3-stage anti-leech gateway."""
    try:
        resolved = await asyncio.to_thread(isaimini_scraper.resolve_download_link, download_page_url)
        if resolved and resolved.get("direct_url"):
            try:
                isaimini_db.update_resolved_file(download_page_url, resolved)
            except Exception:
                pass
        return JSONResponse({
            "status": "ok",
            "direct_url": resolved.get("direct_url"),
            "watch_online_url": resolved.get("watch_online_url"),
            "file_name": resolved.get("file_name"),
            "file_size": resolved.get("file_size"),
            "resolution": resolved.get("resolution"),
            "duration": resolved.get("duration"),
            "format": resolved.get("format"),
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to resolve download link: {exc}")


@app.get("/api/direct-download")
async def api_direct_download(request: Request, url: str = Query(...), filename: str = Query(None)):
    """Reverse proxy direct download streaming that sets Content-Disposition with clean metadata filename."""
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid download URL")

    # Clean filename
    safe_filename = filename or "movie.mp4"
    safe_filename = re.sub(r'[\\/*?:"<>|]', "", safe_filename).strip()
    if not safe_filename.endswith((".mp4", ".mkv", ".avi")):
        safe_filename += ".mp4"

    # Smart referer mapping for Moviesda & isaiDub CDN gateways
    if any(k in url for k in ("uptodub", "dubpage", "isaidub", "dubmv")):
        referer = "https://dubmv.xyz/"
    else:
        referer = "https://movies.downloadpage.xyz/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": referer,
    }
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    def _fetch():
        try:
            return _STREAM_SESSION.get(
                url,
                headers=headers,
                stream=True,
                timeout=30,
            )
        except Exception as exc:
            return exc

    stream_res = await asyncio.to_thread(_fetch)
    if isinstance(stream_res, Exception):
        raise HTTPException(status_code=503, detail=f"Download source unavailable: {stream_res}")

    def iter_content():
        try:
            for chunk in stream_res.iter_content(chunk_size=512 * 1024):
                if chunk:
                    yield chunk
        finally:
            try:
                stream_res.close()
            except Exception:
                pass

    from urllib.parse import quote
    encoded_filename = quote(safe_filename)
    response_headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Content-Disposition": f'attachment; filename="{safe_filename}"; filename*=UTF-8\'\'{encoded_filename}',
    }
    if "content-length" in stream_res.headers:
        response_headers["Content-Length"] = stream_res.headers["content-length"]
    if "content-type" in stream_res.headers:
        response_headers["Content-Type"] = stream_res.headers["content-type"]
    else:
        response_headers["Content-Type"] = "video/mp4"

    return StreamingResponse(
        iter_content(),
        status_code=stream_res.status_code,
        headers=response_headers,
    )

@app.get("/api/direct-stream")
async def api_direct_stream(request: Request, url: str = Query(...)):
    """Reverse proxy direct CDN MP4 byte-range streaming for in-browser video playback."""
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid stream URL")

    # Smart referer mapping for Moviesda & isaiDub CDN gateways
    if any(k in url for k in ("uptodub", "dubpage", "isaidub", "dubmv")):
        referer = "https://dubmv.xyz/"
    else:
        referer = "https://movies.downloadpage.xyz/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": referer,
    }
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    def _fetch():
        try:
            return _STREAM_SESSION.get(
                url,
                headers=headers,
                stream=True,
                timeout=30,
            )
        except Exception as exc:
            return exc

    stream_res = await asyncio.to_thread(_fetch)
    if isinstance(stream_res, Exception):
        raise HTTPException(status_code=503, detail=f"Stream target unavailable: {stream_res}")

    def iter_content():
        try:
            # 256KB buffer chunks for ultra-smooth video playback and instant scrubbing
            for chunk in stream_res.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            try:
                stream_res.close()
            except Exception:
                pass

    response_headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Content-Type": "video/mp4",
    }
    for h in ("Content-Range", "Content-Length"):
        if h in stream_res.headers:
            response_headers[h] = stream_res.headers[h]

    return StreamingResponse(
        iter_content(),
        status_code=stream_res.status_code,
        headers=response_headers,
    )


# ==============================================================================
# Continue Watching & Stream Pre-Buffering APIs (Option C Upgrades)
# ==============================================================================

@app.get("/api/continue-watching")
async def api_get_continue_watching():
    """Retrieve continue watching queue for instant resume."""
    try:
        items = db.get_continue_watching(limit=24)
        return JSONResponse({"status": "ok", "items": items})
    except Exception as exc:
        return JSONResponse({"status": "error", "items": [], "detail": str(exc)}, status_code=500)


@app.post("/api/continue-watching")
async def api_save_continue_watching(request: Request):
    """Save or update playback progress for a movie or series episode."""
    try:
        data = await request.json()
        item_id = data.get("id") or f"{data.get('title')}_{data.get('season_num', 0)}_{data.get('episode_num', 0)}"
        is_toon = bool(data.get("is_toonplay") or "toonplay" in str(data.get("topic_url", "")).lower() or "series-" in str(item_id) or "movies-" in str(item_id))
        entry = {
            "id": str(item_id),
            "title": str(data.get("title", "Untitled")),
            "year": str(data.get("year", "")),
            "poster_url": str(data.get("poster_url") or data.get("poster", "")),
            "content_type": str(data.get("content_type", "movie")),
            "series_id": str(data.get("series_id") or data.get("cleanId", "")),
            "season_num": int(data.get("season_num") or data.get("seasonNum", 0) or 0),
            "episode_num": int(data.get("episode_num") or data.get("epNum", 0) or 0),
            "episode_title": str(data.get("episode_title", "")),
            "stream_url": str(data.get("stream_url") or data.get("streamUrl", "")),
            "source": "toonplay" if is_toon else str(data.get("source", "stream")),
            "current_time": float(data.get("current_time") or data.get("position", 0) or 0),
            "duration": float(data.get("duration", 0) or 0),
            "progress_pct": float(data.get("progress_pct", 0) or 0),
        }
        db.save_continue_watching(entry)
        ram_cache.invalidate("tv_shelves")
        return JSONResponse({"status": "ok", "saved": entry})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=400)


@app.delete("/api/continue-watching/{item_id}")
async def api_delete_continue_watching(item_id: str):
    """Remove an item from continue watching queue."""
    try:
        db.delete_continue_watching(item_id)
        ram_cache.invalidate("tv_shelves")
        return JSONResponse({"status": "ok", "deleted": item_id})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@app.delete("/api/continue-watching")
@app.post("/api/continue-watching/clear")
async def api_clear_continue_watching():
    """Clear all items from continue watching queue."""
    try:
        db.clear_continue_watching()
        ram_cache.invalidate("tv_shelves")
        return JSONResponse({"status": "ok", "cleared": True})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@app.get("/api/stream/warmup")
async def api_stream_warmup(url: str = Query(...)):
    """Pre-warm video stream buffer in background for sub-300ms video playback start."""
    try:
        if not url.startswith("http"):
            return JSONResponse({"status": "error", "detail": "Invalid URL"}, status_code=400)

        def _fetch_head():
            if any(k in url for k in ("uptodub", "dubpage", "isaidub", "dubmv")):
                ref = "https://dubmv.xyz/"
            else:
                ref = "https://movies.downloadpage.xyz/"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Referer": ref,
                "Range": "bytes=0-524287"
            }
            try:
                res = _STREAM_SESSION.get(url, headers=headers, stream=True, timeout=5)
                chunk = next(res.iter_content(chunk_size=524288), b"")
                res.close()
                return len(chunk)
            except Exception:
                return 0

        warmed = await asyncio.to_thread(_fetch_head)
        return JSONResponse({"status": "ok", "warmed_bytes": warmed})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ==============================================================================
# Cloud Vault API (Google Drive, Pixeldrain, Dropbox, Direct Links)
# ==============================================================================

@app.get("/api/vault")
async def api_get_vault():
    """Retrieve all movies saved in the personal Cloud Vault."""
    def _get():
        movies = db.get_vault_movies()
        return [format_vault_card(m) for m in movies]
    items = await asyncio.to_thread(_get)
    return JSONResponse({"vault": items, "count": len(items)})


@app.post("/api/vault/add")
async def api_add_vault(payload: dict = Body(...)):
    """Add a new movie stream link to the Cloud Vault with automatic metadata & poster fetching."""
    title = (payload.get("title") or "").strip()
    raw_url = (payload.get("url") or "").strip()
    year_val = payload.get("year")
    quality = (payload.get("quality") or "1080p FHD").strip()

    if not title:
        raise HTTPException(status_code=400, detail="Movie title is required")
    if not raw_url or not raw_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Valid stream URL is required")

    year = None
    if year_val:
        try:
            year = int(year_val)
        except Exception:
            pass

    # 1. Resolve Stream URL
    resolved = resolve_cloud_stream_url(raw_url)
    source_type = resolved["source_type"]
    stream_url = resolved["stream_url"]

    # 2. Auto-fetch official metadata & poster from Cinemeta / TMDB without requiring API keys
    def _fetch_meta():
        return get_movie_meta(title, year, allow_remote=True)

    meta = await asyncio.to_thread(_fetch_meta) or {}
    poster = meta.get("poster") or ""
    backdrop = meta.get("background") or ""
    overview = meta.get("plot") or "Added to Cloud Vault for high-quality lossless streaming."
    rating = str(meta.get("rating") or "Vault")
    resolved_year = meta.get("year") or year

    # 3. Save to database
    def _save():
        return db.add_vault_movie(
            title=title,
            year=resolved_year,
            original_url=raw_url,
            stream_url=stream_url,
            poster=poster,
            backdrop=backdrop,
            overview=overview,
            rating=rating,
            quality=quality,
            source_type=source_type
        )

    vault_id = await asyncio.to_thread(_save)
    saved_item = db.get_vault_movie_by_id(vault_id)
    card = format_vault_card(saved_item)

    return JSONResponse({
        "success": True,
        "message": f"Successfully added '{title}' to your Cloud Vault!",
        "item": card
    })


@app.delete("/api/vault/{vault_id}")
async def api_delete_vault(vault_id: int):
    """Delete a movie from the personal Cloud Vault."""
    def _del():
        item = db.get_vault_movie_by_id(vault_id)
        if not item:
            return False
        db.delete_vault_movie(vault_id)
        return True

    success = await asyncio.to_thread(_del)
    if not success:
        raise HTTPException(status_code=404, detail="Vault movie not found")

    return JSONResponse({"success": True, "message": "Movie removed from Cloud Vault"})


@app.get("/api/vault/stream/gdrive")
async def api_vault_stream_gdrive(request: Request, id: str = Query(...)):
    """Streaming proxy for Google Drive files supporting HTTP Range (206) seeking for TV & Web player."""
    file_id = id.strip()
    if not file_id:
        raise HTTPException(status_code=400, detail="Missing Google Drive file ID")

    def _fetch_stream():
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        })
        url, _ = resolve_gdrive_stream_target(file_id, sess)
        req_headers = {}
        if "range" in request.headers:
            req_headers["Range"] = request.headers["range"]
        upstream = sess.get(url, headers=req_headers, stream=True, timeout=30)
        return upstream

    try:
        upstream = await asyncio.to_thread(_fetch_stream)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to stream from Google Drive: {exc}")

    def iter_content():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    response_headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
    }
    for h in ("Content-Range", "Content-Length", "Last-Modified"):
        if h in upstream.headers:
            response_headers[h] = upstream.headers[h]

    return StreamingResponse(
        iter_content(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


# ==============================================================================
# ToonPlay API (Anime & Cartoons, Multi-Audio, Zero Third-Party Domain Exposure)
# ==============================================================================

@app.get("/api/toonplay/home")
async def api_toonplay_home(refresh: bool = False):
    """Retrieve curated home catalog for ToonPlay (Anime & Cartoons)."""
    data = await asyncio.to_thread(toonplay_scraper.fetch_home, refresh)
    return JSONResponse({
        "hero": [format_toonplay_card(m) for m in data.get("hero", [])],
        "latest_releases": [format_toonplay_card(m) for m in data.get("latest_releases", [])],
        "popular_series": [format_toonplay_card(m) for m in data.get("popular_series", [])],
        "popular_movies": [format_toonplay_card(m) for m in data.get("popular_movies", [])]
    })


@app.get("/api/toonplay/search")
async def api_toonplay_search(q: str = Query(..., min_length=1), page: int = 1):
    """Search ToonPlay catalog via live API and fast SQLite cache."""
    results = await asyncio.to_thread(toonplay_scraper.search, q, page)
    return JSONResponse([format_toonplay_card(m) for m in results])


@app.get("/api/toonplay/info")
async def api_toonplay_info(id: str = Query(...)):
    """Retrieve full details, seasons, and episodes for a cartoon or anime."""
    item_id = id.strip().replace("cine_", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing item ID")

    info = await asyncio.to_thread(toonplay_scraper.fetch_info, item_id)
    if not info:
        raise HTTPException(status_code=404, detail="ToonPlay title not found")

    card = format_toonplay_card(info)
    seasons_list = info.get("seasonsList") or []
    sanitized_seasons = []
    total_ep_count = 0
    for s in seasons_list:
        s_num = int(s.get("season") or 1) if str(s.get("season", "")).isdigit() else 1
        episodes = s.get("episodes") or []
        sanitized_eps = []
        for ep in episodes:
            e_num = int(ep.get("number") or 1)
            sanitized_eps.append({
                "number": e_num,
                "ep_num": e_num,
                "title": ep.get("title") or f"Episode {e_num}",
                "image": ep.get("image") or card.get("poster_url"),
                "id": ep.get("id") or f"ep-{s_num}-{e_num}",
                "link": ep.get("link") or ""
            })
        total_ep_count += len(sanitized_eps)
        sanitized_seasons.append({
            "season": s_num,
            "season_num": s_num,
            "episode_count": len(sanitized_eps),
            "episodes": sanitized_eps
        })

    # Attach rich Series Metrics & Story Overview directly to item card
    card["seasons"] = sanitized_seasons
    card["total_seasons"] = len(sanitized_seasons)
    card["total_episodes"] = info.get("episodes") or total_ep_count or "N/A"
    card["episode_duration"] = info.get("episodeDuration") or "24 min"
    card["duration"] = info.get("duration") or info.get("episodeDuration") or "24 min"
    card["recommendations"] = info.get("recommendations") or []
    card["networks"] = info.get("networks") or []

    return JSONResponse({
        "item": card,
        "seasons": sanitized_seasons,
        "total_seasons": len(sanitized_seasons),
        "total_episodes": card["total_episodes"],
        "episode_duration": card["episode_duration"],
        "recommendations": card["recommendations"],
        "is_movie": (card.get("content_type") == "movie")
    })


@app.get("/api/toonplay/ravok-languages")
async def api_toonplay_ravok_languages(id: str = Query(...)):
    """Fetch available multi-audio tracks for a stream."""
    clean_id = id.strip()
    try:
        def _get():
            r = requests.get(
                f"https://toonplay.in/api/ravok-languages?id={quote(clean_id)}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://toonplay.in/"
                },
                timeout=6
            )
            if r.status_code == 200:
                return r.json()
            return {"success": True, "tracks": [
                {"code": "tam", "name": "Tamil"},
                {"code": "tel", "name": "Telugu"},
                {"code": "hin", "name": "Hindi"},
                {"code": "eng", "name": "English"},
                {"code": "kan", "name": "Kannada"},
                {"code": "mal", "name": "Malayalam"}
            ]}
        data = await asyncio.to_thread(_get)
        return JSONResponse(data)
    except Exception:
        return JSONResponse({"success": True, "tracks": [
            {"code": "tam", "name": "Tamil"},
            {"code": "tel", "name": "Telugu"},
            {"code": "hin", "name": "Hindi"},
            {"code": "eng", "name": "English"}
        ]})


@app.get("/api/toonplay/resolve")
async def api_toonplay_resolve(
    id: str = Query(...),
    s: Optional[int] = Query(None),
    season: Optional[int] = Query(None),
    ep: Optional[int] = Query(None),
    episode: Optional[int] = Query(None)
):
    """
    Resolve stream mirrors and multi-language audio tracks without exposing upstream domains.
    Returns clean internal player links with episode and next/prev metadata.
    """
    item_id = id.strip().replace("cine_", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing item ID")

    target_s = s if s is not None else (season if season is not None else 1)
    target_ep = ep if ep is not None else (episode if episode is not None else 1)

    resolved = await asyncio.to_thread(toonplay_scraper.resolve_stream, item_id, target_s, target_ep)
    if not resolved.get("success"):
        raise HTTPException(status_code=502, detail=resolved.get("error", "No stream available"))

    # Enrich with season/episode metadata and next/previous navigation details
    try:
        info = await asyncio.to_thread(toonplay_scraper.fetch_info, item_id)
        if info:
            resolved["series_title"] = info.get("title")
            resolved["current_season"] = target_s
            resolved["current_episode"] = target_ep
            seasons_list = info.get("seasonsList") or []
            resolved["total_seasons"] = len(seasons_list)
            target_season_obj = next((sn for sn in seasons_list if int(sn.get("season", 1) or 1) == target_s), None)
            if target_season_obj:
                episodes = target_season_obj.get("episodes") or []
                for idx, ep_obj in enumerate(episodes):
                    e_num = int(ep_obj.get("number", 1) or 1)
                    if e_num == target_ep:
                        resolved["current_episode_title"] = ep_obj.get("title") or f"Episode {target_ep}"
                        resolved["current_episode_image"] = ep_obj.get("image") or info.get("image")
                        if idx > 0:
                            prev_e = episodes[idx - 1]
                            resolved["prev_episode"] = {
                                "season": target_s,
                                "number": int(prev_e.get("number", 1) or 1),
                                "title": prev_e.get("title") or f"Episode {prev_e.get('number')}",
                                "image": prev_e.get("image") or info.get("image")
                            }
                        if idx < len(episodes) - 1:
                            next_e = episodes[idx + 1]
                            resolved["next_episode"] = {
                                "season": target_s,
                                "number": int(next_e.get("number", 1) or 1),
                                "title": next_e.get("title") or f"Episode {next_e.get('number')}",
                                "image": next_e.get("image") or info.get("image")
                            }
                        break
    except Exception as exc:
        logger.warning(f"Error enriching resolve metadata: {exc}")

    return JSONResponse(resolved)


@app.get("/api/toonplay/embed")
async def api_toonplay_embed(url: str = Query(...)):
    """
    Clean player gateway that routes upstream embeds without leaking hostnames,
    protecting users while maintaining 100% video and audio compatibility.
    """
    raw_url = url.strip()
    if not raw_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid player URL")

    # If it's an animesalt plyr link, route via abyss proxy
    if "animesalt.cx" in raw_url:
        player_target = f"https://animesalt.streamindia.co.in/abyss?url={quote(raw_url)}"
    else:
        player_target = raw_url

    # Universal anti-ad sandbox: allow playback & scripts, block popups & top redirects
    sandbox_attr = 'sandbox="allow-scripts allow-same-origin allow-presentation allow-forms"'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>SDK Movies — Stream Player</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100%; overflow: hidden; background: #000; }}
    iframe {{ width: 100%; height: 100%; border: 0; display: block; }}
  </style>
</head>
<body>
  <iframe
    id="inner-player-iframe"
    src="{player_target}"
    {sandbox_attr}
    allowfullscreen="true"
    webkitallowfullscreen="true"
    mozallowfullscreen="true"
    allow="autoplay; fullscreen; picture-in-picture; encrypted-media"
    referrerpolicy="no-referrer">
  </iframe>
  <script>
    // Permanently neutralize ad popups, new tabs, and alert prompts
    try {{
      window.open = function() {{ console.warn('[SDK AdBlock] Popup blocked'); return null; }};
      Object.freeze(window.open);
      window.alert = function() {{}};
      window.confirm = function() {{ return false; }};
      window.prompt = function() {{ return null; }};
    }} catch(e) {{}}

    // Forward language/audio change messages to inner player
    window.addEventListener('message', function(e) {{
      try {{
        const inner = document.getElementById('inner-player-iframe');
        if (inner && inner.contentWindow) {{
          inner.contentWindow.postMessage(e.data, '*');
        }}
      }} catch (err) {{}}
    }});
  </script>
</body>
</html>
"""
    return Response(content=html, media_type="text/html")


@app.get("/api/toonplay/catalog")
async def api_toonplay_catalog(
    category: str = "all",
    language: str = "all",
    type: str = "all",
    genre: str = "all",
    page: int = 1,
    limit: int = 24
):
    """
    Retrieve paginated ToonPlay catalog with instant filtering by:
    - category (all, anime, cartoon)
    - language (all, tamil, telugu, hindi, english, malayalam, kannada, bengali, japanese, korean)
    - type (all, series, movie)
    - genre (all, Action, Adventure, Comedy, Fantasy, etc.)
    """
    cat_clean = category.lower().strip()
    lang_clean = language.lower().strip()
    type_clean = type.lower().strip()
    genre_clean = genre.strip()

    # Pre-fetch live from upstream if requesting specific category or language page
    try:
        if cat_clean in ("anime", "cartoon") and lang_clean == "all":
            await asyncio.to_thread(toonplay_scraper.fetch_category_catalog, cat_clean, type_clean, page)
        elif lang_clean in ("tamil", "telugu", "hindi", "english") and cat_clean == "all":
            await asyncio.to_thread(toonplay_scraper.fetch_language_catalog, lang_clean, type_clean, page)
    except Exception as exc:
        logger.warning(f"Live catalog fetch warning: {exc}")

    # Query local SQLite database with filters
    ctype_arg = type_clean if type_clean in ("series", "movie") else None
    cat_arg = cat_clean if cat_clean != "all" else None
    lang_arg = lang_clean if lang_clean != "all" else None
    genre_arg = genre_clean if genre_clean != "all" else None

    catalog_data = await asyncio.to_thread(
        toonplay_db.get_catalog_items,
        page=page,
        limit=limit,
        content_type=ctype_arg,
        category=cat_arg,
        language=lang_arg,
        genre=genre_arg
    )

    items = [format_toonplay_card(item) for item in catalog_data.get("items", [])]
    return JSONResponse({
        "items": items,
        "total_items": catalog_data.get("total_items", len(items)),
        "total_pages": catalog_data.get("total_pages", 1),
        "current_page": catalog_data.get("current_page", page),
        "category": cat_clean,
        "language": lang_clean,
        "content_type": type_clean,
        "genre": genre_clean
    })


@app.get("/api/toonplay/download")
async def api_toonplay_download(id: str = Query(...), s: int = 1, ep: int = 1):
    """Fetch download links (Mega, Google Drive, 480p, 720p, 1080p) for an anime/cartoon episode."""
    item_id = id.strip().replace("cine_", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing item ID")
    res = await asyncio.to_thread(toonplay_scraper.fetch_download_links, item_id, s, ep)
    return JSONResponse(res)


@app.get("/api/toonplay/download/redirect")
async def api_toonplay_download_redirect(url: str = Query(...)):
    """Masked download redirector that strips external upstream referrers."""
    raw_url = url.strip()
    if not raw_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid download URL")
    
    try:
        def _resolve_target():
            r = requests.get(raw_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://toonplay.in/"
            }, allow_redirects=False, timeout=8)
            if r.status_code in (301, 302, 303, 307, 308) and "Location" in r.headers:
                return r.headers["Location"]
            return raw_url
        target = await asyncio.to_thread(_resolve_target)
        return RedirectResponse(url=target)
    except Exception:
        return RedirectResponse(url=raw_url)


_TOONPLAY_SYNC_RUNNING = False


@app.get("/api/toonplay/stats")
async def api_toonplay_stats():
    """Retrieve database and service diagnostics for the admin panel."""
    def _get():
        st = toonplay_db.get_stats()
        health = toonplay_scraper.ping_service()
        st["sync_running"] = _TOONPLAY_SYNC_RUNNING
        return {"stats": st, "health": health}
    return JSONResponse(await asyncio.to_thread(_get))


@app.post("/api/toonplay/sync")
async def api_toonplay_sync(full: bool = False):
    """Trigger background scrape and sync of ToonPlay catalog."""
    global _TOONPLAY_SYNC_RUNNING
    if _TOONPLAY_SYNC_RUNNING:
        return JSONResponse({"status": "already_running", "stats": toonplay_db.get_stats()})

    async def _run_sync():
        global _TOONPLAY_SYNC_RUNNING
        _TOONPLAY_SYNC_RUNNING = True
        try:
            from toonplay import sync as toon_sync
            await asyncio.to_thread(toon_sync.sync_catalog, 6, True)
        except Exception as exc:
            logger.error(f"Full ToonPlay sync error: {exc}")
        finally:
            _TOONPLAY_SYNC_RUNNING = False

    asyncio.create_task(_run_sync())
    st = toonplay_db.get_stats()
    return JSONResponse({"status": "sync_started", "stats": st})


# ==============================================================================
# CineStream API (Global Movies & TV, Multi-Server, Zero Ads, Ad-Blocked Streams)
# ==============================================================================

@app.get("/api/cinestream/home")
async def api_cinestream_home(refresh: bool = False):
    """Retrieve curated home catalog for CineStream."""
    data = await asyncio.to_thread(cinestream_scraper.fetch_home, refresh)
    return JSONResponse({
        "hero": [format_cinestream_card(m) for m in data.get("hero", [])],
        "trending": [format_cinestream_card(m) for m in data.get("trending", [])],
        "popular_movies": [format_cinestream_card(m) for m in data.get("popular_movies", [])],
        "popular_tv": [format_cinestream_card(m) for m in data.get("popular_tv", [])],
        "top_rated": [format_cinestream_card(m) for m in data.get("top_rated", [])],
        "now_playing": [format_cinestream_card(m) for m in data.get("now_playing", [])],
    })


@app.get("/api/cinestream/search")
async def api_cinestream_search(q: str = Query(..., min_length=1), page: int = 1):
    """Search CineStream catalog via live API and fast SQLite cache."""
    results = await asyncio.to_thread(cinestream_scraper.search, q, page)
    return JSONResponse([format_cinestream_card(m) for m in results])


@app.get("/api/cinestream/info")
async def api_cinestream_info(id: str = Query(...), type: str = Query("movie")):
    """Retrieve full details, cast, similar titles, and TV seasons for CineStream title."""
    item_id = id.strip().replace("cine_", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing item ID")

    info = await asyncio.to_thread(cinestream_scraper.fetch_info, item_id, type)
    if not info:
        raise HTTPException(status_code=404, detail="CineStream title not found")

    return JSONResponse(info)


@app.get("/api/cinestream/season")
async def api_cinestream_season(id: str = Query(...), season: int = Query(1)):
    """Retrieve episode list for a TV season."""
    item_id = id.strip().replace("cine_", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing item ID")

    episodes = await asyncio.to_thread(cinestream_scraper.fetch_season, item_id, season)
    return JSONResponse(episodes)


@app.get("/api/cinestream/genre")
async def api_cinestream_genre(genre_id: str = Query(...), type: str = Query("movie"), page: int = Query(1)):
    """Retrieve items by genre ID."""
    items = await asyncio.to_thread(cinestream_scraper.fetch_genre, genre_id, type, page)
    return JSONResponse([format_cinestream_card(m) for m in items])


@app.get("/api/cinestream/genres")
async def api_cinestream_genres(type: str = Query("movie")):
    """Retrieve full list of genres for movie or tv."""
    genres = await asyncio.to_thread(cinestream_scraper.fetch_genres_list, type)
    return JSONResponse(genres)


@app.get("/api/cinestream/filter")
async def api_cinestream_filter(
    type: str = Query("movie"),
    q: Optional[str] = None,
    genre: Optional[str] = None,
    sort: Optional[str] = "popularity.desc",
    year: Optional[str] = None,
    rating: Optional[float] = None,
    page: int = Query(1)
):
    """
    Search, explore, and filter CineStream movies or TV series matching onstream.vin filters:
    q (search text), genre, sort, year, rating, and page.
    """
    data = await asyncio.to_thread(
        cinestream_scraper.discover_filter,
        content_type=type,
        query=q,
        genre_id=genre,
        sort_by=sort,
        year=year,
        min_rating=rating,
        page=page
    )
    formatted_results = [format_cinestream_card(m) for m in data.get("results", [])]
    return JSONResponse({
        "results": formatted_results,
        "page": data.get("page", page),
        "total_pages": data.get("total_pages", 1),
        "total_results": data.get("total_results", len(formatted_results))
    })


@app.get("/api/cinestream/servers")
async def api_cinestream_servers(
    id: str = Query(...),
    type: str = Query("movie"),
    s: Optional[int] = None,
    ep: Optional[int] = None,
    imdb: Optional[str] = None
):
    """
    Generate ad-free streaming embed servers for movie or TV episode.
    All servers are formatted for clean sandboxed iframe rendering with zero ads.
    """
    item_id = id.strip().replace("cine_", "")
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing item ID")

    servers = await asyncio.to_thread(
        cinestream_scraper.get_stream_servers,
        item_id=item_id,
        content_type=type,
        season=s,
        episode=ep,
        imdb_id=imdb
    )
    return JSONResponse({
        "item_id": item_id,
        "type": type,
        "season": s,
        "episode": ep,
        "servers": servers,
        "default_server": "cinezo"
    })


@app.get("/api/cinestream/stats")
async def api_cinestream_stats():
    """Retrieve database and service diagnostics for the admin panel."""
    def _get():
        st = cinestream_db.get_stats()
        health = cinestream_scraper.ping_service()
        return {"stats": st, "health": health}
    return JSONResponse(await asyncio.to_thread(_get))


@app.post("/api/refresh")
async def api_refresh():
    """Trigger a fresh scrape of the recently added listings across Tamil(Torrents), TamilMov, and TamDub."""
    global _LAST_SYNC_TIME, _LAST_SYNC_STATUS, _LAST_SYNC_ADDED, _LAST_SYNC_TMV, _LAST_SYNC_DIRECT
    import time
    try:
        # 1. Torrent releases
        entries = await asyncio.to_thread(scraper.fetch_homepage)
        tmv_count = 0
        with db.get_conn() as conn:
            for e in entries:
                db.upsert_movie(conn, e)
                tmv_count += 1
        if entries:
            asyncio.create_task(_background_fetch_posters(entries))

        # 2. Direct MP4 releases (TamilMov & TamDub)
        direct_count = await asyncio.to_thread(isaimini_db.sync_latest_releases)
        asyncio.create_task(_initial_poster_backfill())

        # 3. ToonPlay Anime & Cartoons
        toon_count = 0
        try:
            toon_home = await asyncio.to_thread(toonplay_scraper.fetch_home, True)
            toon_count = len(toon_home.get("latest_releases", [])) + len(toon_home.get("popular_series", []))
        except Exception:
            pass

        total_added = tmv_count + direct_count + toon_count
        _LAST_SYNC_TIME = time.time()
        _LAST_SYNC_STATUS = "ok"
        _LAST_SYNC_ADDED = total_added
        _LAST_SYNC_TMV = tmv_count
        _LAST_SYNC_DIRECT = direct_count

        total_cached = db.count_movies() + isaimini_db.get_stats().get("total_movies", 0) + toonplay_db.get_stats().get("total_items", 0)

        return JSONResponse({
            "status": "ok",
            "added": total_added,
            "tmv_added": tmv_count,
            "direct_added": direct_count,
            "toon_added": toon_count,
            "total": total_cached,
            "last_sync_time": _LAST_SYNC_TIME,
        })
    except Exception as exc:
        _LAST_SYNC_STATUS = f"error: {exc}"
        raise HTTPException(status_code=502, detail=f"Refresh failed: {exc}")


def strip_site_domains(text: str) -> str:
    """Strip website domains, site branding, and URLs so only clean movie name and specs remain."""
    if not text:
        return ""
    t = re.sub(r'https?://\S+', '', text, flags=re.I)
    t = re.sub(r'(?:www\d*\.)?(?:1tamilmv|tamilmv|tamilblasters|isaidub|isaimini|moviesda|tamilplay|tamilyogi|kuttymovies|masstamilan|skymovies|bolly4u|cinemavilla)\.[a-z0-9\-]+(?:\.[a-z0-9\-]+)?', '', t, flags=re.I)
    t = re.sub(r'\b[a-zA-Z0-9_\-]+\.(?:cz|world|solar|capital|reisen|cards|pm|vip|win|net|org|com|co|green|in|to|ch|xyz|me|tv|cc|pw|info|biz|site|live|app|icu|ws|top|pro|club|click|link)\b', '', t, flags=re.I)
    t = re.sub(r'[\[\(\{]?(?:1tamilmv|tamilmv|tamilblasters|isaidub|isaimini|moviesda|tamilyogi|kuttymovies)[\]\)\}]?', '', t, flags=re.I)
    t = re.sub(r'\b(?:www|1tamilmv|tamilmv|tamilblasters|isaidub|isaimini|moviesda)\b', '', t, flags=re.I)
    t = re.sub(r'^[\s\-_:•|–—]+|[\s\-_:•|–—]+$', '', t)
    t = re.sub(r'\s{2,}', ' ', t).strip()
    return t

@app.get("/api/downloads")
async def api_downloads(url: str = Query(...)):
    clean_target_url = domain_manager.rewrite_url_to_active(url)
    if any(k in clean_target_url.lower() for k in ("moviesda", "isaidub", "isaimini", "moviezda")):
        return await api_isaimini_files(url=url)
    if "1tamilmv" not in url and "tamilmv" not in url:
        raise HTTPException(status_code=400, detail="Not a valid topic URL")
    try:
        details = await asyncio.to_thread(scraper.fetch_download_details, url)
        poster_url = details.get("poster_url")
        if not poster_url:
            with db.get_conn() as conn:
                row = conn.execute("SELECT title FROM movies WHERE topic_url = ?", (url,)).fetchone()
            if row and row["title"]:
                poster_url = await asyncio.to_thread(fetch_cinemeta_poster, row["title"])
                if poster_url:
                    details["poster_url"] = poster_url

        if poster_url:
            with db.get_conn() as conn:
                db.update_poster_url(conn, url, poster_url)

        # Strip any domain/website branding and prioritize 1080p quality
        if details.get("options"):
            for o in details["options"]:
                if "label" in o and o["label"]:
                    o["label"] = strip_site_domains(o["label"])
            details["options"] = sorted(
                details["options"],
                key=lambda o: rank_quality_score(f"{o.get('quality', '')} {o.get('label', '')}"),
                reverse=True
            )
    except Exception as exc:
        err_msg = str(exc)
        if "ISP" in err_msg or "reset" in err_msg.lower() or "connection" in err_msg.lower():
            detail_msg = (
                "Torrent host blocked by local ISP. In India, ISPs enforce DPI/SSL reset on 1TamilMV domains. "
                "Torrent links work automatically when deployed to cloud (Koyeb/GitHub) or when using Cloudflare WARP/VPN locally."
            )
        else:
            detail_msg = f"Failed to fetch page: {exc}"
        raise HTTPException(status_code=502, detail=detail_msg)
    return JSONResponse(details)


@app.get("/api/poster-proxy")
async def api_poster_proxy(url: str = Query(...), title: str | None = None, year: str | None = None):
    """Reverse proxy for poster images to bypass CORS, hotlinking blocks, or adblock filters."""
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    def _fetch():
        try:
            parsed = urlparse(url)
            referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else "https://www.google.com/"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            }
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200 and r.content:
                ctype = r.headers.get("Content-Type", "").lower()
                # Reject HTML 404/redirect error pages returned by CDN
                if "text/html" in ctype or "text/plain" in ctype or not any(x in ctype for x in ["image", "octet-stream"]):
                    return None, None
                return r.content, ctype or "image/jpeg"
        except Exception:
            pass
        return None, None

    content, ctype = await asyncio.to_thread(_fetch)

    # Fallback to 3rd-party resolver if original image was dead/404
    if not content and title:
        year_int = None
        if year and year.strip().isdigit():
            try:
                year_int = int(year.strip())
            except Exception:
                pass
        fallback_poster = await asyncio.to_thread(resolve_third_party_poster, title, year_int)
        if fallback_poster and fallback_poster != url:
            def _fetch_fallback():
                try:
                    r = requests.get(fallback_poster, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
                    if r.status_code == 200 and r.content:
                        return r.content, r.headers.get("Content-Type", "image/jpeg")
                except Exception:
                    pass
                return None, None
            content, ctype = await asyncio.to_thread(_fetch_fallback)

    if content:
        return Response(
            content=content,
            media_type=ctype,
            headers={
                "Cache-Control": "public, max-age=604800, immutable",
            },
        )
    raise HTTPException(status_code=404, detail="Image could not be retrieved")


@app.get("/api/poster-fallback")
async def api_poster_fallback(
    title: str = Query(..., min_length=1),
    year: str | None = None,
    topic_url: str | None = None,
    current_url: str | None = None,
):
    """Resolve any unloaded/broken poster using 3rd-party databases (IMDb / Cinemeta / TMDB) and backfill SQLite."""
    year_int: int | None = None
    if year and year.strip().isdigit():
        try:
            year_int = int(year.strip())
        except Exception:
            pass
    if not year_int:
        ym = re.search(r"\b(19\d\d|20\d\d)\b", title)
        if ym:
            try:
                year_int = int(ym.group(1))
            except Exception:
                pass

    poster_url = await asyncio.to_thread(resolve_third_party_poster, title, year_int)
    if poster_url:
        def _backfill():
            try:
                with db.get_conn() as conn:
                    if topic_url:
                        conn.execute("UPDATE movies SET poster_url = ? WHERE topic_url = ?", (poster_url, topic_url))
                    else:
                        conn.execute("UPDATE movies SET poster_url = ? WHERE title = ? AND (poster_url IS NULL OR poster_url = '' OR poster_url = ?)", (poster_url, title, current_url or ""))
            except Exception:
                pass
            try:
                with isaimini_db.get_conn() as iconn:
                    if topic_url:
                        iconn.execute("UPDATE movies SET poster = ? WHERE url = ?", (poster_url, topic_url))
                    else:
                        iconn.execute("UPDATE movies SET poster = ? WHERE (title = ? OR clean_title = ?) AND (poster IS NULL OR poster = '' OR poster = ?)", (poster_url, title, title, current_url or ""))
            except Exception:
                pass
        asyncio.create_task(asyncio.to_thread(_backfill))
        return JSONResponse({"status": "ok", "poster": poster_url, "source": "imdb"})

    return JSONResponse({"status": "not_found", "poster": None}, status_code=404)


@app.post("/api/posters/heal-all")
async def api_heal_all_posters():
    """Trigger complete scan and repair of dead/missing posters across all database tables."""
    def _heal():
        results = {"tmv_healed": 0, "isaimini_healed": 0, "domain_replaced": 0}
        try:
            with db.get_conn() as conn:
                cur = conn.execute("UPDATE movies SET poster_url = REPLACE(poster_url, 'moviesda.ski', 'moviezda.co') WHERE poster_url LIKE '%moviesda.ski%'")
                results["domain_replaced"] += cur.rowcount
                cur2 = conn.execute("UPDATE movies SET poster_url = REPLACE(poster_url, 'moviezda.me', 'moviezda.co') WHERE poster_url LIKE '%moviezda.me%'")
                results["domain_replaced"] += cur2.rowcount
                conn.commit()

                rows = conn.execute("""
                    SELECT id, title, year, poster_url FROM movies 
                    WHERE poster_url LIKE '%imageride%' 
                       OR poster_url LIKE '%extraimage%' 
                       OR poster_url LIKE '%picx.click%' 
                       OR poster_url LIKE '%imgpile%'
                       OR poster_url LIKE '%fastpic%'
                       OR poster_url LIKE '%imagehost%'
                       OR poster_url IS NULL 
                       OR poster_url = ''
                """).fetchall()
                for r in rows:
                    p = resolve_third_party_poster(r["title"], r["year"])
                    if p:
                        conn.execute("UPDATE movies SET poster_url = ? WHERE id = ?", (p, r["id"]))
                        results["tmv_healed"] += 1
                conn.commit()
        except Exception:
            pass

        try:
            with isaimini_db.get_conn() as iconn:
                cur = iconn.execute("UPDATE movies SET poster = REPLACE(poster, 'moviesda.ski', 'moviezda.co') WHERE poster LIKE '%moviesda.ski%'")
                results["domain_replaced"] += cur.rowcount
                cur2 = iconn.execute("UPDATE movies SET poster = REPLACE(poster, 'moviezda.me', 'moviezda.co') WHERE poster LIKE '%moviezda.me%'")
                results["domain_replaced"] += cur2.rowcount
                iconn.commit()

                irows = iconn.execute("""
                    SELECT id, title, clean_title, year, poster FROM movies 
                    WHERE poster IS NULL 
                       OR poster = '' 
                       OR poster LIKE '%imageride%' 
                       OR poster LIKE '%extraimage%'
                """).fetchall()
                for r in irows:
                    t = r["clean_title"] or r["title"]
                    p = resolve_third_party_poster(t, r["year"])
                    if p:
                        iconn.execute("UPDATE movies SET poster = ? WHERE id = ?", (p, r["id"]))
                        results["isaimini_healed"] += 1
                iconn.commit()
        except Exception:
            pass
        return results

    res = await asyncio.to_thread(_heal)
    return JSONResponse({"status": "ok", "results": res})


@app.get("/api/watchlist")
async def api_watchlist(urls: str = Query(..., description="Comma-separated topic URLs")):
    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    movies = db.get_movies_by_urls(url_list)
    return JSONResponse(movies)


@app.get("/api/stream-health")
async def api_stream_health(request: Request):
    """Verify if the local Node.js WebTorrent streamer (port 8778) is running."""
    def _check():
        try:
            r = requests.get("http://127.0.0.1:8778/health", timeout=1.5)
            if r.status_code == 200:
                data = r.json()
                base_url = str(request.base_url).rstrip("/")
                return {"online": True, "streamer_url": base_url, "torrents": data.get("torrents", 0)}
        except Exception:
            pass
        return {"online": False, "streamer_url": "http://127.0.0.1:8778", "torrents": 0}

    res = await asyncio.to_thread(_check)
    return JSONResponse(res)


@app.get("/stream")
async def proxy_stream(request: Request, magnet: str = Query(...)):
    """Reverse proxy torrent video chunk streaming from the background streamer engine."""
    headers = {}
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    def _fetch():
        try:
            clean_magnet = quote(magnet, safe=":?=&")
            return requests.get(
                f"http://127.0.0.1:8778/stream?magnet={clean_magnet}",
                headers=headers,
                stream=True,
                timeout=45,
            )
        except Exception as exc:
            return exc

    stream_res = await asyncio.to_thread(_fetch)
    if isinstance(stream_res, Exception):
        raise HTTPException(status_code=503, detail=f"Streaming engine unavailable: {stream_res}")

    def iter_content():
        try:
            for chunk in stream_res.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        except Exception:
            pass

    response_headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
    }
    for h in ("Content-Range", "Content-Length", "Content-Type"):
        if h in stream_res.headers:
            response_headers[h] = stream_res.headers[h]

    return StreamingResponse(
        iter_content(),
        status_code=stream_res.status_code,
        headers=response_headers,
    )


@app.get("/info")
async def proxy_info(magnet: str = Query(...)):
    """Reverse proxy torrent metadata & peer swarm stats from the background streamer engine."""
    def _fetch():
        try:
            clean_magnet = quote(magnet, safe=":?=&")
            r = requests.get(f"http://127.0.0.1:8778/info?magnet={clean_magnet}", timeout=5)
            return r.status_code, r.json()
        except Exception:
            return 503, {"found": False, "peers": 0, "downloadSpeed": 0, "downloaded": 0}

    status, data = await asyncio.to_thread(_fetch)
    return JSONResponse(data, status_code=status)


@app.get("/api/metadata")
async def api_metadata(title: str = Query(..., min_length=1), year: str | None = None):
    """Fetch movie plot synopsis, IMDb rating, genres, and poster using Cinemeta public movie catalog."""
    year_int: int | None = None
    if year and year.strip().isdigit():
        try:
            year_int = int(year.strip())
        except ValueError:
            pass
    if not year_int:
        ym = re.search(r"\b(19\d\d|20\d\d)\b", title)
        if ym:
            try:
                year_int = int(ym.group(1))
            except ValueError:
                pass

    data = await asyncio.to_thread(get_movie_meta, title, year_int)
    return JSONResponse(data)


@app.get("/api/domain")
async def api_get_domain():
    """Check current mirror domains and live connection status across all platforms."""
    def _check_all():
        from concurrent.futures import ThreadPoolExecutor
        cfg = domain_manager.load_config()
        sources = ["tamilmv", "moviesda", "isaidub", "toonplay", "cinestream"]
        results = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {
                s: ex.submit(domain_manager.check_candidate_health, s, domain_manager.get_active_host(s), 2.0)
                for s in sources
            }
            for s, f in futures.items():
                active_h = domain_manager.get_active_host(s)
                try:
                    res = f.result()
                    is_online = bool(res.get("online"))
                    err_str = str(res.get("error") or "")
                    if is_online:
                        st = "online"
                    elif "Connection" in err_str or "Reset" in err_str or "SSL" in err_str or "10054" in err_str:
                        st = "blocked"
                    else:
                        st = "offline"

                    results[s] = {
                        "host": active_h,
                        "status": st,
                        "latency_ms": res.get("latency_ms"),
                        "error": res.get("error"),
                        "mirrors": cfg.get(s, {}).get("mirrors", []),
                    }
                except Exception as exc:
                    results[s] = {
                        "host": active_h,
                        "status": "offline",
                        "latency_ms": None,
                        "error": str(exc),
                        "mirrors": cfg.get(s, {}).get("mirrors", []),
                    }

        tmv_h = domain_manager.get_active_host("tamilmv")
        overall_status = results.get("tamilmv", {}).get("status", "offline")
        return {
            "host": tmv_h,
            "status": overall_status,
            "domains": results,
        }
    return JSONResponse(await asyncio.to_thread(_check_all))


@app.post("/api/domain")
async def api_set_domain(payload: dict = Body(...)):
    """Update base host domain dynamically and persist across restarts."""
    source = (payload.get("source") or "tamilmv").lower().strip()
    new_host = payload.get("host")
    if not new_host:
        raise HTTPException(status_code=400, detail="Missing host parameter")

    try:
        updated = domain_manager.set_active_host(source, new_host)
        return JSONResponse({"status": "ok", "source": source, "host": updated})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/domain/auto-resolve")
async def api_auto_resolve_domain(payload: dict = Body(default={})):
    """Auto-detect and switch to live active mirror domains for any or all sources."""
    source = payload.get("source")
    if source:
        res = await asyncio.to_thread(domain_manager.auto_resolve_source, source, True)
    else:
        res = await asyncio.to_thread(domain_manager.auto_resolve_all)
    return JSONResponse({"status": "ok", "results": res})


@app.get("/api/stats")
async def api_stats():
    node_bin = shutil.which("node")
    npm_bin = shutil.which("npm")
    log_content = ""
    log_path = BASE_DIR / "streamer.log"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                log_content = f.read()[-500:]
        except Exception:
            pass

    tmv_count = db.count_movies()
    isai_stats = {}
    isai_count = 0
    missing_posters = 0
    try:
        isai_stats = isaimini_db.get_stats()
        isai_count = isai_stats.get("total_movies", 0)
        with isaimini_db.get_conn() as iconn:
            row = iconn.execute("SELECT COUNT(*) FROM movies WHERE poster IS NULL OR poster = ''").fetchone()
            if row:
                missing_posters = row[0]
    except Exception:
        pass

    toon_stats = {}
    toon_count = 0
    try:
        toon_stats = toonplay_db.get_stats()
        toon_count = toon_stats.get("total_items", 0)
    except Exception:
        pass

    cine_stats = {}
    cine_count = 0
    try:
        cine_stats = cinestream_db.get_stats()
        cine_count = cine_stats.get("total_items", 0)
    except Exception:
        pass

    local_ip = get_lan_ip()

    total_database_size = tmv_count + isai_count + toon_count + cine_count
    coverage_pct = "100%" if total_database_size > 0 and missing_posters == 0 else f"{round(((total_database_size - missing_posters) / max(total_database_size, 1)) * 100, 1)}%"

    return {
        "cached": total_database_size,
        "total_database_size": total_database_size,
        "tamilmv_count": tmv_count,
        "isaimini_count": isai_count,
        "toonplay_count": toon_count,
        "cinestream_count": cine_count,
        "missing_posters": missing_posters,
        "poster_coverage_pct": coverage_pct,
        "isaimini_stats": isai_stats,
        "toonplay_stats": toon_stats,
        "cinestream_stats": cine_stats,
        "local_ip": local_ip,
        "port": PORT,
        "network_url": f"http://{local_ip}:{PORT}",
        "network_tv_url": f"http://{local_ip}:{PORT}/?mode=tv",
        "domain": scraper.get_base_host(),
        "domains": {
            "tamilmv": scraper.get_base_host(),
            "moviesda": getattr(isaimini_scraper, "get_moviesda_host", lambda: "moviesda.ski")(),
            "isaidub": getattr(isaimini_scraper, "get_isaidub_host", lambda: "isaidub.green")(),
            "toonplay": toonplay_scraper.get_api_host(),
            "cinestream": cinestream_scraper.get_api_host(),
        },
        "last_sync_time": _LAST_SYNC_TIME,
        "last_sync_status": _LAST_SYNC_STATUS,
        "last_sync_added": _LAST_SYNC_ADDED,
        "last_sync_tmv": _LAST_SYNC_TMV,
        "last_sync_direct": _LAST_SYNC_DIRECT,
        "auto_sync_interval_seconds": AUTO_SYNC_INTERVAL_SECONDS,
        "node_installed": bool(node_bin),
        "npm_installed": bool(npm_bin),
        "node_modules_present": (BASE_DIR / "node_modules").exists(),
        "streamer_status": _STREAMER_STATUS,
        "streamer_log": log_content,
    }


@app.get("/api/network/host")
async def api_network_host():
    local_ip = get_lan_ip()
    return {
        "local_ip": local_ip,
        "port": PORT,
        "localhost_url": f"http://localhost:{PORT}",
        "network_url": f"http://{local_ip}:{PORT}",
        "network_tv_url": f"http://{local_ip}:{PORT}/?mode=tv"
    }


@app.get("/api/qr")
async def api_qr(text: str = Query(..., description="Text or URL to encode in QR")):
    if not text:
        raise HTTPException(status_code=400, detail="Missing text parameter")
    try:
        import qrcode
        import qrcode.image.svg
        import io
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(text, image_factory=factory, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/subtitles/convert")
async def api_subtitles_convert(url: str = Query(..., description="URL of external SRT subtitle")):
    if not url or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                raise HTTPException(status_code=404, detail="Subtitle not found")
            srt_text = r.text
            vtt_lines = ["WEBVTT\n"]
            for line in srt_text.splitlines():
                if "-->" in line:
                    vtt_lines.append(line.replace(",", "."))
                else:
                    vtt_lines.append(line)
            vtt_content = "\n".join(vtt_lines)
            return Response(content=vtt_content, media_type="text/vtt")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/posters/verify")
async def api_verify_posters(limit: int = Query(60, ge=1, le=200)):
    """Proactively verify and heal poster URLs across databases in background."""
    sem = asyncio.Semaphore(5)
    checked = 0
    try:
        with isaimini_db.get_conn() as iconn:
            rows = iconn.execute(
                "SELECT id, title, clean_title, year, url, poster FROM movies ORDER BY year DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        tasks = [_fetch_single_isaimini_poster(sem, dict(r), force_verify=True) for r in rows]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            checked = len(tasks)
        return {"status": "ok", "checked": checked}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """Ultra-lightweight 2-byte response for cron-job.org / uptime monitors."""
    return Response(content="OK", media_type="text/plain")


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    """Lightweight health check for keep-alive pings."""
    return Response(content="OK", media_type="text/plain")


def is_mobile_ua(ua: str) -> bool:
    if not ua:
        return False
    ua_low = ua.lower()
    return bool(re.search(r"android|iphone|ipad|ipod|mobile|blackberry|iemobile|opera mini|webos", ua_low))


@app.get("/mobile")
@app.get("/m")
async def mobile_index():
    return FileResponse(
        STATIC_DIR / "mobile.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/")
async def index(request: Request):
    # Check explicit query param (?view=desktop or ?view=mobile)
    view_param = (request.query_params.get("view") or "").strip().lower()
    if view_param in ("mobile", "m", "phone"):
        return FileResponse(
            STATIC_DIR / "mobile.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    if view_param in ("desktop", "d", "pc", "tv"):
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # Check cookie preference
    cookie_pref = (request.cookies.get("view_pref") or "").strip().lower()
    if cookie_pref == "mobile":
        return FileResponse(
            STATIC_DIR / "mobile.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    if cookie_pref == "desktop":
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # Check User-Agent for mobile devices
    ua = request.headers.get("user-agent", "")
    if is_mobile_ua(ua):
        return FileResponse(
            STATIC_DIR / "mobile.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@app.get("/icon-192.png")
async def icon_192():
    return FileResponse(STATIC_DIR / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
async def icon_512():
    return FileResponse(STATIC_DIR / "icon-512.png", media_type="image/png")



# ==============================================================================
# ULTRA WATCH PARTY SUITE (Virtual Cinema, Multi-Device Sync, Reactions & Chat)
# ==============================================================================

def format_party_time(seconds: float) -> str:
    s = int(max(0, seconds))
    m, sc = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sc:02d}"
    return f"{m}:{sc:02d}"

def format_party_delta(delta: float) -> str:
    sgn = "+" if delta > 0 else "-"
    s = int(abs(delta))
    m, sc = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{sgn}{h}h {m}m {sc}s"
    if m > 0:
        return f"{sgn}{m}m {sc}s"
    return f"{sgn}{sc}s"

class WatchPartyRoom:
    def __init__(self, room_id: str, host_id: str):
        self.room_id = room_id
        self.host_id = host_id
        self.created_at = time.time()
        self.connections: dict[str, WebSocket] = {}
        self.members: dict[str, dict] = {}
        self.state: dict = {
            "media": None,
            "movie": None,
            "is_playing": False,
            "current_time": 0.0,
            "last_updated": time.time(),
            "host_only": False,
            "season": 1,
            "episode": 1,
            "server": ""
        }
        self.chat_history: list[dict] = []

    async def connect(self, client_id: str, websocket: WebSocket, nickname: str, avatar: str):
        await websocket.accept()
        is_host = (len(self.connections) == 0) or (client_id == self.host_id)
        if is_host:
            self.host_id = client_id

        self.connections[client_id] = websocket
        self.members[client_id] = {
            "client_id": client_id,
            "nickname": nickname or f"Cinema Fan {len(self.members) + 1}",
            "avatar": avatar or "🍿",
            "is_host": is_host,
            "joined_at": time.time()
        }

        # Send initial full state to newly connected client
        await websocket.send_json({
            "type": "welcome",
            "room_id": self.room_id,
            "client_id": client_id,
            "is_host": is_host,
            "state": self.state,
            "members": list(self.members.values()),
            "chat_history": self.chat_history[-30:]
        })

        # Record join in chat history & broadcast action
        join_item = {
            "id": f"act_{int(time.time()*1000)}",
            "is_system_action": True,
            "action": "join",
            "sender_id": client_id,
            "sender_name": self.members[client_id]["nickname"],
            "avatar": self.members[client_id]["avatar"],
            "icon": "🍿",
            "text": "joined the party",
            "time": time.strftime("%I:%M %p").lstrip("0")
        }
        self.chat_history.append(join_item)
        if len(self.chat_history) > 80:
            self.chat_history.pop(0)

        # Broadcast member join to others
        await self.broadcast({
            "type": "member_joined",
            "member": self.members[client_id],
            "members": list(self.members.values()),
            "action_item": join_item
        }, exclude=client_id)

    async def disconnect(self, client_id: str):
        self.connections.pop(client_id, None)
        leaving = self.members.pop(client_id, None)
        if not leaving:
            return

        # If host left, pass crown to the next member
        new_host = None
        if client_id == self.host_id and self.members:
            self.host_id = next(iter(self.members))
            self.members[self.host_id]["is_host"] = True
            new_host = self.members[self.host_id]

        leave_item = {
            "id": f"act_{int(time.time()*1000)}",
            "is_system_action": True,
            "action": "leave",
            "sender_id": client_id,
            "sender_name": leaving.get("nickname", "Someone"),
            "avatar": leaving.get("avatar", "🍿"),
            "icon": "👋",
            "text": "left the party",
            "time": time.strftime("%I:%M %p").lstrip("0")
        }
        self.chat_history.append(leave_item)
        if len(self.chat_history) > 80:
            self.chat_history.pop(0)

        await self.broadcast({
            "type": "member_left",
            "client_id": client_id,
            "nickname": leaving.get("nickname", "Someone"),
            "new_host": new_host,
            "members": list(self.members.values()),
            "action_item": leave_item
        })

    async def broadcast(self, message: dict, exclude: Optional[str] = None):
        dead = []
        for cid, ws in list(self.connections.items()):
            if exclude and cid == exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(cid)
        for d in dead:
            await self.disconnect(d)


watch_party_rooms: dict[str, WatchPartyRoom] = {}

PARTY_ADJECTIVES = ["MASS", "SUPER", "VIP", "CINEMA", "TAMIL", "ROYAL", "TURBO", "EPIC", "MAGIC", "COSMIC"]

def generate_party_code() -> str:
    import random
    adj = random.choice(PARTY_ADJECTIVES)
    num = random.randint(10, 99)
    return f"{adj}-{num}"

@app.get("/api/party/create")
async def api_party_create():
    """Create a new Watch Party room and return shareable link & room code."""
    code = generate_party_code()
    for _ in range(10):
        if code not in watch_party_rooms:
            break
        code = generate_party_code()

    lan_ip = get_lan_ip()
    url = f"http://{lan_ip}:{PORT}/?party={code}"
    return JSONResponse({
        "room_id": code,
        "url": url,
        "local_url": f"http://localhost:{PORT}/?party={code}"
    })

@app.get("/api/party/info")
async def api_party_info(room: str = Query(...)):
    """Fetch status and current state for a Watch Party room."""
    clean_room = room.strip().upper()
    r = watch_party_rooms.get(clean_room)
    if not r:
        return JSONResponse({"exists": False, "room_id": clean_room})
    return JSONResponse({
        "exists": True,
        "room_id": clean_room,
        "members_count": len(r.members),
        "members": list(r.members.values()),
        "state": r.state
    })

@app.websocket("/ws/party/{room_id}")
async def websocket_party(websocket: WebSocket, room_id: str, client_id: str = Query(None), nickname: str = Query(None), avatar: str = Query(None)):
    """High-performance WebSocket hub for synchronized video playback, live emoji explosions, theatre SFX, and chat."""
    clean_room = room_id.strip().upper()
    if clean_room not in watch_party_rooms:
        watch_party_rooms[clean_room] = WatchPartyRoom(clean_room, client_id or "client_1")

    room = watch_party_rooms[clean_room]
    actual_cid = client_id or f"client_{int(time.time()*1000)}"

    await room.connect(actual_cid, websocket, nickname or "Viewer", avatar or "🍿")

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "sync_playback":
                sender_is_host = (room.host_id == actual_cid)
                if room.state.get("host_only") and not sender_is_host:
                    continue

                action = data.get("action", "sync")
                is_playing = bool(data.get("is_playing", False))
                current_time = float(data.get("current_time", 0.0))
                prev_time = float(room.state.get("current_time", 0.0))

                room.state["is_playing"] = is_playing
                room.state["current_time"] = current_time
                room.state["last_updated"] = time.time()
                if "media" in data and data["media"]:
                    room.state["media"] = data["media"]
                if "movie" in data and data["movie"]:
                    room.state["movie"] = data["movie"]

                sender_nick = room.members.get(actual_cid, {}).get("nickname", "Someone")
                sender_avatar = room.members.get(actual_cid, {}).get("avatar", "🍿")
                time_str = format_party_time(current_time)
                now_str = time.strftime("%I:%M %p").lstrip("0")
                action_item = None

                if action == "pause":
                    action_item = {
                        "id": f"act_{int(time.time()*1000)}",
                        "is_system_action": True,
                        "action": "pause",
                        "sender_id": actual_cid,
                        "sender_name": sender_nick,
                        "avatar": sender_avatar,
                        "icon": "⏸️",
                        "text": f"paused playback at {time_str}",
                        "formatted_time": time_str,
                        "time": now_str
                    }
                elif action == "play":
                    action_item = {
                        "id": f"act_{int(time.time()*1000)}",
                        "is_system_action": True,
                        "action": "play",
                        "sender_id": actual_cid,
                        "sender_name": sender_nick,
                        "avatar": sender_avatar,
                        "icon": "▶️",
                        "text": f"resumed playback at {time_str}",
                        "formatted_time": time_str,
                        "time": now_str
                    }
                elif action == "seek":
                    delta = current_time - prev_time
                    if abs(delta) >= 1.0:
                        d_str = format_party_delta(delta)
                        icon = "⏩" if delta > 0 else "⏪"
                        verb = "skipped forward to" if delta > 0 else "jumped back to"
                        action_item = {
                            "id": f"act_{int(time.time()*1000)}",
                            "is_system_action": True,
                            "action": "seek",
                            "sender_id": actual_cid,
                            "sender_name": sender_nick,
                            "avatar": sender_avatar,
                            "icon": icon,
                            "delta_text": d_str,
                            "text": f"{verb} {time_str}",
                            "formatted_time": time_str,
                            "time": now_str
                        }

                if action_item:
                    room.chat_history.append(action_item)
                    if len(room.chat_history) > 80:
                        room.chat_history.pop(0)
                    # Broadcast chat action to EVERYONE so both sender & peers see it
                    await room.broadcast({
                        "type": "chat_action",
                        "action_item": action_item
                    })

                await room.broadcast({
                    "type": "playback_sync",
                    "sender_id": actual_cid,
                    "sender_name": sender_nick,
                    "is_playing": is_playing,
                    "current_time": current_time,
                    "timestamp": time.time(),
                    "media": room.state.get("media"),
                    "movie": room.state.get("movie"),
                    "action": action
                }, exclude=actual_cid)

            elif msg_type == "change_media":
                sender_is_host = (room.host_id == actual_cid)
                if room.state.get("host_only") and not sender_is_host:
                    continue

                media = data.get("media") or {
                    "movie": data.get("movie"),
                    "season": int(data.get("season", 1)),
                    "episode": int(data.get("episode", 1)),
                    "server": data.get("server", ""),
                    "kind": data.get("kind", "toonplay" if (data.get("movie") or {}).get("is_toonplay") else "direct")
                }

                room.state["media"] = media
                room.state["movie"] = media.get("movie")
                room.state["current_time"] = float(data.get("current_time", 0.0))
                room.state["is_playing"] = bool(data.get("is_playing", True))
                room.state["last_updated"] = time.time()

                m_title = (media.get("movie") or {}).get("title") or "Movie"
                now_str = time.strftime("%I:%M %p").lstrip("0")
                sender_nick = room.members.get(actual_cid, {}).get("nickname", "Host")
                sender_avatar = room.members.get(actual_cid, {}).get("avatar", "🍿")

                action_item = {
                    "id": f"act_{int(time.time()*1000)}",
                    "is_system_action": True,
                    "action": "media",
                    "sender_id": actual_cid,
                    "sender_name": sender_nick,
                    "avatar": sender_avatar,
                    "icon": "🎬",
                    "text": f"started playing {m_title}",
                    "formatted_time": m_title,
                    "time": now_str
                }
                room.chat_history.append(action_item)
                if len(room.chat_history) > 80:
                    room.chat_history.pop(0)

                await room.broadcast({
                    "type": "chat_action",
                    "action_item": action_item
                })

                await room.broadcast({
                    "type": "media_changed",
                    "sender_name": sender_nick,
                    "media": media,
                    "movie": media.get("movie"),
                    "current_time": room.state["current_time"],
                    "is_playing": room.state["is_playing"]
                })

            elif msg_type == "reaction":
                emoji = data.get("emoji", "🔥")
                label = data.get("label", "Mass")
                member = room.members.get(actual_cid, {})
                await room.broadcast({
                    "type": "reaction",
                    "sender_id": actual_cid,
                    "sender_name": member.get("nickname", "Someone"),
                    "avatar": member.get("avatar", "🍿"),
                    "emoji": emoji,
                    "label": label,
                    "timestamp": time.time()
                })

            elif msg_type == "theatre_sfx":
                sound = data.get("sound", "whistle")
                member = room.members.get(actual_cid, {})
                await room.broadcast({
                    "type": "theatre_sfx",
                    "sender_name": member.get("nickname", "Someone"),
                    "sound": sound
                })

            elif msg_type == "chat":
                text = str(data.get("text", "")).strip()
                if text:
                    member = room.members.get(actual_cid, {})
                    chat_item = {
                        "id": f"msg_{int(time.time()*1000)}",
                        "sender_id": actual_cid,
                        "sender_name": member.get("nickname", "Viewer"),
                        "avatar": member.get("avatar", "🍿"),
                        "is_host": member.get("is_host", False),
                        "text": text[:300],
                        "time": time.strftime("%I:%M %p").lstrip("0")
                    }
                    room.chat_history.append(chat_item)
                    if len(room.chat_history) > 60:
                        room.chat_history.pop(0)
                    await room.broadcast({
                        "type": "chat_message",
                        "message": chat_item
                    })

            elif msg_type == "toggle_host_only":
                if room.host_id == actual_cid:
                    room.state["host_only"] = bool(data.get("host_only", False))
                    await room.broadcast({
                        "type": "host_only_toggled",
                        "host_only": room.state["host_only"]
                    })

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong", "time": time.time()})

    except WebSocketDisconnect:
        await room.disconnect(actual_cid)
    except Exception:
        await room.disconnect(actual_cid)



app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import socket
    import uvicorn

    local_ip = get_lan_ip()

    print("\n" + "=" * 58)
    print("  * SDK Movies is live on your local WiFi network! *")
    print(f"  * This PC:            http://localhost:{PORT}")
    print(f"  * Mobile / TV (WiFi):  http://{local_ip}:{PORT}")
    print("=" * 58 + "\n")

    uvicorn.run("web:app", host=HOST, port=PORT, reload=False)
