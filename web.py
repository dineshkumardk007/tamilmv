"""
SDK Movies — a local web front-end over the same scraper the MCP server uses.

Run:
    .venv/Scripts/python.exe web.py
then open http://127.0.0.1:8777

Endpoints:
    GET /                      -> the SPA
    GET /api/search?q=...      -> [{title, year, language, quality, size, topic_url, ...}]
    GET /api/downloads?url=... -> [{quality, size, magnet, torrent_url, label}]
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import db
import scraper

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

PORT = 8777


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    # Auto-refresh cache on cold start if DB is empty
    if db.count_movies() == 0:
        try:
            entries = await asyncio.to_thread(scraper.fetch_homepage)
            with db.get_conn() as conn:
                for e in entries:
                    db.upsert_movie(conn, e)
        except Exception:
            pass  # fail silently, user can still search live
    yield


app = FastAPI(title="SDK Movies", lifespan=lifespan)


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

    return JSONResponse(results)


@app.get("/api/downloads")
async def api_downloads(url: str = Query(...)):
    if "1tamilmv" not in url:
        raise HTTPException(status_code=400, detail="Not a valid topic URL")
    try:
        options = await asyncio.to_thread(scraper.fetch_download_links, url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch page: {exc}")
    return JSONResponse(options)


@app.get("/api/stats")
async def api_stats():
    return {"cached": db.count_movies()}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
