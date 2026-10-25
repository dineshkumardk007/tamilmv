"""
SDK Movies — a local web front-end over the scraper and WebTorrent streamer.

Run:
    .venv/Scripts/python.exe web.py
then open http://127.0.0.1:8777
"""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import requests

import db
import scraper

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
PORT = int(os.environ.get("PORT", 8777))
HOST = os.environ.get("HOST", "0.0.0.0")

# In-memory cache for movie metadata (plot, ratings, genres, posters)
_META_CACHE: dict[str, dict] = {}


def fetch_cinemeta_poster(title: str) -> str | None:
    """Fallback to Cinemeta / IMDb / TMDB public catalog for movie & series posters."""
    clean_title = re.sub(r"\[.*?\]|\(.*?\)|S\d+|EP\d+|TRUE WEB-DL.*", "", title, flags=re.I).strip()
    if not clean_title:
        return None
    for ctype in ("movie", "series"):
        try:
            url = f"https://v3-cinemeta.strem.io/catalog/{ctype}/top/search={quote(clean_title)}.json"
            r = requests.get(url, timeout=3.5)
            if r.status_code == 200:
                metas = r.json().get("metas", [])
                if metas and metas[0].get("poster"):
                    return metas[0]["poster"]
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
                poster_url = await asyncio.to_thread(fetch_cinemeta_poster, title)
            except Exception:
                pass

        if poster_url:
            with db.get_conn() as conn:
                db.update_poster_url(conn, topic_url, poster_url)


async def _background_fetch_posters(results: list[dict]):
    """Fetch and cache posters in the background concurrently without blocking search response."""
    sem = asyncio.Semaphore(4)
    tasks = [_fetch_single_poster(sem, m) for m in results[:35] if not m.get("poster_url")]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _initial_poster_backfill():
    """Backfill missing posters for existing database records in background."""
    await asyncio.sleep(1)
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, title, topic_url, poster_url FROM movies WHERE poster_url IS NULL OR poster_url = '' ORDER BY id DESC LIMIT 60"
            ).fetchall()
        if rows:
            sem = asyncio.Semaphore(4)
            tasks = [_fetch_single_poster(sem, dict(r)) for r in rows]
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass


# Background Auto-Sync Cron State
_LAST_SYNC_TIME: float | None = None
_LAST_SYNC_STATUS: str = "never"
_LAST_SYNC_ADDED: int = 0
AUTO_SYNC_INTERVAL_SECONDS: int = 3600  # Run hourly


async def _background_auto_sync():
    """Background cron loop that periodically fetches new releases from homepage into SQLite."""
    global _LAST_SYNC_TIME, _LAST_SYNC_STATUS, _LAST_SYNC_ADDED
    import time
    # Wait 30 seconds after server launch before initial periodic sync
    await asyncio.sleep(30)
    while True:
        try:
            print("[Auto-Sync Cron] Starting scheduled homepage sync...")
            entries = await asyncio.to_thread(scraper.fetch_homepage)
            added = 0
            if entries:
                with db.get_conn() as conn:
                    for e in entries:
                        db.upsert_movie(conn, e)
                        added += 1
                asyncio.create_task(_background_fetch_posters(entries))
            _LAST_SYNC_TIME = time.time()
            _LAST_SYNC_STATUS = "ok"
            _LAST_SYNC_ADDED = added
            print(f"[Auto-Sync Cron] Scheduled sync finished. Added/updated {added} releases.")
        except Exception as exc:
            _LAST_SYNC_STATUS = f"error: {exc}"
            print(f"[Auto-Sync Cron] Periodic sync encountered error: {exc}")

        await asyncio.sleep(AUTO_SYNC_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time
    db.init_db()

    # Auto-refresh cache on cold start if DB is empty
    if db.count_movies() == 0:
        try:
            entries = await asyncio.to_thread(scraper.fetch_homepage)
            with db.get_conn() as conn:
                for e in entries:
                    db.upsert_movie(conn, e)
            global _LAST_SYNC_TIME, _LAST_SYNC_STATUS, _LAST_SYNC_ADDED
            _LAST_SYNC_TIME = time.time()
            _LAST_SYNC_STATUS = "ok"
            _LAST_SYNC_ADDED = len(entries)
        except Exception:
            pass  # fail silently, user can still search live

    # Background backfill of posters for already stored records
    asyncio.create_task(_initial_poster_backfill())
    # Start scheduled periodic auto-sync cron
    sync_task = asyncio.create_task(_background_auto_sync())
    yield
    sync_task.cancel()


app = FastAPI(title="SDK Movies", lifespan=lifespan)


@app.get("/api/recent")
async def api_recent(
    limit: int = Query(48, ge=1, le=100),
    offset: int = Query(0, ge=0),
    language: str | None = None,
    quality: str | None = None,
    sort_by: str = "recent",
    content_type: str | None = None,
):
    """Fetch latest releases from local database, or auto-sync if empty."""
    results = db.get_recent_movies(
        limit=limit,
        offset=offset,
        language=language,
        quality=quality,
        sort_by=sort_by,
        content_type=content_type,
    )

    if not results and offset == 0:
        try:
            entries = await asyncio.to_thread(scraper.fetch_homepage)
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
        except Exception:
            pass

    if results:
        asyncio.create_task(_background_fetch_posters(results))

    return JSONResponse(results)


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    refresh: bool = False,
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query")

    results = [] if refresh else db.search_movies(query)

    if not results:
        try:
            live = await asyncio.to_thread(scraper.fetch_search, query)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Live search failed: {exc}")
        with db.get_conn() as conn:
            for e in live:
                db.upsert_movie(conn, e)
        results = db.search_movies(query) or live

    if results:
        asyncio.create_task(_background_fetch_posters(results))

    return JSONResponse(results)


@app.post("/api/refresh")
async def api_refresh():
    """Trigger a fresh scrape of the homepage recently added listing."""
    global _LAST_SYNC_TIME, _LAST_SYNC_STATUS, _LAST_SYNC_ADDED
    import time
    try:
        entries = await asyncio.to_thread(scraper.fetch_homepage)
        count = 0
        with db.get_conn() as conn:
            for e in entries:
                db.upsert_movie(conn, e)
                count += 1
        _LAST_SYNC_TIME = time.time()
        _LAST_SYNC_STATUS = "ok"
        _LAST_SYNC_ADDED = count
        if entries:
            asyncio.create_task(_background_fetch_posters(entries))
        return JSONResponse({
            "status": "ok",
            "added": count,
            "total": db.count_movies(),
            "last_sync_time": _LAST_SYNC_TIME,
        })
    except Exception as exc:
        _LAST_SYNC_STATUS = f"error: {exc}"
        raise HTTPException(status_code=502, detail=f"Refresh failed: {exc}")


@app.get("/api/downloads")
async def api_downloads(url: str = Query(...)):
    if "1tamilmv" not in url:
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
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch page: {exc}")
    return JSONResponse(details)


@app.get("/api/poster-proxy")
async def api_poster_proxy(url: str = Query(...)):
    """Reverse proxy for poster images to bypass CORS, hotlinking blocks, or adblock filters."""
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    def _fetch():
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.google.com/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            }
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200 and r.content:
                ctype = r.headers.get("Content-Type", "image/jpeg")
                return r.content, ctype
        except Exception:
            pass
        return None, None

    content, ctype = await asyncio.to_thread(_fetch)
    if content:
        return Response(
            content=content,
            media_type=ctype,
            headers={
                "Cache-Control": "public, max-age=604800, immutable",
            },
        )
    raise HTTPException(status_code=404, detail="Image could not be retrieved")


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


