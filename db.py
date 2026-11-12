import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "movies.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    year INTEGER,
    language TEXT,
    quality TEXT,
    size TEXT,
    topic_url TEXT NOT NULL UNIQUE,
    watch_url TEXT,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS movies_fts USING fts5(
    title,
    content='movies',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS movies_ai AFTER INSERT ON movies BEGIN
    INSERT INTO movies_fts(rowid, title) VALUES (new.id, new.title);
END;

CREATE TRIGGER IF NOT EXISTS movies_ad AFTER DELETE ON movies BEGIN
    INSERT INTO movies_fts(movies_fts, rowid, title) VALUES ('delete', old.id, old.title);
END;

CREATE TRIGGER IF NOT EXISTS movies_au AFTER UPDATE ON movies BEGIN
    INSERT INTO movies_fts(movies_fts, rowid, title) VALUES ('delete', old.id, old.title);
    INSERT INTO movies_fts(rowid, title) VALUES (new.id, new.title);
END;
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the initial release; applied via _migrate() so existing
# movies.db files keep working.
_EXTRA_COLUMNS = {
    "topic_id": "INTEGER",
    "is_official": "INTEGER",
    "has_direct_link": "INTEGER",
    "author": "TEXT",
    "replies": "INTEGER",
    "views": "INTEGER",
    "poster_url": "TEXT",
    "is_top_week": "INTEGER",
    "content_type": "TEXT",
}

# Every column upsert_movie writes (besides scraped_at).
_FIELDS = [
    "title", "year", "language", "quality", "size", "topic_url", "watch_url",
    "topic_id", "is_official", "has_direct_link", "author", "replies", "views",
    "poster_url", "is_top_week", "content_type",
]


def _migrate(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(movies)")}
    for col, ddl in _EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {col} {ddl}")

    # Auto-classify existing null content_type rows as series or movie
    conn.execute(
        """
        UPDATE movies
        SET content_type = CASE
            WHEN title LIKE '% S0%' OR title LIKE '% S1%' OR title LIKE '% S2%' OR title LIKE '% Season%'
                 OR title LIKE '% EP%' OR title LIKE '% Episode%' OR topic_url LIKE '%-s0%' OR topic_url LIKE '%-season%'
                 THEN 'series'
            ELSE 'movie'
        END
        WHERE content_type IS NULL OR content_type = ''
        """
    )


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert_movie(conn, entry: dict):
    """Insert or update a movie from a scraper entry dict (extra keys ignored)."""
    # Auto-extract topic_id if missing
    if not entry.get("topic_id") and entry.get("topic_url"):
        m = re.search(r"/topic/(\d+)", entry["topic_url"])
        if m:
            entry["topic_id"] = int(m.group(1))

    # If topic_id exists, update existing record if already present
    if entry.get("topic_id"):
        existing = conn.execute(
            "SELECT id FROM movies WHERE topic_id = ?",
            (entry["topic_id"],),
        ).fetchone()
        if existing:
            updates = []
            params = []
            for f in _FIELDS:
                if f in ("topic_id", "topic_url"):
                    continue
                val = entry.get(f)
                if val is not None:
                    if isinstance(val, bool):
                        val = int(val)
                    updates.append(f"{f} = ?")
                    params.append(val)
            if updates:
                updates.append("scraped_at = datetime('now')")
                params.append(existing["id"])
                conn.execute(f"UPDATE movies SET {', '.join(updates)} WHERE id = ?", params)
            return

    values = [entry.get(f) for f in _FIELDS]
    # SQLite has no bool type; normalise to 0/1
    for i, f in enumerate(_FIELDS):
        if isinstance(values[i], bool):
            values[i] = int(values[i])

    cols = ", ".join(_FIELDS)
    placeholders = ", ".join("?" for _ in _FIELDS)
    updates = ", ".join(
        f"{f}=excluded.{f}" for f in _FIELDS if f != "topic_url"
    )
    conn.execute(
        f"""
        INSERT INTO movies ({cols}, scraped_at)
        VALUES ({placeholders}, datetime('now'))
        ON CONFLICT(topic_url) DO UPDATE SET
            {updates},
            scraped_at=excluded.scraped_at
        """,
        values,
    )


def search_movies(query: str, limit: int = 40):
    """Full-text search over cached titles. Falls back to LIKE if FTS query syntax fails."""
    with get_conn() as conn:
        try:
            fts_query = " ".join(f'"{tok}"*' for tok in query.split())
            rows = conn.execute(
                """
                SELECT m.* FROM movies_fts f
                JOIN movies m ON m.id = f.rowid
                WHERE movies_fts MATCH ? AND substr(m.title, 1, 1) != '['
                ORDER BY (m.topic_id IS NULL), m.topic_id DESC, m.scraped_at DESC
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        rows = conn.execute(
            "SELECT * FROM movies WHERE title LIKE ? AND substr(title, 1, 1) != '[' ORDER BY (topic_id IS NULL), topic_id DESC, scraped_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_movies(
    limit: int = 50,
    offset: int = 0,
    language: str | None = None,
    quality: str | None = None,
    sort_by: str = "recent",
    content_type: str | None = None,
    year: int | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
) -> list[dict]:
    """Retrieve recently scraped or popular movies with optional filtering and sorting."""
    with get_conn() as conn:
        conditions = ["substr(title, 1, 1) != '['"]
        params = []
        if start_year and end_year:
            conditions.append("year >= ? AND year <= ?")
            params.extend([int(start_year), int(end_year)])
        elif year:
            conditions.append("year = ?")
            params.append(int(year))
        if language and language.lower() != "all":
            conditions.append("(language LIKE ? OR title LIKE ?)")
            params.extend([f"%{language}%", f"%{language}%"])
        if quality and quality.lower() != "all":
            conditions.append("(quality LIKE ? OR title LIKE ?)")
            params.extend([f"%{quality}%", f"%{quality}%"])
        if content_type and content_type.lower() != "all":
            if content_type.lower() == "series":
                conditions.append("content_type = 'series'")
            elif content_type.lower() == "movie":
                conditions.append("(content_type = 'movie' OR content_type IS NULL)")

        where_clause = f"WHERE {' AND '.join(conditions)}"

        if sort_by == "top_week":
            order_clause = "ORDER BY (CASE WHEN is_top_week = 1 THEN 1 ELSE 0 END) DESC, (topic_id IS NULL), topic_id DESC, id DESC"
        elif sort_by == "views":
            order_clause = "ORDER BY (views IS NULL), views DESC, (topic_id IS NULL), topic_id DESC, id DESC"
        elif sort_by == "replies":
            order_clause = "ORDER BY (replies IS NULL), replies DESC, (topic_id IS NULL), topic_id DESC, id DESC"
        elif sort_by == "year":
            order_clause = "ORDER BY (year IS NULL), year DESC, (topic_id IS NULL), topic_id DESC, id DESC"
        else:
            order_clause = "ORDER BY (topic_id IS NULL), topic_id DESC, scraped_at DESC, id DESC"

        sql = f"SELECT * FROM movies {where_clause} {order_clause} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_movies_by_urls(topic_urls: list[str]) -> list[dict]:
    """Retrieve movie records matching a list of topic URLs."""
    if not topic_urls:
        return []
    placeholders = ",".join("?" for _ in topic_urls)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM movies WHERE topic_url IN ({placeholders}) ORDER BY id DESC",
            topic_urls,
        ).fetchall()
        return [dict(r) for r in rows]


def count_movies() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM movies").fetchone()["c"]


def update_poster_url(conn, topic_url: str, poster_url: str):
    """Update poster_url for an existing topic_url entry."""
    conn.execute(
        "UPDATE movies SET poster_url = ? WHERE topic_url = ?",
        (poster_url, topic_url),
    )


# ==============================================================================
# Custom Cloud Vault (Google Drive, Pixeldrain, Dropbox, Direct Links)
# ==============================================================================

def init_vault_table():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_vault (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                year INTEGER,
                original_url TEXT NOT NULL,
                stream_url TEXT NOT NULL,
                poster TEXT,
                backdrop TEXT,
                overview TEXT,
                rating TEXT,
                quality TEXT DEFAULT '1080p',
                source_type TEXT DEFAULT 'cloud',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def add_vault_movie(
    title: str,
    year: int | None,
    original_url: str,
    stream_url: str,
    poster: str = "",
    backdrop: str = "",
    overview: str = "",
    rating: str = "",
    quality: str = "1080p",
    source_type: str = "cloud"
) -> int:
    init_vault_table()
    with get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO custom_vault (title, year, original_url, stream_url, poster, backdrop, overview, rating, quality, source_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, year, original_url, stream_url, poster, backdrop, overview, rating, quality, source_type))
        return cursor.lastrowid


def get_vault_movies() -> list[dict]:
    init_vault_table()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM custom_vault ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def get_vault_movie_by_id(vault_id: int) -> dict | None:
    init_vault_table()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM custom_vault WHERE id = ?", (vault_id,)).fetchone()
        return dict(row) if row else None


def delete_vault_movie(vault_id: int):
    init_vault_table()
    with get_conn() as conn:
        conn.execute("DELETE FROM custom_vault WHERE id = ?", (vault_id,))


def init_continue_watching_table():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS continue_watching (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                year TEXT,
                poster_url TEXT,
                content_type TEXT,
                series_id TEXT,
                season_num INTEGER,
                episode_num INTEGER,
                episode_title TEXT,
                stream_url TEXT,
                source TEXT,
                current_time REAL DEFAULT 0,
                duration REAL DEFAULT 0,
                progress_pct REAL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def save_continue_watching(entry: dict):
    init_continue_watching_table()
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO continue_watching (
                id, title, year, poster_url, content_type,
                series_id, season_num, episode_num, episode_title,
                stream_url, source, current_time, duration,
                progress_pct, updated_at
            ) VALUES (
                :id, :title, :year, :poster_url, :content_type,
                :series_id, :season_num, :episode_num, :episode_title,
                :stream_url, :source, :current_time, :duration,
                :progress_pct, CURRENT_TIMESTAMP
            )
        """, entry)


def get_continue_watching(limit: int = 24) -> list[dict]:
    init_continue_watching_table()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM continue_watching
            WHERE progress_pct < 95
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def delete_continue_watching(item_id: str):
    init_continue_watching_table()
    with get_conn() as conn:
        conn.execute("DELETE FROM continue_watching WHERE id = ?", (item_id,))


def clear_continue_watching():
    init_continue_watching_table()
    with get_conn() as conn:
        conn.execute("DELETE FROM continue_watching")




