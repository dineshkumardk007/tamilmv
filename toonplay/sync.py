"""
toonplay/sync.py — Background catalog ingestion worker for ToonPlay.
Paginates through languages, categories, and series/movies to build a complete offline-first database.
"""
import logging
import time
from typing import Any
import requests

from toonplay import db

logger = logging.getLogger("toonplay.sync")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_API = "https://animesalt.streamindia.co.in/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://toonplay.in/",
    "Accept": "application/json"
}

def sync_catalog(max_pages_per_section: int = 5, fetch_details: bool = True):
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Sync Home
    logger.info("Syncing ToonPlay home...")
    try:
        r = session.get(f"{BASE_API}/home", timeout=12)
        if r.status_code == 200:
            data = r.json().get("data", {})
            for sec in ("hero", "latest_releases", "popular_series", "popular_movies"):
                for item in data.get(sec, []):
                    item["category"] = sec
                    db.upsert_item(item)
    except Exception as exc:
        logger.warning(f"Error syncing home: {exc}")

    # 2. Sync Languages & Categories
    sections = [
        ("tamil", "series"), ("tamil", "movies"),
        ("telugu", "series"), ("telugu", "movies"),
        ("hindi", "series"), ("hindi", "movies"),
        ("english", "series"), ("english", "movies"),
        ("anime", "series"), ("anime", "movies"),
        ("cartoon", "series"), ("cartoon", "movies")
    ]

    all_item_ids = set()

    for sec, ctype in sections:
        logger.info(f"Scanning section: {sec}/{ctype}...")
        for p in range(1, max_pages_per_section + 1):
            url = f"{BASE_API}/{sec}/{ctype}?page={p}"
            try:
                r = session.get(url, timeout=10)
                if r.status_code != 200:
                    break
                res = r.json()
                items = res.get("data", [])
                if not items:
                    break

                for it in items:
                    it_id = it.get("id")
                    if it_id:
                        all_item_ids.add(it_id)
                        it["content_type"] = "movie" if ctype == "movies" else "series"
                        if sec in ("tamil", "telugu", "hindi", "english"):
                            it["languages"] = [sec.title()]
                        elif sec in ("anime", "cartoon"):
                            it["category"] = sec
                        db.upsert_item(it)

                pagination = res.get("pagination", {})
                total_pages = pagination.get("totalPages", 1)
                if p >= total_pages:
                    break
                time.sleep(0.1)
            except Exception as exc:
                logger.warning(f"Error scanning {url}: {exc}")
                break

    logger.info(f"Discovered {len(all_item_ids)} unique titles. Fetching metadata & episodes...")

    if fetch_details:
        for idx, item_id in enumerate(all_item_ids, 1):
            try:
                r = session.get(f"{BASE_API}/info?id={item_id}", timeout=10)
                if r.status_code == 200:
                    anime = r.json().get("anime", {})
                    if anime:
                        db.upsert_item(anime)
                        seasons = anime.get("seasonsList") or []
                        for s in seasons:
                            s_num = int(s.get("season") or 1) if str(s.get("season", "")).isdigit() else 1
                            eps = s.get("episodes") or []
                            if eps:
                                db.upsert_episodes(item_id, s_num, eps)
                if idx % 10 == 0:
                    logger.info(f"Fetched details for {idx}/{len(all_item_ids)} items...")
                time.sleep(0.05)
            except Exception as exc:
                logger.debug(f"Error fetching info for {item_id}: {exc}")

    stats = db.get_stats()
    logger.info(f"Sync complete! Stats: {stats}")
    return stats

if __name__ == "__main__":
    sync_catalog(max_pages_per_section=6, fetch_details=True)

