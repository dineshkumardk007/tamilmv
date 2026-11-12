"""
toonplay/db.py — High-performance SQLite database storage with FTS5 search for ToonPlay (Anime & Cartoons).
Completely decoupled from the main torrent and isaimini databases.
"""
import contextlib
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any

logger = logging.getLogger("toonplay.db")

DB_PATH = Path(__file__).parent / "toonplay.db"


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create schema if not exists, including full-text search table."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                clean_title TEXT,
                content_type TEXT DEFAULT 'series',
                image TEXT,
                backdrop TEXT,
                overview TEXT,
                year TEXT,
                genres TEXT,
                languages TEXT,
                duration TEXT,
                rating REAL,
                category TEXT DEFAULT 'anime',
                seasons_count INTEGER DEFAULT 1,
                episodes_count INTEGER DEFAULT 1,
                raw_data TEXT,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                season_number INTEGER NOT NULL DEFAULT 1,
                episode_number INTEGER NOT NULL DEFAULT 1,
                title TEXT,
                image TEXT,
                link TEXT,
                servers_json TEXT,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_toon_episodes_item ON episodes(item_id, season_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_toon_items_type ON items(content_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_toon_items_scraped ON items(scraped_at DESC)")

        # Create FTS5 virtual table for lightning-fast search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS toon_fts USING fts5(
                title,
                clean_title,
                overview,
                genres,
                content='items',
                content_rowid='rowid'
            )
        """)

        # Triggers to keep FTS in sync
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS toon_items_ai AFTER INSERT ON items BEGIN
                INSERT INTO toon_fts(rowid, title, clean_title, overview, genres)
                VALUES (new.rowid, new.title, new.clean_title, new.overview, new.genres);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS toon_items_ad AFTER DELETE ON items BEGIN
                INSERT INTO toon_fts(toon_fts, rowid, title, clean_title, overview, genres)
                VALUES ('delete', old.rowid, old.title, old.clean_title, old.overview, old.genres);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS toon_items_au AFTER UPDATE ON items BEGIN
                INSERT INTO toon_fts(toon_fts, rowid, title, clean_title, overview, genres)
                VALUES ('delete', old.rowid, old.title, old.clean_title, old.overview, old.genres);
                INSERT INTO toon_fts(rowid, title, clean_title, overview, genres)
                VALUES (new.rowid, new.title, new.clean_title, new.overview, new.genres);
            END;
        """)


