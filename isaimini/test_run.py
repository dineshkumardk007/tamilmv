"""
Test runner for isaimini scraper and database
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db
import scraper

print("1. Initializing SQLite Database...")
db.init_db()
print("   [OK] DB Schema initialized.")

print("\n2. Fetching Homepage Categories...")
cats = scraper.fetch_categories()
print(f"   [OK] Found {len(cats['years'])} Years, {len(cats['web_series'])} Series, {len(cats['atoz'])} A-Z letters.")

print("\n3. Fetching Latest Homepage Updates...")
latest = scraper.fetch_latest_updates()
print(f"   [OK] Found {len(latest)} updates. Example: {latest[0]['title']}")

print("\n4. Fetching Category Movies (Tamil 2026 Movies)...")
c26 = "https://moviesda.ski/tamil-2026-movies/"
page_data = scraper.fetch_category_movies(c26, page=1)
print(f"   [OK] Page {page_data['current_page']} of {page_data['total_pages']}. Found {len(page_data['movies'])} movies.")

first_movie = page_data["movies"][0]
print(f"\n5. Fetching Details for: {first_movie['title']}...")
details = scraper.fetch_movie_details(first_movie["url"])
print(f"   [OK] Title: {details['title']}")
print(f"   [OK] Poster: {details['poster']}")
print(f"   [OK] Director: {details['director']}")
print(f"   [OK] Cast: {details['starring']}")
print(f"   [OK] Subfolders: {len(details['folders'])}")

print("\n6. Saving Movie to Database...")
with db.get_conn() as conn:
    m_id = db.upsert_movie(conn, details)
print(f"   [OK] Saved movie with Database ID: {m_id}")

print(f"\n7. Discovering Downloadable File Options...")
file_options = scraper.fetch_movie_download_options(first_movie["url"])
print(f"   [OK] Discovered {len(file_options)} downloadable file variants.")

if file_options:
    test_file = file_options[0]
    print(f"\n8. Resolving Anti-Leech Download Gateway for: {test_file['file_name']}...")
    resolved = scraper.resolve_download_link(test_file["download_page_url"])
    print(f"   [OK] Stage 1 Gateway: {resolved['stage1_url']}")
    print(f"   [OK] Stage 2 Gateway: {resolved['stage2_url']}")
    print(f"   [OK] Stage 3 CDN URL: {resolved['cdn_url']}")
    print(f"   [OK] DIRECT MP4 URL:  {resolved['direct_url']}")
    print(f"   [OK] Watch Online:    {resolved['watch_online_url']}")

    print("\n9. Saving File to Database...")
    with db.get_conn() as conn:
        f_id = db.upsert_file(conn, m_id, {**test_file, **resolved})
    print(f"   [OK] Saved file to DB with ID: {f_id}")

print("\n10. Testing FTS5 Full-Text Search...")
matches = db.search_movies(details["clean_title"])
print(f"   [OK] Search query '{details['clean_title']}' matched {len(matches)} records in SQLite FTS.")

print("\n11. Database Stats:")
stats = db.get_stats()
for k, v in stats.items():
    print(f"   - {k}: {v}")

print("\n===> ALL TESTS COMPLETED SUCCESSFULLY! <===")
