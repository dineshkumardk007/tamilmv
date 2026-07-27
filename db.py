"""SQLite storage for scraped movie listings."""
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
}

# Every column upsert_movie writes (besides scraped_at).
_FIELDS = [
    "title", "year", "language", "quality", "size", "topic_url", "watch_url",
    "topic_id", "is_official", "has_direct_link", "author", "replies", "views",
    "poster_url",
]


def _migrate(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(movies)")}
    for col, ddl in _EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {col} {ddl}")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert_movie(conn, entry: dict):
    """Insert or update a movie from a scraper entry dict (extra keys ignored)."""
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


def search_movies(query: str, limit: int = 15):
    """Full-text search over cached titles. Falls back to LIKE if FTS query syntax fails."""
    with get_conn() as conn:
        try:
            fts_query = " ".join(f'"{tok}"*' for tok in query.split())
            rows = conn.execute(
                """
                SELECT m.* FROM movies_fts f
                JOIN movies m ON m.id = f.rowid
                WHERE movies_fts MATCH ?
                ORDER BY m.scraped_at DESC
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass

        rows = conn.execute(
            "SELECT * FROM movies WHERE title LIKE ? ORDER BY scraped_at DESC LIMIT ?",
            (f"%{query}%", limit),
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

