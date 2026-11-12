"""
isaimini/db.py — SQLite database storage with FTS5 search for Moviesda (Isaimini).
"""
import contextlib
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

DB_PATH = Path(__file__).parent / "isaimini.db"


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
        # Migrate old schema where title was UNIQUE
        schema_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='movies'").fetchone()
        if schema_sql and "title TEXT UNIQUE" in schema_sql[0]:
            conn.execute("DROP TRIGGER IF EXISTS movies_ai")
            conn.execute("DROP TRIGGER IF EXISTS movies_ad")
            conn.execute("DROP TRIGGER IF EXISTS movies_au")
            conn.execute("DROP TABLE IF EXISTS movies_fts")
            conn.execute("ALTER TABLE movies RENAME TO movies_old")
            conn.execute("""
                CREATE TABLE movies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    clean_title TEXT,
                    year INTEGER,
                    url TEXT UNIQUE NOT NULL,
                    poster TEXT,
                    director TEXT,
                    starring TEXT,
                    genres TEXT,
                    quality TEXT,
                    language TEXT,
                    rating REAL,
                    synopsis TEXT,
                    content_type TEXT DEFAULT 'movie',
                    last_updated TEXT,
                    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO movies (id, title, clean_title, year, url, poster, director, starring, genres, quality, language, rating, synopsis, content_type, last_updated, scraped_at)
                SELECT id, title, clean_title, year, url, poster, director, starring, genres, quality, language, rating, synopsis, content_type, last_updated, scraped_at FROM movies_old
            """)
            conn.execute("DROP TABLE movies_old")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                clean_title TEXT,
                year INTEGER,
                url TEXT UNIQUE NOT NULL,
                poster TEXT,
                director TEXT,
                starring TEXT,
                genres TEXT,
                quality TEXT,
                language TEXT,
                rating REAL,
                synopsis TEXT,
                content_type TEXT DEFAULT 'movie',
                last_updated TEXT,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS movie_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                quality_label TEXT,
                file_name TEXT,
                file_size TEXT,
                format TEXT,
                resolution TEXT,
                duration TEXT,
                download_page_url TEXT UNIQUE NOT NULL,
                direct_url TEXT,
                cdn_url TEXT,
                watch_online_url TEXT,
                resolved_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                category_type TEXT DEFAULT 'year',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_movies_year ON movies(year)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_movies_content_type ON movies(content_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_movie_id ON movie_files(movie_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_movies_scraped ON movies(scraped_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_movies_year_id ON movies(year DESC, id DESC)")

        # Create FTS5 virtual table for lightning-fast search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS movies_fts USING fts5(
                title,
                clean_title,
                starring,
                director,
                genres,
                content='movies',
                content_rowid='id'
            )
        """)

        # Triggers to keep FTS in sync with movies table
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS movies_ai AFTER INSERT ON movies BEGIN
                INSERT INTO movies_fts(rowid, title, clean_title, starring, director, genres)
                VALUES (new.id, new.title, new.clean_title, new.starring, new.director, new.genres);
            END;
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS movies_ad AFTER DELETE ON movies BEGIN
                INSERT INTO movies_fts(movies_fts, rowid, title, clean_title, starring, director, genres)
                VALUES('delete', old.id, old.title, old.clean_title, old.starring, old.director, old.genres);
            END;
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS movies_au AFTER UPDATE ON movies BEGIN
                INSERT INTO movies_fts(movies_fts, rowid, title, clean_title, starring, director, genres)
                VALUES('delete', old.id, old.title, old.clean_title, old.starring, old.director, old.genres);
                INSERT INTO movies_fts(rowid, title, clean_title, starring, director, genres)
                VALUES (new.id, new.title, new.clean_title, new.starring, new.director, new.genres);
            END;
        """)


def clean_title_str(title: str) -> str:
    """Normalize title by stripping year, resolution, and extra annotations."""
    t = re.sub(r"\(.*?\)|\[.*?\]", "", title)
    t = re.sub(r"\b(720p|1080p|360p|480p|HD|HQ|Original|PreDVD|DVDRip|WEB-DL|Mp4)\b", "", t, flags=re.I)
    return " ".join(t.split()).strip()


def extract_year(text: str) -> int | None:
    """Extract 4-digit release year (1900-2099)."""
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return int(m.group(1)) if m else None


def extract_slug_from_url(url: str) -> str:
    """Extract page slug identifier from URL, stripping movie and web-series suffixes."""
    parts = [p for p in url.strip("/").split("/") if p and not p.startswith("http") and p != "movie"]
    if parts:
        last = parts[-1].replace(".html", "").replace(".xhtml", "")
        slug = re.sub(
            r"-(tamil-dubbed-movie-\d+|tamil-dubbed-movie|tamil-dubbed-web-series-\d+|tamil-dubbed-web-series|tamil-movie-\d+|tamil-movie|tamil-web-series-\d+|tamil-web-series|original-movie|movie-\d+|movie|web-series-\d+|web-series|hd-\d+|hd)$",
            "",
            last,
            flags=re.I,
        )
        return slug
    return ""


def derive_poster_url(url: str) -> str | None:
    """Derive deterministic CDN poster URL from URL slug for Moviesda (isaiDub has no local posters)."""
    slug = extract_slug_from_url(url)
    if slug:
        from . import scraper
        m_base = getattr(scraper, "MOVIESDA_BASE_URL", "https://moviesda.ski").rstrip("/")
        if "isaidub" in url:
            return None  # isaiDub does not host posters at /uploads/posters/; relies on Cinemeta / IMDb!
        elif "moviesda" in url or "isaimini" in url:
            return f"{m_base}/uploads/posters/{slug}.jpg"
    return None


def upsert_movie(conn: sqlite3.Connection, data: dict[str, Any]) -> int:
    """Insert or update a movie record. Returns the movie's database ID."""
    title = (data.get("title") or "").strip()
    url = (data.get("url") or "").strip()
    if not title or not url:
        raise ValueError("Movie must have both 'title' and 'url'")

    clean_title = data.get("clean_title") or clean_title_str(title)
    year = data.get("year") or extract_year(title) or extract_year(url)
    content_type = data.get("content_type", "movie")
    poster = data.get("poster") or derive_poster_url(url)

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO movies (
            title, clean_title, year, url, poster, director, starring,
            genres, quality, language, rating, synopsis, content_type, last_updated
        ) VALUES (
            :title, :clean_title, :year, :url, :poster, :director, :starring,
            :genres, :quality, :language, :rating, :synopsis, :content_type, :last_updated
        )
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            clean_title = excluded.clean_title,
            year = COALESCE(excluded.year, movies.year),
            poster = COALESCE(excluded.poster, movies.poster),
            director = COALESCE(excluded.director, movies.director),
            starring = COALESCE(excluded.starring, movies.starring),
            genres = COALESCE(excluded.genres, movies.genres),
            quality = COALESCE(excluded.quality, movies.quality),
            language = COALESCE(excluded.language, movies.language),
            rating = COALESCE(excluded.rating, movies.rating),
            synopsis = COALESCE(excluded.synopsis, movies.synopsis),
            content_type = COALESCE(excluded.content_type, movies.content_type),
            last_updated = COALESCE(excluded.last_updated, CURRENT_TIMESTAMP),
            scraped_at = CURRENT_TIMESTAMP
        RETURNING id
    """, {
        "title": title,
        "clean_title": clean_title,
        "year": year,
        "url": url,
        "poster": poster,
        "director": data.get("director"),
        "starring": data.get("starring"),
        "genres": data.get("genres"),
        "quality": data.get("quality"),
        "language": data.get("language") or "Tamil",
        "rating": float(data["rating"]) if data.get("rating") else None,
        "synopsis": data.get("synopsis"),
        "content_type": content_type,
        "last_updated": data.get("last_updated"),
    })
    row = cur.fetchone()
    return row[0] if row else 0


def upsert_movies_batch(conn: sqlite3.Connection, movies_list: list[dict[str, Any]]) -> int:
    """High-speed batch upsert of multiple movies in a single transaction."""
    if not movies_list:
        return 0
    params = []
    seen_urls = set()
    for data in movies_list:
        title = (data.get("title") or "").strip()
        url = (data.get("url") or "").strip()
        if not title or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        clean_title = data.get("clean_title") or clean_title_str(title)
        year = data.get("year") or extract_year(title) or extract_year(url)
        content_type = data.get("content_type", "movie")
        poster = data.get("poster") or derive_poster_url(url)
        params.append({
            "title": title,
            "clean_title": clean_title,
            "year": year,
            "url": url,
            "poster": poster,
            "director": data.get("director"),
            "starring": data.get("starring"),
            "genres": data.get("genres"),
            "quality": data.get("quality"),
            "language": data.get("language") or "Tamil",
            "rating": float(data["rating"]) if data.get("rating") else None,
            "synopsis": data.get("synopsis"),
            "content_type": content_type,
            "last_updated": data.get("last_updated"),
        })

    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO movies (
            title, clean_title, year, url, poster, director, starring,
            genres, quality, language, rating, synopsis, content_type, last_updated
        ) VALUES (
            :title, :clean_title, :year, :url, :poster, :director, :starring,
            :genres, :quality, :language, :rating, :synopsis, :content_type, :last_updated
        )
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            clean_title = excluded.clean_title,
            year = COALESCE(excluded.year, movies.year),
            poster = COALESCE(excluded.poster, movies.poster),
            director = COALESCE(excluded.director, movies.director),
            starring = COALESCE(excluded.starring, movies.starring),
            genres = COALESCE(excluded.genres, movies.genres),
            quality = COALESCE(excluded.quality, movies.quality),
            language = COALESCE(excluded.language, movies.language),
            rating = COALESCE(excluded.rating, movies.rating),
            synopsis = COALESCE(excluded.synopsis, movies.synopsis),
            content_type = COALESCE(excluded.content_type, movies.content_type),
            last_updated = COALESCE(excluded.last_updated, CURRENT_TIMESTAMP),
            scraped_at = CURRENT_TIMESTAMP
    """, params)
    return len(params)


