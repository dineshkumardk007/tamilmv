"""
MCP server: search for a movie by name and get its listing page / mirror links.

Tools exposed:
  - search_movie(query, refresh=False): search cached DB, optionally refresh from live search first
  - refresh_cache(): re-scrape the homepage "recently added" listing into SQLite
  - cache_stats(): row count in the local cache

Run standalone for local testing:
    python server.py

Register with an MCP client (e.g. Claude Desktop / Claude Code) via stdio transport.
"""
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import db
import scraper

app = Server("tamilmv-search")


def _format_results(results: list[dict]) -> str:
    if not results:
        return "No matches found. Try refresh_cache first, or a different/shorter query."

    lines = []
    for r in results:
        bits = [r["title"]]
        if r.get("year"):
            bits.append(f"({r['year']})")
        if r.get("is_official"):
            bits.append("[Official]")
        if r.get("has_direct_link"):
            bits.append("[Direct Link]")
        header = " ".join(bits)
        meta = []
        if r.get("language"):
            meta.append(r["language"])
        if r.get("quality"):
            meta.append(r["quality"])
        if r.get("size"):
            meta.append(r["size"])
        if r.get("views"):
            meta.append(f"{r['views']:,} views")
        meta_line = " | ".join(meta)

        lines.append(f"- {header}")
        if meta_line:
            lines.append(f"  {meta_line}")
        lines.append(f"  Page: {r['topic_url']}")
        if r.get("watch_url"):
            lines.append(f"  Watch mirror: {r['watch_url']}")
    return "\n".join(lines)


def _format_download_links(topic_url: str, options: list[dict]) -> str:
    if not options:
        return (
            f"No download links found on that page.\n{topic_url}\n"
            "The page markup may have changed, or the URL might not be a movie topic page."
        )

    lines = [f"Download options ({len(options)} found):", ""]
    for o in options:
        header = o.get("quality") or "Unknown quality"
        if o.get("size"):
            header += f" - {o['size']}"
        lines.append(header)
        if o.get("magnet"):
            lines.append(f"  Magnet: {o['magnet']}")
        if o.get("torrent_url"):
            lines.append(f"  Torrent: {o['torrent_url']}")
        lines.append("")
    return "\n".join(lines).rstrip()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_movie",
            description=(
                "Search for a movie/show by name. Searches the local SQLite cache first; "
                "if nothing is found (or refresh=true), performs a live search against the "
                "1tamilmv site's forum search and caches the results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Movie or show name to search for"},
                    "refresh": {
                        "type": "boolean",
                        "description": "Force a live search even if cached results exist",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="refresh_cache",
            description="Re-scrape the site's homepage 'recently added' listing into the local SQLite cache.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_download_links",
            description=(
                "Given a movie's topic page URL (the 'Page:' link returned by search_movie), "
                "fetch that page and extract the actual download options — each quality "
                "(1080p/720p/480p...) with its size, magnet link, and .torrent file link. "
                "Use this after the user confirms which movie they want."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_url": {
                        "type": "string",
                        "description": "The forum topic/page URL for the movie (from search_movie output)",
                    },
                },
                "required": ["topic_url"],
            },
        ),
        Tool(
            name="cache_stats",
            description="Report how many movie entries are currently cached locally.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    db.init_db()

    if name == "cache_stats":
        n = db.count_movies()
        return [TextContent(type="text", text=f"{n} movies cached locally.")]

    if name == "get_download_links":
        topic_url = arguments["topic_url"]
        try:
            options = await asyncio.to_thread(scraper.fetch_download_links, topic_url)
        except Exception as exc:
            return [TextContent(type="text", text=f"Failed to fetch download page: {exc}")]
        return [TextContent(type="text", text=_format_download_links(topic_url, options))]

    if name == "refresh_cache":
        entries = await asyncio.to_thread(scraper.fetch_homepage)
        with db.get_conn() as conn:
            for e in entries:
                db.upsert_movie(conn, e)
        return [TextContent(type="text", text=f"Cached {len(entries)} entries from homepage.")]

    if name == "search_movie":
        query = arguments["query"]
        refresh = arguments.get("refresh", False)

        results = [] if refresh else db.search_movies(query)

        if not results:
            try:
                live_entries = await asyncio.to_thread(scraper.fetch_search, query)
            except Exception as exc:
                return [TextContent(type="text", text=f"Live search failed: {exc}")]

            with db.get_conn() as conn:
                for e in live_entries:
                    db.upsert_movie(conn, e)
            results = db.search_movies(query)

        return [TextContent(type="text", text=_format_results(results))]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    db.init_db()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
