"""
isaimini/full_sync.py — Exhaustive Background Catalog Sync for Moviesda & isaiDub.
Crawls all uncapped years, series, collections, and A-Z directories into isaimini.db.
"""
import sys
import time
from pathlib import Path

# Ensure local imports
sys.path.insert(0, str(Path(__file__).parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
import scraper


def run_full_sync():
    db.init_db()
    print("=========================================================", flush=True)
    print("Starting Comprehensive Catalog Sync for Moviesda & isaiDub", flush=True)
    print("=========================================================", flush=True)

    with db.get_conn() as conn:
        existing_count = conn.execute("SELECT count(1) FROM movies").fetchone()[0]
    print(f"Initial movies in database: {existing_count}\n", flush=True)

    sites = [
        ("isaiDub (Tamil Dubbed)", "https://isaidub.green"),
        ("Moviesda (Tamil Original)", "https://moviesda.ski"),
    ]

    total_new = 0

    def on_batch(batch):
        nonlocal total_new
        with db.get_conn() as conn:
            added = db.upsert_movies_batch(conn, batch)
            total_new += added

    def on_status(text, count):
        print(f"  {text} (Total Added in session: {total_new})", flush=True)

    for site_label, site_url in sites:
        print(f"\n>>> Crawling full catalog for {site_label} ({site_url}) <<<", flush=True)
        t0 = time.time()
        discovered = scraper.crawl_all_folders(
            folder_type="all",
            site_url=site_url,
            max_pages_per_folder=None,  # Uncapped — crawl ALL pages
            max_workers=10,
            on_movie_batch=on_batch,
            on_folder_status=on_status,
        )
        duration = time.time() - t0
        print(f">>> Finished {site_label}: scanned {discovered} items in {duration:.1f}s <<<\n", flush=True)

    with db.get_conn() as conn:
        final_count = conn.execute("SELECT count(1) FROM movies").fetchone()[0]

    print("=========================================================", flush=True)
    print(f"Sync Complete! Total movies in isaimini.db: {final_count} (+{total_new} added)", flush=True)
    print("=========================================================", flush=True)


if __name__ == "__main__":
    run_full_sync()