@app.get("/api/metadata")
async def api_metadata(title: str = Query(..., min_length=1), year: int | None = None):
    """Fetch movie plot synopsis, IMDb rating, genres, and poster using Cinemeta public movie catalog."""
    clean_title = re.sub(r"\[.*?\]|\(.*?\)|S\d+|EP\d+|TRUE WEB-DL.*", "", title, flags=re.I).strip()
    cache_key = f"{clean_title.lower()}_{year or ''}"
    if cache_key in _META_CACHE:
        return JSONResponse(_META_CACHE[cache_key])

    def _fetch():
        for ctype in ("movie", "series"):
            try:
                search_url = f"https://v3-cinemeta.strem.io/catalog/{ctype}/top/search={quote(clean_title)}.json"
                resp = requests.get(search_url, timeout=3.5)
                if resp.status_code == 200:
                    data = resp.json()
                    metas = data.get("metas", [])
                    if metas:
                        target_id = metas[0].get("id")
                        fallback_poster = metas[0].get("poster")
                        if target_id:
                            full_resp = requests.get(f"https://v3-cinemeta.strem.io/meta/{ctype}/{target_id}.json", timeout=3.5)
                            if full_resp.status_code == 200:
                                meta = full_resp.json().get("meta", {})
                                return {
                                    "found": True,
                                    "rating": meta.get("imdbRating") or None,
                                    "genres": meta.get("genres") or [],
                                    "plot": meta.get("description") or None,
                                    "year": meta.get("year") or None,
                                    "poster": meta.get("poster") or fallback_poster or None,
                                }
                        if fallback_poster:
                            return {
                                "found": True,
                                "rating": None,
                                "genres": [],
                                "plot": None,
                                "year": None,
                                "poster": fallback_poster,
                            }
            except Exception:
                pass
        return {"found": False, "rating": None, "genres": [], "plot": None, "poster": None}

    data = await asyncio.to_thread(_fetch)
    _META_CACHE[cache_key] = data

    # If poster found, cache it in DB for matching title without poster
    if data.get("poster"):
        try:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE movies SET poster_url = ? WHERE title = ? AND (poster_url IS NULL OR poster_url = '')",
                    (data["poster"], title),
                )
        except Exception:
            pass

    return JSONResponse(data)


@app.get("/api/domain")
async def api_get_domain():
    """Check current mirror domain and connection status."""
    host = scraper.get_base_host()
    health_info = await asyncio.to_thread(scraper.check_host_health)
    return JSONResponse(health_info)


@app.post("/api/domain")
async def api_set_domain(payload: dict = Body(...)):
    """Update base host domain dynamically (e.g. if 1tamilmv rotates TLD)."""
    new_host = payload.get("host")
    if not new_host:
        raise HTTPException(status_code=400, detail="Missing host parameter")
    try:
        updated = scraper.set_base_host(new_host)
        health_info = await asyncio.to_thread(scraper.check_host_health, updated)
        return JSONResponse({"status": "ok", "host": updated, "health": health_info})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/stats")
async def api_stats():
    return {
        "cached": db.count_movies(),
        "domain": scraper.get_base_host(),
        "last_sync_time": _LAST_SYNC_TIME,
        "last_sync_status": _LAST_SYNC_STATUS,
        "last_sync_added": _LAST_SYNC_ADDED,
        "auto_sync_interval_seconds": AUTO_SYNC_INTERVAL_SECONDS,
    }

@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """Ultra-lightweight 2-byte response for cron-job.org / uptime monitors."""
    return Response(content="OK", media_type="text/plain")


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    """Lightweight health check for keep-alive pings."""
    return Response(content="OK", media_type="text/plain")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web:app", host=HOST, port=PORT, reload=False)