def clean_title(title: str) -> str:
    if not title:
        return ""
    t = re.sub(r"\[.*?\]|\(.*?\)", "", title)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def upsert_item(item: dict[str, Any]) -> str:
    """Insert or update a ToonPlay cartoon/anime item."""
    init_db()
    item_id = item.get("id")
    if not item_id:
        return ""

    title = (item.get("title") or "").strip()
    c_title = clean_title(title)
    c_type = item.get("type") or item.get("content_type") or "series"
    image = item.get("image") or ""
    backdrop = item.get("backdrop") or image
    overview = item.get("overview") or ""
    year = str(item.get("year") or "")
    
    genres_val = item.get("genres") or item.get("categories") or []
    genres = json.dumps(genres_val) if isinstance(genres_val, list) else str(genres_val)
    
    languages_val = item.get("languages") or []
    languages = json.dumps(languages_val) if isinstance(languages_val, list) else str(languages_val)
    
    duration = str(item.get("duration") or item.get("episodeDuration") or "")
    rating = None
    if item.get("rating"):
        try:
            rating = float(item["rating"])
        except Exception:
            pass

    category = item.get("category") or "anime"
    seasons_cnt = int(item.get("seasons") or 1) if str(item.get("seasons", "")).isdigit() else 1
    episodes_cnt = int(item.get("episodes") or 1) if str(item.get("episodes", "")).isdigit() else 1
    raw_data = json.dumps(item)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO items (
                id, title, clean_title, content_type, image, backdrop, overview,
                year, genres, languages, duration, rating, category,
                seasons_count, episodes_count, raw_data, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                clean_title = excluded.clean_title,
                content_type = excluded.content_type,
                image = CASE WHEN excluded.image != '' THEN excluded.image ELSE items.image END,
                backdrop = CASE WHEN excluded.backdrop != '' THEN excluded.backdrop ELSE items.backdrop END,
                overview = CASE WHEN excluded.overview != '' THEN excluded.overview ELSE items.overview END,
                year = CASE WHEN excluded.year != '' THEN excluded.year ELSE items.year END,
                genres = excluded.genres,
                languages = excluded.languages,
                duration = excluded.duration,
                rating = COALESCE(excluded.rating, items.rating),
                category = excluded.category,
                seasons_count = excluded.seasons_count,
                episodes_count = excluded.episodes_count,
                raw_data = excluded.raw_data,
                scraped_at = CURRENT_TIMESTAMP
        """, (
            item_id, title, c_title, c_type, image, backdrop, overview,
            year, genres, languages, duration, rating, category,
            seasons_cnt, episodes_cnt, raw_data
        ))

    return item_id


def upsert_episodes(item_id: str, season_number: int, episodes_list: list[dict[str, Any]]):
    """Store episodes list for a series season."""
    init_db()
    if not episodes_list:
        return

    with get_conn() as conn:
        for ep in episodes_list:
            ep_id = ep.get("id") or f"{item_id}-s{season_number}e{ep.get('number', 1)}"
            ep_num = int(ep.get("number") or 1)
            title = ep.get("title") or f"Episode {ep_num}"
            image = ep.get("image") or ""
            link = ep.get("link") or ""
            servers = json.dumps(ep.get("watchServers")) if ep.get("watchServers") else None

            conn.execute("""
                INSERT INTO episodes (
                    id, item_id, season_number, episode_number, title, image, link, servers_json, scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    image = excluded.image,
                    link = excluded.link,
                    servers_json = COALESCE(excluded.servers_json, episodes.servers_json),
                    scraped_at = CURRENT_TIMESTAMP
            """, (ep_id, item_id, season_number, ep_num, title, image, link, servers))


def get_item_by_id(item_id: str) -> dict[str, Any] | None:
    """Retrieve full item details by ID."""
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["genres"] = json.loads(d["genres"]) if d.get("genres") else []
        except Exception:
            d["genres"] = []
        try:
            d["languages"] = json.loads(d["languages"]) if d.get("languages") else []
        except Exception:
            d["languages"] = []
        try:
            d["raw_data"] = json.loads(d["raw_data"]) if d.get("raw_data") else {}
        except Exception:
            d["raw_data"] = {}
        return d


def get_episodes(item_id: str, season_number: int | None = None) -> list[dict[str, Any]]:
    """Retrieve episodes for an item."""
    init_db()
    with get_conn() as conn:
        if season_number is not None:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE item_id = ? AND season_number = ? ORDER BY episode_number ASC",
                (item_id, season_number)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE item_id = ? ORDER BY season_number ASC, episode_number ASC",
                (item_id,)
            ).fetchall()
        
        result = []
        for r in rows:
            d = dict(r)
            if d.get("servers_json"):
                try:
                    d["watchServers"] = json.loads(d["servers_json"])
                except Exception:
                    d["watchServers"] = []
            else:
                d["watchServers"] = []
            result.append(d)
        return result


def search_items(query: str, content_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Fast FTS5 or LIKE search."""
    init_db()
    q = query.strip()
    if not q:
        return []

    with get_conn() as conn:
        # Try FTS first
        results = []
        try:
            fts_query = ' '.join(re.findall(r'\w+', q)) + '*'
            sql = """
                SELECT i.* FROM items i
                JOIN toon_fts f ON i.rowid = f.rowid
                WHERE toon_fts MATCH ?
            """
            params = [fts_query]
            if content_type and content_type in ('movie', 'series'):
                sql += " AND i.content_type = ?"
                params.append(content_type)
            sql += f" ORDER BY rank LIMIT {limit}"
            rows = conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]
        except Exception:
            results = []

        if not results:
            like_pat = f"%{q}%"
            sql = "SELECT * FROM items WHERE (title LIKE ? OR clean_title LIKE ?)"
            params = [like_pat, like_pat]
            if content_type and content_type in ('movie', 'series'):
                sql += " AND content_type = ?"
                params.append(content_type)
            sql += f" ORDER BY scraped_at DESC LIMIT {limit}"
            rows = conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]

        for r in results:
            try:
                r["genres"] = json.loads(r["genres"]) if r.get("genres") else []
            except Exception:
                r["genres"] = []
        return results


