"""
isaimini/cli.py — Command-Line Interface for Moviesda (Isaimini) Scraper & Downloader.

Usage examples:
  python isaimini/cli.py latest
  python isaimini/cli.py sync --year 2026 --pages 2
  python isaimini/cli.py search "418"
  python isaimini/cli.py info "https://moviesda.ski/418-2026-tamil-movie/"
  python isaimini/cli.py links "https://moviesda.ski/418-2026-tamil-movie/"
  python isaimini/cli.py stats
  python isaimini/cli.py (interactive menu)
"""
import argparse
import json
import sys
from pathlib import Path

# Ensure local imports work cleanly
sys.path.insert(0, str(Path(__file__).parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import db
import scraper


def cmd_latest(args):
    """Fetch and display latest updates directly from the homepage ticker."""
    print("\nFetching latest additions from Moviesda homepage...\n")
    updates = scraper.fetch_latest_updates()
    if not updates:
        print("No updates found.")
        return

    print(f"{'#':<3} | {'Type':<8} | {'Year':<6} | {'Title'}")
    print("-" * 75)
    for idx, item in enumerate(updates, start=1):
        ctype = item.get("content_type", "movie")
        yr = str(item.get("year") or "-")
        print(f"{idx:<3} | {ctype:<8} | {yr:<6} | {item['title']}")
        if item.get("url"):
            print(f"    URL: {item['url']}")
    print(f"\nTotal: {len(updates)} recent updates.\n")


def cmd_search(args):
    """Search for movies across the SQLite database with live folder search fallback."""
    db.init_db()
    query = args.query.strip()
    print(f"\nSearching database for '{query}'...")
    results = db.search_movies(query, limit=args.limit)

    is_live = getattr(args, "live", False)
    is_full = getattr(args, "full", False)

    if not results or is_live or is_full:
        if is_full:
            print(f"Executing Full Site Exhaustive Search for '{query}' across all 26 letters & folders...")
        elif not results:
            print(f"No matches in local database. Searching live website folders for '{query}'...")
        else:
            print("Searching live website folders for additional matches...")

        site_val = getattr(args, "site", None)
        target_site_url = None
        if site_val == "isaidub":
            target_site_url = "https://isaidub.green"
        elif site_val == "moviesda":
            target_site_url = "https://moviesda.ski"

        def _on_crawled(batch):
            with db.get_conn() as conn:
                db.upsert_movies_batch(conn, batch)

        live_matches = scraper.search_site(
            query,
            site_url=target_site_url,
            max_pages=getattr(args, "pages", None),
            full_crawl=is_full,
            on_folder_crawled=_on_crawled,
        )

        if live_matches:
            print(f"Found {len(live_matches)} movie(s) across folders! Saved to database.")
            results = db.search_movies(query, limit=args.limit)

    if not results:
        print(f"No matches found for '{query}'.\n")
        return

    print(f"\nFound {len(results)} match(es):\n")
    print(f"{'ID':<4} | {'Site':<8} | {'Year':<6} | {'Type':<7} | {'Title'}")
    print("-" * 75)
    for m in results:
        mid = str(m.get("id", "-"))
        yr = str(m.get("year") or "-")
        ctype = m.get("content_type", "movie")
        url = m.get("url", "")
        site_name = "isaiDub" if "isaidub" in url else "Moviesda"
        print(f"{mid:<4} | {site_name:<8} | {yr:<6} | {ctype:<7} | {m.get('title')}")
        print(f"     URL: {url}")
        if m.get("starring"):
            print(f"     Cast: {m.get('starring')}")
    print()


def cmd_crawl_all(args):
    """Index movies from all folders on moviesda.ski or isaidub.green concurrently into SQLite."""
    db.init_db()
    folder_type = getattr(args, "type", "all")
    max_pages = getattr(args, "pages", None)
    site = getattr(args, "site", "all")

    sites_to_crawl = []
    if site in ("moviesda", "all"):
        sites_to_crawl.append(("Moviesda", "https://moviesda.ski"))
    if site in ("isaidub", "all"):
        sites_to_crawl.append(("isaiDub", "https://isaidub.green"))

    def on_batch(movies):
        with db.get_conn() as conn:
            db.upsert_movies_batch(conn, movies)

    total_indexed = 0
    for site_label, site_url in sites_to_crawl:
        print(f"\n[Multi-Folder Sync] Target: {site_label} ({site_url}) | Type: '{folder_type}' | Max pages: {max_pages or 'All'}")

        def on_status(status_text, count):
            print(f"  [{site_label}] --> {status_text} (Total indexed: {count})")

        total = scraper.crawl_all_folders(
            folder_type=folder_type,
            site_url=site_url,
            max_pages_per_folder=max_pages,
            max_workers=getattr(args, "workers", 6),
            on_movie_batch=on_batch,
            on_folder_status=on_status,
        )
        total_indexed += total
        print(f"[{site_label} Sync Complete] Indexed {total} movies.")

    print(f"\n[Total Sync Complete] Successfully indexed {total_indexed} movies into SQLite database!\n")


def cmd_sync(args):
    """Sync movie catalog for a given year or URL into SQLite."""
    db.init_db()
    year = args.year
    max_pages = args.pages
    site = getattr(args, "site", "moviesda")
    if site == "isaidub":
        category_url = f"https://isaidub.green/tamil-{year}-dubbed-movies/"
    else:
        category_url = f"{scraper.BASE_URL}/tamil-{year}-movies/"

    print(f"\n[Sync] Scraping Tamil {year} Movies from: {category_url}")
    print(f"[Sync] Max pages to fetch: {max_pages}\n")

    total_added = 0
    session = scraper.create_session()

    for page in range(1, max_pages + 1):
        print(f"--- Scraping Page {page} / {max_pages} ---")
        try:
            page_data = scraper.fetch_category_movies(category_url, page=page, session=session)
            movies = page_data.get("movies", [])
            print(f"Found {len(movies)} movies on page {page}.")

            with db.get_conn() as conn:
                for m in movies:
                    try:
                        # Fetch full metadata if details flag is set
                        if args.with_details:
                            print(f"  Fetching details for: {m['title']}...")
                            full_meta = scraper.fetch_movie_details(m["url"], session=session)
                            db.upsert_movie(conn, full_meta)
                        else:
                            db.upsert_movie(conn, m)
                        total_added += 1
                    except Exception as row_err:
                        print(f"  Error storing {m.get('title')}: {row_err}")

            if not page_data.get("has_next") or page >= page_data.get("total_pages", 1):
                print("Reached the last available page.")
                break

        except Exception as page_err:
            print(f"Failed to fetch page {page}: {page_err}")
            break

    print(f"\n[Sync Finished] Successfully added/updated {total_added} movies in SQLite.\n")


def cmd_info(args):
    """Scrape and display rich metadata for a specific movie URL."""
    url = args.url.strip()
    print(f"\nFetching movie info from: {url}\n")
    data = scraper.fetch_movie_details(url)

    print("=" * 60)
    print(f" Title:        {data.get('title')}")
    print(f" Year:         {data.get('year')}")
    print(f" Type:         {data.get('content_type')}")
    print(f" Rating:       {data.get('rating') or 'N/A'}")
    print(f" Director:     {data.get('director') or 'N/A'}")
    print(f" Cast:         {data.get('starring') or 'N/A'}")
    print(f" Genres:       {data.get('genres') or 'N/A'}")
    print(f" Quality:      {data.get('quality') or 'N/A'}")
    print(f" Language:     {data.get('language') or 'N/A'}")
    print(f" Last Updated: {data.get('last_updated') or 'N/A'}")
    if data.get("poster"):
        print(f" Poster URL:   {data.get('poster')}")
    if data.get("synopsis"):
        print(f"\n Synopsis:\n {data.get('synopsis')}")
    if data.get("folders"):
        print("\n Version / Quality Subfolders:")
        for f in data["folders"]:
            print(f"   - {f['title']}: {f['url']}")
    print("=" * 60 + "\n")


def cmd_links(args):
    """Discover all downloadable files for a movie and optionally resolve direct MP4 links."""
    url = args.url.strip()
    print(f"\nDiscovering download options for: {url}...")
    files = scraper.fetch_movie_download_options(url)

    if not files:
        print("No downloadable files found under this movie URL.")
        return

    print(f"\nDiscovered {len(files)} file variant(s):\n")
    db.init_db()

    for idx, f in enumerate(files, start=1):
        print(f"[{idx}] {f.get('file_name')}")
        print(f"    Quality: {f.get('quality_label')} | Size: {f.get('file_size')} | Format: {f.get('format')}")
        print(f"    Page URL: {f.get('download_page_url')}")

        if args.resolve:
            print("    Resolving 3-stage anti-leech gateway...")
            try:
                resolved = scraper.resolve_download_link(f["download_page_url"])
                print(f"    --> DIRECT MP4:     {resolved.get('direct_url')}")
                if resolved.get("watch_online_url"):
                    print(f"    --> WATCH ONLINE:   {resolved.get('watch_online_url')}")
                if resolved.get("duration"):
                    print(f"    --> Duration:       {resolved.get('duration')} | Res: {resolved.get('resolution')}")

                # Save resolved link to DB if movie is known
                movie_record = db.get_movie(url)
                if movie_record:
                    with db.get_conn() as conn:
                        db.upsert_file(conn, movie_record["id"], {**f, **resolved})
            except Exception as res_err:
                print(f"    [Error] Failed to resolve link: {res_err}")

        print("-" * 65)
    print()


def cmd_stats(args):
    """Display SQLite database statistics."""
    db.init_db()
    stats = db.get_stats()
    print("\n=== Isaimini SQLite Database Stats ===")
    print(f"  Total Movies:           {stats.get('total_movies')}")
    print(f"  Total Files Indexed:    {stats.get('total_files')}")
    print(f"  Resolved Direct Links:  {stats.get('resolved_direct_links')}")
    print(f"  Web Series:             {stats.get('web_series_count')}")
    print(f"  Years in Database:      {', '.join(str(y) for y in stats.get('years', [])) or 'None'}")
    print("======================================\n")


def interactive_menu():
    """Simple interactive CLI menu if script is run with no arguments."""
    db.init_db()
    while True:
        print("\n=== Moviesda & isaiDub Multi-Site Scraper Menu ===")
        print("1. Search movie (Local Database + Live Folders Auto-Search across Moviesda & isaiDub)")
        print("2. View latest additions ticker")
        print("3. Crawl & Index Folders (Moviesda.ski / isaiDub.green)")
        print("4. Sync catalog by specific Year (e.g. 2026, 2025)")
        print("5. Inspect movie details by URL")
        print("6. Get & Resolve direct MP4 download links")
        print("7. Show database statistics")
        print("0. Exit")
        choice = input("\nEnter choice (0-7): ").strip()

        if choice == "1":
            q = input("Enter movie name to search: ").strip()
            if q:
                res = db.search_movies(q, limit=25)
                if res:
                    cmd_search(argparse.Namespace(query=q, limit=25, live=False, full=False, site=None, pages=None))
                else:
                    print(f"\nNo local match for '{q}'.")
                    print("Live search modes: [1] Smart Alphabet & Year Search (Fast) | [2] Full Exhaustive Site Search (All 26 Letters)")
                    sm = input("Select search mode (1-2, default 1): ").strip() or "1"
                    full_flag = True if sm == "2" else False
                    cmd_search(argparse.Namespace(query=q, limit=25, live=True, full=full_flag, site=None, pages=None))
        elif choice == "2":
            cmd_latest(None)
        elif choice == "3":
            print("\nSelect Site to Sync:")
            print("  [1] Moviesda.ski (Tamil Original Movies)")
            print("  [2] isaiDub.green (Tamil Dubbed Movies)")
            print("  [3] Both Sites")
            site_pick = input("Select site (1-3, default 1): ").strip() or "1"
            site_arg = "moviesda" if site_pick == "1" else ("isaidub" if site_pick == "2" else "all")

            print("\nSync options: [1] Quick Sync (Years 2024-2026) | [2] All Years | [3] Complete Alphabetical (A-Z) | [4] Everything")
            sub_c = input("Select (1-4, default 1): ").strip() or "1"
            if sub_c == "1":
                cmd_crawl_all(argparse.Namespace(site=site_arg, type="years", pages=3, workers=6))
            elif sub_c == "2":
                cmd_crawl_all(argparse.Namespace(site=site_arg, type="years", pages=None, workers=8))
            elif sub_c == "3":
                cmd_crawl_all(argparse.Namespace(site=site_arg, type="atoz", pages=None, workers=8))
            else:
                cmd_crawl_all(argparse.Namespace(site=site_arg, type="all", pages=None, workers=8))
        elif choice == "4":
            print("\nSelect Site: [1] Moviesda.ski | [2] isaiDub.green")
            s_pick = input("Select (1-2, default 1): ").strip() or "1"
            site_arg = "isaidub" if s_pick == "2" else "moviesda"
            yr = input("Enter year (default 2026): ").strip() or "2026"
            pages = input("Enter number of pages (default 2): ").strip() or "2"
            cmd_sync(argparse.Namespace(site=site_arg, year=int(yr), pages=int(pages), with_details=False))
        elif choice == "5":
            u = input("Enter movie page URL: ").strip()
            if u:
                cmd_info(argparse.Namespace(url=u))
        elif choice == "6":
            u = input("Enter movie page URL: ").strip()
            if u:
                cmd_links(argparse.Namespace(url=u, resolve=True))
        elif choice == "7":
            cmd_stats(None)
        elif choice in ("0", "q", "exit"):
            print("Goodbye!\n")
            break
        else:
            print("Invalid choice, please select 0 to 7.")


def main():
    parser = argparse.ArgumentParser(
        description="Moviesda & isaiDub Web Scraper & Direct MP4 Downloader CLI"
    )
    subparsers = parser.add_subparsers(dest="command")

    # latest
    p_latest = subparsers.add_parser("latest", help="Show latest additions from the homepage")
    p_latest.add_argument("--site", choices=["moviesda", "isaidub"], default="moviesda")

    # search
    p_search = subparsers.add_parser("search", help="Search movies in SQLite cache (with live folder fallback)")
    p_search.add_argument("query", help="Movie title or keywords")
    p_search.add_argument("--limit", type=int, default=25, help="Max results")
    p_search.add_argument("--live", action="store_true", help="Force live search on site folders")
    p_search.add_argument("--full", action="store_true", help="Full exhaustive search across all 26 letters (A-Z) and years")
    p_search.add_argument("--site", choices=["moviesda", "isaidub", "all"], default=None, help="Specific site to live search")
    p_search.add_argument("--pages", type=int, default=None, help="Max pages per folder (default: all pages)")

    # sync-all (multi-folder)
    p_crawl = subparsers.add_parser("sync-all", help="Crawl movies across all website folders concurrently")
    p_crawl.add_argument("--site", choices=["moviesda", "isaidub", "all"], default="all", help="Website to crawl")
    p_crawl.add_argument("--type", choices=["years", "atoz", "series", "dubbed", "collections", "all"], default="all")
    p_crawl.add_argument("--pages", type=int, default=None, help="Max pages per folder")
    p_crawl.add_argument("--workers", type=int, default=6, help="Concurrent workers")

    # sync (single year)
    p_sync = subparsers.add_parser("sync", help="Sync movies for a given year")
    p_sync.add_argument("--site", choices=["moviesda", "isaidub"], default="moviesda", help="Target website")
    p_sync.add_argument("--year", type=int, default=2026, help="Release year (e.g. 2026, 2025)")
    p_sync.add_argument("--pages", type=int, default=1, help="Number of pages to scrape")
    p_sync.add_argument("--with-details", action="store_true", help="Also scrape full metadata for each movie")

    # info
    p_info = subparsers.add_parser("info", help="Get rich metadata for a movie URL")
    p_info.add_argument("url", help="Movie page URL")

    # links
    p_links = subparsers.add_parser("links", help="Extract and resolve download links for a movie")
    p_links.add_argument("url", help="Movie page URL")
    p_links.add_argument("--resolve", action="store_true", default=True, help="Resolve direct 302 MP4 links")

    # stats
    subparsers.add_parser("stats", help="Show database stats")

    args = parser.parse_args()

    if not args.command:
        interactive_menu()
    elif args.command == "latest":
        cmd_latest(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "sync-all":
        cmd_crawl_all(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "links":
        cmd_links(args)
    elif args.command == "stats":
        cmd_stats(args)


if __name__ == "__main__":
    main()
