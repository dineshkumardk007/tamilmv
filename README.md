# tamilmv-search MCP server

Local MCP server that searches 1tamilmv.pizza listings and caches results in SQLite.

## Setup

```
cd moviesearch
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run standalone (for testing)

```
python server.py
```

## Register with Claude Code / Claude Desktop

Add to your MCP config (e.g. `.claude.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tamilmv-search": {
      "command": "python",
      "args": ["C:\\Users\\dines\\ClaudeCode\\moviesearch\\server.py"]
    }
  }
}
```

## Tools

- `search_movie(query, refresh=false)` — search cache; falls back to a live site search and caches results. Returns each match's title, quality summary, and topic page URL.
- `get_download_links(topic_url)` — fetch a movie's topic page and extract every download option: each quality (1080p/720p/480p...) with its size, magnet link, and .torrent file link.
- `refresh_cache()` — re-scrape the homepage "recently added" block into SQLite
- `cache_stats()` — count of cached rows

## Typical flow

1. `search_movie("The Hawk")` → returns matches, each with a `Page:` URL.
2. User confirms which one they want.
3. `get_download_links(<that Page URL>)` → returns magnet + torrent links per quality.

## Files

- `scraper.py` — requests + BeautifulSoup parsing of the site's topic-link markup
- `db.py` — SQLite schema (with FTS5 full-text search) + helpers
- `server.py` — MCP server (stdio transport) wiring the tools above

## Notes

- The site is a torrent/streaming-link index; this tool only extracts titles and links
  you already have access to via your own browser — it doesn't download or host anything.
- Domain TLDs for this site rotate periodically (`.reisen`, `.cards`, etc. have all been seen).
  Update `BASE_HOST` in `scraper.py` if the search stops returning results.
- Search uses the site's built-in search bar endpoint: `/search/?q=<query>`, parsing the
  `<div id="results">` / `<a class="sRow">` markup (see `parse_search_results`).
- Two topic URL shapes exist and both are accepted: `/index.php?/topic/<id>-...` (search
  results) and `/index.php?/forums/topic/<id>-...` (homepage listing).
- If the site adds bot protection, `HEADERS` in `scraper.py` is where to add cookies/UA tweaks.