def get_recent_items(limit: int = 30, offset: int = 0, content_type: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
    """Retrieve recently scraped items for TV shelves and catalog."""
    init_db()
    with get_conn() as conn:
        conditions = []
        params = []
        if content_type and content_type in ('movie', 'series'):
            conditions.append("content_type = ?")
            params.append(content_type)
        if category:
            conditions.append("category = ?")
            params.append(category)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM items {where_clause} ORDER BY rowid DESC LIMIT {limit} OFFSET {offset}"
        rows = conn.execute(sql, params).fetchall()
        results = [dict(r) for r in rows]
        for r in results:
            try:
                r["genres"] = json.loads(r["genres"]) if r.get("genres") else []
            except Exception:
                r["genres"] = []
            try:
                r["languages"] = json.loads(r["languages"]) if r.get("languages") else []
            except Exception:
                r["languages"] = []
        return results


def get_catalog_items(
    page: int = 1,
    limit: int = 24,
    content_type: str | None = None,
    category: str | None = None,
    language: str | None = None,
    genre: str | None = None
) -> dict[str, Any]:
    """Retrieve filtered, paginated catalog items with total count and total pages."""
    init_db()
    offset = max(0, (page - 1) * limit)
    with get_conn() as conn:
        conditions = []
        params = []
        if content_type and content_type in ('movie', 'series'):
            conditions.append("content_type = ?")
            params.append(content_type)
        if category and category != 'all':
            conditions.append("(category = ? OR raw_data LIKE ?)")
            params.extend([category, f'%"{category}"%'])
        if language and language != 'all':
            conditions.append("(languages LIKE ? OR raw_data LIKE ?)")
            params.extend([f'%{language}%', f'%{language}%'])
        if genre and genre != 'all':
            conditions.append("(genres LIKE ? OR raw_data LIKE ?)")
            params.extend([f'%{genre}%', f'%{genre}%'])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        
        # Count total
        count_sql = f"SELECT COUNT(*) FROM items {where_clause}"
        total_items = conn.execute(count_sql, params).fetchone()[0]
        total_pages = max(1, (total_items + limit - 1) // limit) if total_items > 0 else 1

        # Fetch items
        sql = f"SELECT * FROM items {where_clause} ORDER BY rowid DESC LIMIT {limit} OFFSET {offset}"
        rows = conn.execute(sql, params).fetchall()
        results = [dict(r) for r in rows]
        for r in results:
            try:
                r["genres"] = json.loads(r["genres"]) if r.get("genres") else []
            except Exception:
                r["genres"] = []
            try:
                r["languages"] = json.loads(r["languages"]) if r.get("languages") else []
            except Exception:
                r["languages"] = []

        return {
            "items": results,
            "total_items": total_items,
            "total_pages": total_pages,
            "current_page": page,
            "limit": limit
        }


def get_stats() -> dict[str, Any]:
    """Retrieve database counts for the diagnostics & admin modal."""
    init_db()
    with get_conn() as conn:
        total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        total_series = conn.execute("SELECT COUNT(*) FROM items WHERE content_type = 'series'").fetchone()[0]
        total_movies = conn.execute("SELECT COUNT(*) FROM items WHERE content_type = 'movie'").fetchone()[0]
        total_episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        last_item = conn.execute("SELECT title, scraped_at FROM items ORDER BY scraped_at DESC LIMIT 1").fetchone()

        return {
            "total_items": total_items,
            "total_series": total_series,
            "total_movies": total_movies,
            "total_episodes": total_episodes,
            "last_synced_title": last_item["title"] if last_item else None,
            "last_synced_at": last_item["scraped_at"] if last_item else None,
            "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0
        }