def upsert_file(conn: sqlite3.Connection, movie_id: int, file_data: dict[str, Any]) -> int:
    """Insert or update a downloadable file entry for a movie."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO movie_files (
            movie_id, quality_label, file_name, file_size, format,
            resolution, duration, download_page_url, direct_url, cdn_url,
            watch_online_url, resolved_at
        ) VALUES (
            :movie_id, :quality_label, :file_name, :file_size, :format,
            :resolution, :duration, :download_page_url, :direct_url, :cdn_url,
            :watch_online_url, :resolved_at
        )
        ON CONFLICT(download_page_url) DO UPDATE SET
            quality_label = COALESCE(excluded.quality_label, movie_files.quality_label),
            file_name = COALESCE(excluded.file_name, movie_files.file_name),
            file_size = COALESCE(excluded.file_size, movie_files.file_size),
            format = COALESCE(excluded.format, movie_files.format),
            resolution = COALESCE(excluded.resolution, movie_files.resolution),
            duration = COALESCE(excluded.duration, movie_files.duration),
            direct_url = COALESCE(excluded.direct_url, movie_files.direct_url),
            cdn_url = COALESCE(excluded.cdn_url, movie_files.cdn_url),
            watch_online_url = COALESCE(excluded.watch_online_url, movie_files.watch_online_url),
            resolved_at = COALESCE(excluded.resolved_at, movie_files.resolved_at)
        RETURNING id
    """, {
        "movie_id": movie_id,
        "quality_label": file_data.get("quality_label"),
        "file_name": file_data.get("file_name"),
        "file_size": file_data.get("file_size"),
        "format": file_data.get("format") or "Mp4",
        "resolution": file_data.get("resolution"),
        "duration": file_data.get("duration"),
        "download_page_url": file_data.get("download_page_url"),
        "direct_url": file_data.get("direct_url"),
        "cdn_url": file_data.get("cdn_url"),
        "watch_online_url": file_data.get("watch_online_url"),
        "resolved_at": file_data.get("resolved_at"),
    })
    row = cur.fetchone()

    # Automatically set content_type = 'series' if any file contains episodic indicators
    file_name = file_data.get("file_name") or ""
    if re.search(r"\b(season|epi|episode|s\d+|ep\d+)\b", file_name, re.I):
        cur.execute("UPDATE movies SET content_type = 'series' WHERE id = ? AND content_type != 'series'", (movie_id,))

    return row[0] if row else 0


