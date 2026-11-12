"""
isaimini/server.py — MCP Server (stdio transport) exposing Moviesda / Isaimini tools.
Can be registered in Claude Code or Claude Desktop.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

# Ensure local imports work
sys.path.insert(0, str(Path(__file__).parent))
import db
import scraper

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None

mcp = FastMCP("isaimini-search") if FastMCP else None


if mcp:
    @mcp.tool()
    def search_isaimini(query: str, limit: int = 20) -> list[dict]:
        """Search Moviesda / Isaimini cached movies database by title, cast, or keywords."""
        db.init_db()
        return db.search_movies(query, limit=limit)

    @mcp.tool()
    def get_isaimini_latest() -> list[dict]:
        """Fetch real-time recently added releases from the Moviesda homepage ticker."""
        return scraper.fetch_latest_updates()

    @mcp.tool()
    def get_isaimini_movie(url: str) -> dict:
        """Fetch rich movie metadata: poster, cast, director, rating, synopsis, and quality folders."""
        db.init_db()
        details = scraper.fetch_movie_details(url)
        with db.get_conn() as conn:
            db.upsert_movie(conn, details)
        return details

    @mcp.tool()
    def get_isaimini_download_links(movie_url: str, resolve_direct: bool = True) -> list[dict]:
        """
        Discover all downloadable files for a movie URL (1080p, 720p, 360p)
        and resolve direct high-speed MP4 streaming and download links.
        """
        db.init_db()
        files = scraper.fetch_movie_download_options(movie_url)
        resolved_list = []

        movie_record = db.get_movie(movie_url)
        mid = movie_record["id"] if movie_record else None

        for f in files:
            entry = dict(f)
            if resolve_direct:
                try:
                    res = scraper.resolve_download_link(f["download_page_url"])
                    entry.update(res)
                    if mid:
                        with db.get_conn() as conn:
                            db.upsert_file(conn, mid, entry)
                except Exception as err:
                    entry["resolve_error"] = str(err)
            resolved_list.append(entry)

        return resolved_list

    @mcp.tool()
    def sync_isaimini_year(year: int = 2026, pages: int = 1) -> dict:
        """Crawl and cache movie listings for a specific release year into SQLite."""
        db.init_db()
        category_url = f"{scraper.BASE_URL}/tamil-{year}-movies/"
        session = scraper.create_session()
        added = 0

        for p in range(1, pages + 1):
            try:
                page_data = scraper.fetch_category_movies(category_url, page=p, session=session)
                with db.get_conn() as conn:
                    for m in page_data.get("movies", []):
                        db.upsert_movie(conn, m)
                        added += 1
                if not page_data.get("has_next"):
                    break
            except Exception as err:
                return {"status": "error", "page": p, "error": str(err), "added": added}

        return {"status": "ok", "year": year, "pages_crawled": pages, "movies_added": added, "total": db.get_stats()["total_movies"]}

    @mcp.tool()
    def isaimini_stats() -> dict:
        """Get summary statistics of cached Isaimini movies and resolved files."""
        db.init_db()
        return db.get_stats()


def main():
    if not mcp:
        print("MCP library not found. Install requirements with: pip install mcp")
        sys.exit(1)
    db.init_db()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
