"""
cinestream/db.py — High-performance SQLite database storage with FTS5 search for CineStream.
Caches TMDB metadata, categories, episodes, and search results for instant page loads.
"""
import contextlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Optional

logger = logging.getLogger("cinestream.db")

DB_PATH = Path(__file__).parent / "cinestream.db"


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
                tmdb_id INTEGER,
                imdb_id TEXT,
                title TEXT NOT NULL,
                clean_title TEXT,
                content_type TEXT DEFAULT 'movie',
                poster TEXT,
                backdrop TEXT,
                overview TEXT,
                year TEXT,
                genres TEXT,
                rating REAL DEFAULT 0,
                vote_count INTEGER DEFAULT 0,
                duration TEXT,
                raw_json TEXT,
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
                overview TEXT,
                still_path TEXT,
                air_date TEXT,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS shelves (
                name TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_cine_items_type ON items(content_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cine_items_rating ON items(rating DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cine_episodes_lookup ON episodes(item_id, season_number, episode_number)")

        # Create FTS5 virtual table for lightning-fast title search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS cine_fts USING fts5(
                title,
                clean_title,
                genres,
                content='items',
                content_rowid='rowid'
            )
        """)

        # Triggers to keep FTS in sync
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS cine_items_ai AFTER INSERT ON items BEGIN
                INSERT INTO cine_fts(rowid, title, clean_title, genres)
                VALUES (new.rowid, new.title, new.clean_title, new.genres);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS cine_items_ad AFTER DELETE ON items BEGIN
                INSERT INTO cine_fts(cine_fts, rowid, title, clean_title, genres)
                VALUES('delete', old.rowid, old.title, old.clean_title, old.genres);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS cine_items_au AFTER UPDATE ON items BEGIN
                INSERT INTO cine_fts(cine_fts, rowid, title, clean_title, genres)
                VALUES('delete', old.rowid, old.title, old.clean_title, old.genres);
                INSERT INTO cine_fts(rowid, title, clean_title, genres)
                VALUES (new.rowid, new.title, new.clean_title, new.genres);
            END;
        """)


def clean_title_str(title: str) -> str:
    if not title:
        return ""
    clean = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", clean).strip()


def upsert_item(item: dict[str, Any]) -> None:
    """Insert or update a CineStream movie or TV show."""
    item_id = str(item.get("id") or item.get("tmdb_id"))
    if not item_id or item_id == "None":
        return

    content_type = item.get("media_type") or item.get("content_type") or ("tv" if item.get("first_air_date") else "movie")
    title = item.get("title") or item.get("name") or "Unknown Title"
    release_date = item.get("release_date") or item.get("first_air_date") or ""
    year = release_date[:4] if len(release_date) >= 4 else ""

    poster = item.get("poster_path") or item.get("poster") or ""
    if poster and not poster.startswith("http"):
        poster = f"https://image.tmdb.org/t/p/w500{poster}"

    backdrop = item.get("backdrop_path") or item.get("backdrop") or ""
    if backdrop and not backdrop.startswith("http"):
        backdrop = f"https://image.tmdb.org/t/p/original{backdrop}"

    genres = item.get("genres")
    if isinstance(genres, list):
        if genres and isinstance(genres[0], dict):
            genres_str = ", ".join([g.get("name", "") for g in genres if g.get("name")])
        else:
            genres_str = ", ".join([str(g) for g in genres])
    else:
        genres_str = str(genres or "")

    rating = float(item.get("vote_average") or item.get("rating") or 0.0)
    vote_count = int(item.get("vote_count") or 0)
    overview = item.get("overview") or ""
    imdb_id = item.get("imdb_id") or ""
    tmdb_id = int(item.get("tmdb_id") or (item_id if item_id.isdigit() else 0))

    duration = str(item.get("runtime") or item.get("duration") or "")
    raw_json = json.dumps(item)

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO items (
                id, tmdb_id, imdb_id, title, clean_title, content_type,
                poster, backdrop, overview, year, genres, rating,
                vote_count, duration, raw_json, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                clean_title = excluded.clean_title,
                content_type = excluded.content_type,
                poster = CASE WHEN excluded.poster != '' THEN excluded.poster ELSE items.poster END,
                backdrop = CASE WHEN excluded.backdrop != '' THEN excluded.backdrop ELSE items.backdrop END,
                overview = excluded.overview,
                year = excluded.year,
                genres = excluded.genres,
                rating = excluded.rating,
                vote_count = excluded.vote_count,
                duration = excluded.duration,
                raw_json = excluded.raw_json,
                scraped_at = CURRENT_TIMESTAMP
        """, (
            item_id, tmdb_id, imdb_id, title, clean_title_str(title), content_type,
            poster, backdrop, overview, year, genres_str, rating,
            vote_count, duration, raw_json
        ))


def upsert_items(items: list[dict[str, Any]]) -> None:
    """Batch upsert multiple items."""
    for item in items:
        try:
            upsert_item(item)
        except Exception as e:
            logger.debug(f"Failed to upsert item: {e}")


def get_item(item_id: str) -> Optional[dict[str, Any]]:
    """Retrieve item by ID."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (str(item_id),)).fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("raw_json"):
            try:
                res["raw_data"] = json.loads(res["raw_json"])
            except Exception:
                pass
        return res


def save_shelf(name: str, data: Any) -> None:
    """Save a shelf cache with current timestamp."""
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO shelves (name, data_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
            """, (name, json.dumps(data), time.time()))
    except Exception as e:
        logger.error(f"Error saving shelf {name}: {e}")


def get_shelf(name: str, max_age_seconds: float = 600) -> Optional[Any]:
    """Retrieve a shelf from cache if fresh."""
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT data_json, updated_at FROM shelves WHERE name = ?", (name,)).fetchone()
            if not row:
                return None
            if time.time() - row["updated_at"] > max_age_seconds:
                return None
            return json.loads(row["data_json"])
    except Exception as e:
        logger.error(f"Error getting shelf {name}: {e}")
        return None


def search_db(query: str, limit: int = 30) -> list[dict[str, Any]]:
    """FTS5 search across cached CineStream titles."""
    clean = clean_title_str(query)
    if not clean:
        return []
    words = clean.split()
    fts_query = " ".join([f'"{w}"*' for w in words])

    with get_conn() as conn:
        try:
            rows = conn.execute("""
                SELECT items.* FROM items
                JOIN cine_fts ON items.rowid = cine_fts.rowid
                WHERE cine_fts MATCH ?
                ORDER BY rank, items.rating DESC
                LIMIT ?
            """, (fts_query, limit)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            # Fallback to LIKE
            like_pat = f"%{clean}%"
            rows = conn.execute("""
                SELECT * FROM items
                WHERE clean_title LIKE ? OR genres LIKE ?
                ORDER BY rating DESC
                LIMIT ?
            """, (like_pat, like_pat, limit)).fetchall()
            return [dict(r) for r in rows]


def save_season_episodes(item_id: str, season_number: int, episodes: list[dict[str, Any]], fallback_img: str = "") -> None:
    """Cache TV season episodes with guaranteed visual thumbnail/poster."""
    with get_conn() as conn:
        # Determine parent backdrop or poster if fallback_img not explicitly passed
        parent_img = fallback_img
        if not parent_img:
            p_row = conn.execute("SELECT backdrop, poster FROM items WHERE id = ?", (str(item_id),)).fetchone()
            if p_row:
                parent_img = p_row["backdrop"] or p_row["poster"] or ""

        for ep in episodes:
            ep_num = int(ep.get("episode_number") or 1)
            ep_id = f"{item_id}_s{season_number}_e{ep_num}"
            title = ep.get("name") or f"Episode {ep_num}"
            overview = ep.get("overview") or ""
            still_path = ep.get("still_path") or ""
            if still_path and not still_path.startswith("http"):
                still_path = f"https://image.tmdb.org/t/p/w500{still_path}"
            if not still_path and parent_img:
                still_path = parent_img
            air_date = ep.get("air_date") or ""

            conn.execute("""
                INSERT INTO episodes (id, item_id, season_number, episode_number, title, overview, still_path, air_date, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    overview = excluded.overview,
                    still_path = CASE WHEN excluded.still_path != '' THEN excluded.still_path ELSE episodes.still_path END,
                    air_date = excluded.air_date,
                    scraped_at = CURRENT_TIMESTAMP
            """, (ep_id, str(item_id), season_number, ep_num, title, overview, still_path, air_date))


def get_season_episodes(item_id: str, season_number: int) -> list[dict[str, Any]]:
    """Get cached episodes for a season, ensuring every episode has a valid poster/thumbnail."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM episodes
            WHERE item_id = ? AND season_number = ?
            ORDER BY episode_number ASC
        """, (str(item_id), season_number)).fetchall()
        episodes = [dict(r) for r in rows]

        # If any episode is missing still_path, fill from parent show's backdrop or poster
        needs_fallback = any(not ep.get("still_path") or "favicon.svg" in ep.get("still_path", "") for ep in episodes)
        if needs_fallback:
            p_row = conn.execute("SELECT backdrop, poster FROM items WHERE id = ?", (str(item_id),)).fetchone()
            fallback_img = (p_row["backdrop"] if p_row else "") or (p_row["poster"] if p_row else "") or ""
            if fallback_img:
                for ep in episodes:
                    if not ep.get("still_path") or "favicon.svg" in ep.get("still_path", ""):
                        ep["still_path"] = fallback_img

        return episodes


def get_stats() -> dict[str, Any]:
    """Retrieve database counts for the admin panel."""
    with get_conn() as conn:
        total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        movies_count = conn.execute("SELECT COUNT(*) FROM items WHERE content_type = 'movie'").fetchone()[0]
        tv_count = conn.execute("SELECT COUNT(*) FROM items WHERE content_type = 'tv'").fetchone()[0]
        episodes_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        return {
            "total_items": total_items,
            "movies_count": movies_count,
            "tv_count": tv_count,
            "episodes_count": episodes_count,
            "db_path": str(DB_PATH)
        }