def update_resolved_file(download_page_url: str, resolved_data: dict[str, Any]) -> None:
    """Update direct_url and metadata for an already discovered file entry."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE movie_files SET
                direct_url = COALESCE(?, direct_url),
                cdn_url = COALESCE(?, cdn_url),
                watch_online_url = COALESCE(?, watch_online_url),
                file_name = COALESCE(?, file_name),
                file_size = COALESCE(?, file_size),
                resolution = COALESCE(?, resolution),
                duration = COALESCE(?, duration),
                format = COALESCE(?, format),
                resolved_at = CURRENT_TIMESTAMP
            WHERE download_page_url = ?
        """, (
            resolved_data.get("direct_url"),
            resolved_data.get("cdn_url"),
            resolved_data.get("watch_online_url"),
            resolved_data.get("file_name"),
            resolved_data.get("file_size"),
            resolved_data.get("resolution"),
            resolved_data.get("duration"),
            resolved_data.get("format"),
            download_page_url,
        ))


def update_movie_content_type(movie_id: int, content_type: str) -> None:
    """Update content_type ('movie' or 'series') for a movie."""
    with get_conn() as conn:
        conn.execute("UPDATE movies SET content_type = ? WHERE id = ?", (content_type, movie_id))


def search_movies(query: str, limit: int = 50, site: str | None = None) -> list[dict]:
    """Search movies using FTS5 full-text search with fallback to LIKE and optional site filter."""
    q = query.strip()
    if not q:
        return get_recent_movies(limit=limit, site=site)

    clean_q = re.sub(r"[^\w\s]", "", q).strip()
    words = clean_q.split()

    with get_conn() as conn:
        results = []
        site_clause = ""
        site_params = []
        if site == "isaidub":
            site_clause = " AND m.url LIKE ?"
            site_params.append("%isaidub%")
        elif site in ("isaimini", "moviesda"):
            site_clause = " AND m.url LIKE ?"
            site_params.append("%moviesda%")

        if words:
            fts_query = " ".join(f'"{w}"*' for w in words)
            try:
                cur = conn.execute(f"""
                    SELECT m.*, rank
                    FROM movies_fts f
                    JOIN movies m ON m.id = f.rowid
                    WHERE movies_fts MATCH ? {site_clause}
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, *site_params, limit))
                results = [dict(r) for r in cur.fetchall()]
            except Exception:
                results = []

        # Fallback to standard LIKE if FTS produced 0 results
        if not results:
            like_site_clause = ""
            like_params = [f"%{q}%", f"%{q}%", f"%{q}%"]
            if site == "isaidub":
                like_site_clause = " AND url LIKE ?"
                like_params.append("%isaidub%")
            elif site in ("isaimini", "moviesda"):
                like_site_clause = " AND url LIKE ?"
                like_params.append("%moviesda%")

            cur = conn.execute(f"""
                SELECT * FROM movies
                WHERE (title LIKE ? OR starring LIKE ? OR clean_title LIKE ?) {like_site_clause}
                ORDER BY year DESC, id DESC
                LIMIT ?
            """, (*like_params, limit))
            results = [dict(r) for r in cur.fetchall()]

        # Attach source metadata tags
        for m in results:
            url_str = m.get("url") or ""
            m["source"] = "isaidub" if "isaidub" in url_str else "isaimini"
            m["source_label"] = "TamDub (Dubbed)" if m["source"] == "isaidub" else "TamilMov (Original)"
            m["is_direct"] = True

        return results


def get_recent_movies(
    limit: int = 40,
    offset: int = 0,
    content_type: str | None = None,
    year: int | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    site: str | None = None,
    language: str | None = None,
    quality: str | None = None,
) -> list[dict]:
    """Retrieve recently scraped or updated releases with optional site filter."""
    query = "SELECT * FROM movies WHERE 1=1"
    params: list[Any] = []

    if content_type and content_type.lower() != "all":
        query += " AND content_type = ?"
        params.append(content_type)
    if start_year and end_year:
        query += " AND year >= ? AND year <= ?"
        params.extend([int(start_year), int(end_year)])
    elif year:
        query += " AND year = ?"
        params.append(year)
    if language and language.lower() != "all":
        query += " AND (language LIKE ? OR title LIKE ?)"
        params.extend([f"%{language}%", f"%{language}%"])
    if quality and quality.lower() != "all":
        query += " AND (quality LIKE ? OR title LIKE ?)"
        params.extend([f"%{quality}%", f"%{quality}%"])
    if site == "isaidub":
        query += " AND url LIKE ?"
        params.append("%isaidub%")
    elif site in ("isaimini", "moviesda"):
        query += " AND url LIKE ?"
        params.append("%moviesda%")

    query += " ORDER BY year DESC, COALESCE(last_updated, scraped_at) DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_conn() as conn:
        cur = conn.execute(query, params)
        results = []
        for r in cur.fetchall():
            m = dict(r)
            url_str = m.get("url") or ""
            m["source"] = "isaidub" if "isaidub" in url_str else "isaimini"
            m["source_label"] = "TamDub (Dubbed)" if m["source"] == "isaidub" else "TamilMov (Original)"
            m["is_direct"] = True
            results.append(m)
        return results


def get_movie(movie_id_or_url: int | str) -> dict | None:
    """Get single movie by ID or URL."""
    with get_conn() as conn:
        if isinstance(movie_id_or_url, int) or (isinstance(movie_id_or_url, str) and movie_id_or_url.isdigit()):
            cur = conn.execute("SELECT * FROM movies WHERE id = ?", (int(movie_id_or_url),))
        else:
            cur = conn.execute("SELECT * FROM movies WHERE url = ?", (str(movie_id_or_url),))
        row = cur.fetchone()
        return dict(row) if row else None


def get_movie_files(movie_id: int) -> list[dict]:
    """Get all file downloads recorded for a specific movie."""
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM movie_files WHERE movie_id = ? ORDER BY id ASC", (movie_id,))
        return [dict(r) for r in cur.fetchall()]


def get_stats() -> dict:
    """Return summary counts of cached movies and files."""
    with get_conn() as conn:
        total_movies = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
        total_files = conn.execute("SELECT COUNT(*) FROM movie_files").fetchone()[0]
        resolved_files = conn.execute("SELECT COUNT(*) FROM movie_files WHERE direct_url IS NOT NULL").fetchone()[0]
        series_count = conn.execute("SELECT COUNT(*) FROM movies WHERE content_type = 'series'").fetchone()[0]
        years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM movies WHERE year IS NOT NULL ORDER BY year DESC").fetchall()]
        return {
            "total_movies": total_movies,
            "total_files": total_files,
            "resolved_direct_links": resolved_files,
            "web_series_count": series_count,
            "years": years,
        }


def update_movie_poster(movie_id_or_url: int | str, poster_url: str):
    """Update or backfill poster URL for a movie record."""
    with get_conn() as conn:
        if isinstance(movie_id_or_url, int) or (isinstance(movie_id_or_url, str) and movie_id_or_url.isdigit()):
            conn.execute("UPDATE movies SET poster = ? WHERE id = ?", (poster_url, int(movie_id_or_url)))
        else:
            conn.execute("UPDATE movies SET poster = ? WHERE url = ?", (poster_url, str(movie_id_or_url)))


def sync_latest_releases() -> int:
    """
    Fetch the latest additions from both Moviesda (TamilMov) and isaiDub (TamDub)
    concurrently across all feeds using ThreadPoolExecutor, and batch upsert into isaimini.db.
    """
    from . import scraper
    import datetime
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import logging

    logger = logging.getLogger("isaimini.sync")
    cur_year = datetime.datetime.now().year

    m_base = getattr(scraper, "MOVIESDA_BASE_URL", "https://moviesda.ski").rstrip("/")
    d_base = getattr(scraper, "ISAIDUB_BASE_URL", "https://isaidub.green").rstrip("/")

    tasks = [
        # (function, task_name)
        (lambda: scraper.fetch_latest_updates(site_url=m_base), "moviesda_ticker"),
        (lambda: scraper.fetch_category_movies(f"{m_base}/tamil-{cur_year}-movies/", page=1).get("movies", []), "moviesda_cur_year"),
        (lambda: scraper.fetch_category_movies(f"{m_base}/tamil-web-series/", page=1).get("movies", []), "moviesda_series"),
        (lambda: scraper.fetch_latest_updates(site_url=d_base), "isaidub_ticker"),
        (lambda: scraper.fetch_category_movies(f"{d_base}/tamil-{cur_year}-dubbed-movies/", page=1).get("movies", []), "isaidub_cur_year"),
        (lambda: scraper.fetch_category_movies(f"{d_base}/tamil-dubbed-web-series/", page=1).get("movies", []), "isaidub_series"),
    ]

    all_movies = []
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="sync_releases") as executor:
        future_to_name = {executor.submit(task_fn): name for task_fn, name in tasks}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result()
                if result and isinstance(result, list):
                    all_movies.extend(result)
            except Exception as exc:
                logger.debug(f"Sync task '{name}' error: {exc}")

    if not all_movies:
        return 0

    with get_conn() as conn:
        return upsert_movies_batch(conn, all_movies)
