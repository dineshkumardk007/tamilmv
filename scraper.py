"""
Scraper for 1tamilmv.capital listing/search pages.

Site structure (per the "RECENTLY ADDED" block on the homepage):

    <div class="banger-container">
      <div class="banger-header">...</div>
      <p>
        <strong>
          <a href="https://www.1tamilmv.<tld>/index.php?/forums/topic/<id>-<slug>/">
            Movie Title (Year) S01 EP(01-10) TRUE WEB-DL - [1080p - 720p - AVC - ... - 4.8GB + Rips]
          </a>
        </strong>
        ... more <a> entries, one per line (separated by <br>) ...
        <a href="https://<host>/e/<id>" rel="external nofollow">[W]</a>   <!-- optional "watch" mirror link -->
      </p>
    </div>

Each topic link's anchor text is "Title (Year) ... - [quality/size info]".
A trailing "- [W]" link (to a third-party host like luluvid.com, drakkar.st,
streamcash.to, etc.) is an alternate watch/stream mirror for the preceding entry.

The site's built-in search bar hits:
    https://www.1tamilmv.<tld>/search/?q=who+am+i

and renders results as:

    <div id="results">
      <a class="sRow prio" href="https://.../index.php?/topic/<id>-<slug>/" data-tid="<id>">
        <div class="sIcon">W</div>
        <div class="sMain">
          <span class="sTitle">
            Title (Year) ... - [quality/size info] - ESub
            <span class="sBadge prio">Official TMV Post</span>       <!-- optional -->
            <span class="sBadge direct">Direct Link</span>           <!-- optional -->
          </span>
          <div class="sMeta">By <span class="who">author</span><span class="sep">.</span>Started Feb 12, 2024</div>
        </div>
        <div class="sStats">
          <div class="s"><span class="n">5</span><span class="l">Replies</span></div>
          <div class="s"><span class="n">30,376</span><span class="l">Views</span></div>
        </div>
        <div class="sLast"><span class="who">last poster</span><span class="when">2y ago</span></div>
      </a>
      ...
    </div>

NOTE: search results link to "/index.php?/topic/<id>-..." while the homepage
links to "/index.php?/forums/topic/<id>-...". Both forms are accepted.

This module only extracts titles + links; it does not download or host any
copyrighted media itself.
"""
import re
from urllib.parse import quote_plus, unquote

import requests
from bs4 import BeautifulSoup

BASE_HOST = "www.1tamilmv.io"
try:
    import domain_manager
    _h = domain_manager.get_active_host("tamilmv")
    if _h:
        BASE_HOST = _h
except Exception:
    pass
BASE_URL = f"https://{BASE_HOST}"
SEARCH_URL = BASE_URL + "/search/?q={query}"
# The search page itself now renders results client-side: it fetches this
# JSON endpoint (see /search/assets/js/search.js) and injects HTML into
# <div id="results"> after load. Scraping the static page HTML finds nothing
# for any query not already cached, since that div is empty server-side.
SEARCH_API_URL = BASE_URL + "/search/api/search.php"


def set_base_host(host: str) -> str:
    """Dynamically update base domain for rotating TLDs."""
    global BASE_HOST, BASE_URL, SEARCH_URL, SEARCH_API_URL
    cleaned = host.strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if not cleaned:
        raise ValueError("Invalid host")
    BASE_HOST = cleaned
    BASE_URL = f"https://{BASE_HOST}"
    SEARCH_URL = BASE_URL + "/search/?q={query}"
    SEARCH_API_URL = BASE_URL + "/search/api/search.php"
    return BASE_HOST


def get_base_host() -> str:
    return BASE_HOST


def check_host_health(host: str | None = None) -> dict:
    target_host = host.strip().lower().replace("https://", "").replace("http://", "").strip("/") if host else BASE_HOST
    target_url = f"https://{target_host}"
    try:
        resp = requests.get(target_url, headers=HEADERS, timeout=8)
        return {
            "host": target_host,
            "status": "online" if resp.status_code == 200 else "degraded",
            "status_code": resp.status_code,
            "latency_ms": int(resp.elapsed.total_seconds() * 1000),
        }
    except Exception as exc:
        return {
            "host": target_host,
            "status": "offline",
            "error": str(exc),
        }


# Matches both "/topic/12345-slug/" and "/forums/topic/12345-slug/"
TOPIC_URL_RE = re.compile(r"/(?:forums/)?topic/\d+")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)

_orig_session_get = _SESSION.get


def _wrapped_session_get(url, *args, **kwargs):
    clean_url = _clean_topic_fetch_url(url) if ("/topic/" in str(url) or "1tamilmv" in str(url)) else str(url)
    try:
        r = _orig_session_get(clean_url, *args, **kwargs)
        if r and hasattr(r, "url"):
            from urllib.parse import urlparse
            final_host = urlparse(r.url).netloc.lower()
            if final_host and "1tamilmv" in final_host and final_host != BASE_HOST.lower():
                set_base_host(final_host)
                try:
                    import domain_manager
                    domain_manager.set_active_host("tamilmv", final_host)
                except Exception:
                    pass
        return r
    except (requests.ConnectionError, requests.Timeout) as exc:
        try:
            import domain_manager
            resolved = domain_manager.auto_resolve_source("tamilmv")
            if resolved.get("online") and resolved.get("active_host"):
                new_clean = _clean_topic_fetch_url(url)
                return _orig_session_get(new_clean, *args, **kwargs)
        except Exception:
            pass
        raise exc


_SESSION.get = _wrapped_session_get

YEAR_RE = re.compile(r"\((\d{4})\)")
QUALITY_RE = re.compile(r"\[(.*?)\]")
# A bracket only counts as the "quality" bracket if it mentions a resolution,
# codec or size — otherwise it's something else, e.g. the alternate-title
# marker in "Who Am I [Amnesia] (2015)".
QUALITY_HINT_RE = re.compile(
    r"\b(\d{3,4}p|4K|2160p|x264|x265|AVC|HEVC|WEB-?DL|BluRay|HDRip|PreDVD|"
    r"\d+(?:\.\d+)?\s?(?:GB|MB))\b",
    re.IGNORECASE,
)
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?\s?(?:GB|MB))", re.IGNORECASE)
LANG_RE = re.compile(
    r"\b(Tamil|Telugu|Hindi|Malayalam|Kannada|English)\b", re.IGNORECASE
)
SERIES_RE = re.compile(
    r"\b(?:S\d{1,2}|Season\s*\d+|EP\s*\(?\d+|Episodes?|E\d{1,3})\b",
    re.IGNORECASE,
)


def _clean_text(text: str) -> str:
    """Collapse whitespace, including &nbsp; (\\xa0), into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _bracket_groups(text: str):
    """
    Yield the contents of each top-level [...] group, handling nesting.

    Titles often nest language lists inside the quality bracket, e.g.
      "- [1080p - AVC - [Tamil + Hindi] - 4.8GB + Rips]"
    A non-greedy regex would stop at the inner "]", so scan with a depth
    counter instead and return the whole outer group.
    """
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:i]
                start = None


def _extract_meta(title_text: str, url: str) -> dict:
    """Pull title/year/language/quality/size out of a listing title string."""
    year_match = YEAR_RE.search(title_text)
    size_match = SIZE_RE.search(title_text)
    langs = sorted(
        set(m.group(0) for m in LANG_RE.finditer(title_text)), key=title_text.find
    )

    # Pick the first bracket that actually looks like a quality spec, so
    # markers like "[Amnesia]" in the title aren't mistaken for one.
    quality = None
    for group in _bracket_groups(title_text):
        if QUALITY_HINT_RE.search(group):
            quality = group.strip()
            break

    # Title is everything before the year, or before the first " - [" if no year
    if year_match:
        clean_title = title_text[: year_match.start()].strip(" -")
    else:
        clean_title = title_text.split(" - [")[0].strip(" -")

    year = int(year_match.group(1)) if year_match else None

    # Fallback: if clean_title starts with bracket (e.g. only quality was in anchor text)
    # or is empty, extract the clean movie title and year from the topic URL slug!
    if not clean_title or clean_title.startswith("[") or clean_title == title_text:
        slug_match = re.search(r"/topic/\d+-([a-z0-9-]+?)(?:-((?:19|20)\d{2}))?", url, re.I)
        if slug_match:
            slug_words = slug_match.group(1).replace("-", " ").title()
            clean_name = re.sub(
                r"\b(True|Web|Dl|Hq|Predvd|Rip|Rips|X264|Hevc|Avc|Tam|Tel|Hin|Eng|Tamil|Telugu|Hindi|Malayalam)\b.*$",
                "",
                slug_words,
                flags=re.I,
            ).strip()
            clean_title = clean_name or slug_words
            if slug_match.group(2) and not year:
                year = int(slug_match.group(2))

    # Detect language from URL if missing from title text
    if not langs:
        url_langs = sorted(
            set(m.group(0) for m in LANG_RE.finditer(url)), key=url.find
        )
        if url_langs:
            langs = url_langs

    topic_id = None
    tid_match = re.search(r"/topic/(\d+)", url)
    if tid_match:
        try:
            topic_id = int(tid_match.group(1))
        except ValueError:
            pass

    is_series = bool(
        SERIES_RE.search(title_text)
        or SERIES_RE.search(url)
        or re.search(r"/(?:series|web-series|shows)/", url, re.I)
        or re.search(r"-s\d{1,2}-", url, re.I)
    )
    content_type = "series" if is_series else "movie"

    return {
        "title": clean_title or title_text,
        "year": year,
        "language": ", ".join(langs) if langs else None,
        "quality": quality,
        "size": size_match.group(1) if size_match else None,
        "topic_url": url,
        "topic_id": topic_id,
        "raw_text": title_text,
        "content_type": content_type,
    }


def _parse_entry(anchor) -> dict | None:
    """Parse a single <a> topic-link tag into a movie dict."""
    title_text = _clean_text(anchor.get_text(strip=True))
    url = anchor.get("href", "")
    if not title_text or not TOPIC_URL_RE.search(url):
        return None
    return _extract_meta(title_text, url)


def parse_listing_html(html: str) -> list[dict]:
    """Parse a page (homepage banger-container OR search results) into movie dicts."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_urls = set()

    # Homepage "recently added" block
    container = soup.find("div", class_="banger-container")
    root = container if container else soup

    # Iterate over ALL anchors (not just topic links) so that "[W]" mirror
    # links, which point to a different host, are seen right after the
    # topic link they belong to and can be attached to it.
    anchors = root.find_all("a", href=True)

    for a in anchors:
        text = a.get_text(strip=True)
        href = a.get("href", "")

        if text == "[W]" or "external nofollow" in (a.get("rel") or []):
            # This is a watch-mirror link, attach to the previous entry
            if results:
                results[-1]["watch_url"] = href
            continue

        entry = _parse_entry(a)
        if entry and entry["topic_url"] not in seen_urls:
            seen_urls.add(entry["topic_url"])
            results.append(entry)

    return results


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = text.replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


def parse_search_results(html: str) -> list[dict]:
    """
    Parse the site's built-in search page (/search/?q=...) into movie dicts.

    Results live in <div id="results"> as sibling <a class="sRow"> blocks.
    The title <span class="sTitle"> contains the real title plus zero or more
    nested <span class="sBadge"> chips ("Official TMV Post", "Direct Link")
    which must be stripped out before reading the title text.
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.find("div", id="results")
    if container is None:
        return []

    results = []
    seen_urls = set()

    for row in container.find_all("a", class_="sRow", href=True):
        url = row["href"]
        if not TOPIC_URL_RE.search(url) or url in seen_urls:
            continue

        title_span = row.find("span", class_="sTitle")
        if title_span is None:
            continue

        # Badges are nested inside sTitle; note them, then remove so they
        # don't pollute the title text.
        is_official = title_span.find("span", class_="prio") is not None
        has_direct_link = title_span.find("span", class_="direct") is not None
        for badge in title_span.find_all("span", class_="sBadge"):
            badge.extract()

        title_text = _clean_text(title_span.get_text())
        if not title_text:
            continue

        entry = _extract_meta(title_text, url)
        entry["topic_id"] = _parse_int(row.get("data-tid"))
        entry["is_official"] = is_official
        entry["has_direct_link"] = has_direct_link

        meta = row.find("div", class_="sMeta")
        if meta:
            who = meta.find("span", class_="who")
            entry["author"] = _clean_text(who.get_text()) if who else None

        stats = row.find("div", class_="sStats")
        if stats:
            for stat in stats.find_all("div", class_="s"):
                n = stat.find("span", class_="n")
                label = stat.find("span", class_="l")
                if not (n and label):
                    continue
                key = label.get_text(strip=True).lower()
                if key.startswith("repl"):
                    entry["replies"] = _parse_int(n.get_text())
                elif key.startswith("view"):
                    entry["views"] = _parse_int(n.get_text())

        seen_urls.add(url)
        results.append(entry)

    return results


def _slugify(text: str) -> str:
    """Mirror the site's client-side slugify() in search.js exactly."""
    base = re.sub(r"[^a-z0-9\s-]+", " ", (text or "").lower())
    base = re.sub(r"\s+", "-", base.strip())[:80]
    return base or "t"


def _topic_url_from_tid(tid: int, title: str) -> str:
    return f"{BASE_URL}/index.php?/topic/{tid}-{_slugify(title)}/"


def _entry_from_api_result(r: dict) -> dict:
    title_text = _clean_text(r.get("title") or "")
    entry = _extract_meta(title_text, _topic_url_from_tid(r["tid"], title_text))
    entry["topic_id"] = r.get("tid")
    entry["is_official"] = bool(r.get("priority"))
    entry["has_direct_link"] = bool(r.get("is_direct"))
    entry["author"] = r.get("author")
    entry["replies"] = r.get("replies")
    entry["views"] = r.get("views")
    return entry


def _fetch_search_api(
    query: str, page: int = 1, per_page: int = 25, sort: str = "date_desc"
) -> list[dict]:
    params = {
        "q": query,
        "priority": "1",
        "sort": sort,
        "page": str(page),
        "per_page": str(per_page),
    }
    resp = requests.get(SEARCH_API_URL, params=params, headers=HEADERS, timeout=1.5)
    resp.raise_for_status()
    data = resp.json()
    return [_entry_from_api_result(r) for r in data.get("results", [])]


def fetch_homepage_html() -> str:
    """Fetch homepage HTML using persistent session or fallback to curl."""
    try:
        resp = _SESSION.get(BASE_URL, timeout=10)
        if resp.status_code == 200 and "banger-container" in resp.text:
            return resp.text
    except Exception:
        pass
    try:
        import shutil
        import subprocess
        curl_bin = "curl" if shutil.which("curl") else "curl.exe"
        res = subprocess.run(
            [curl_bin, "-s", "-L", "-A", HEADERS["User-Agent"], BASE_URL],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=12,
        )
        if res.stdout and "banger-container" in res.stdout:
            return res.stdout
    except Exception:
        pass
    return ""


def parse_top_releases(html: str) -> list[dict]:
    """Parse the 'TOP RELEASES THIS WEEK' banger container from the homepage."""
    soup = BeautifulSoup(html, "lxml")
    banger = soup.find("div", class_="banger-container")
    if not banger:
        return []

    row = banger.find("div", class_="banger-row")
    if not row:
        return []

    items = []
    seen_urls = set()

    for a in row.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"/topic/\d+", href) or href in seen_urls:
            continue

        parts = []
        cur = a
        while cur.previous_sibling:
            cur = cur.previous_sibling
            if getattr(cur, "name", None) == "br":
                break
            txt = cur.get_text(strip=True) if hasattr(cur, "get_text") else str(cur).strip()
            if txt:
                parts.insert(0, txt)

        if a.parent and a.parent.name == "strong":
            p = a.parent
            while p.previous_sibling:
                p = p.previous_sibling
                if getattr(p, "name", None) == "br":
                    break
                txt = p.get_text(strip=True) if hasattr(p, "get_text") else str(p).strip()
                if txt:
                    parts.insert(0, txt)

        full_line = " ".join(parts) + " " + a.get_text(strip=True)

        watch_url = None
        next_s = a.next_sibling
        while next_s:
            if getattr(next_s, "name", None) == "a" and "[W]" in next_s.get_text():
                watch_url = next_s.get("href")
                break
            if getattr(next_s, "name", None):
                w = next_s.find("a", href=True)
                if w and "[W]" in w.get_text():
                    watch_url = w["href"]
                    break
            next_s = next_s.next_sibling

        meta = _extract_meta(full_line, href)
        meta["is_top_week"] = 1
        if watch_url:
            meta["watch_url"] = watch_url

        seen_urls.add(href)
        items.append(meta)

    return items


def fetch_homepage() -> list[dict]:
    """Fetch top releases this week, newest search releases, and homepage listings."""
    results = []
    seen_urls = set()

    # 1. First fetch "TOP RELEASES THIS WEEK" from homepage banger block
    hp_html = fetch_homepage_html()
    if hp_html:
        top_items = parse_top_releases(hp_html)
        for item in top_items:
            if item["topic_url"] not in seen_urls:
                seen_urls.add(item["topic_url"])
                results.append(item)

    # 2. Fetch newest releases via API (2026, 2025) sorted by date_desc
    for query in ["2026", "2025"]:
        try:
            api_items = _fetch_search_api(query, per_page=35, sort="date_desc")
            for item in api_items:
                if item["topic_url"] not in seen_urls:
                    seen_urls.add(item["topic_url"])
                    results.append(item)
        except Exception:
            pass

    # 3. Also parse general listing items if available
    if hp_html:
        try:
            hp_items = parse_listing_html(hp_html)
            for item in hp_items:
                if item["topic_url"] not in seen_urls:
                    seen_urls.add(item["topic_url"])
                    results.append(item)
        except Exception:
            pass

    # Sort newest forum topics first
    results.sort(key=lambda x: (x.get("topic_id") or 0), reverse=True)

    # 4. If local ISP completely blocked direct homepage, fall back to cloud bridge
    if not results:
        try:
            for q in ["2026", "2025"]:
                cloud_url = f"https://tamilmv-4vej.onrender.com/api/search?q={q}"
                r = requests.get(cloud_url, headers=HEADERS, timeout=4)
                if r.status_code == 200:
                    for item in r.json():
                        if item.get("topic_url") and item["topic_url"] not in seen_urls:
                            seen_urls.add(item["topic_url"])
                            results.append(item)
        except Exception:
            pass

    return results


def fetch_search(query: str) -> list[dict]:
    """Query the site's search JSON API (what the search page's own JS calls)."""
    try:
        results = _fetch_search_api(query)
        if results:
            return results
    except Exception:
        pass

    # Fall back to scraping the static search page in case the API shape
    # changes again — unlikely to find anything since results are normally
    # injected client-side, but cheap to try.
    try:
        url = SEARCH_URL.format(query=quote_plus(query))
        resp = requests.get(url, headers=HEADERS, timeout=1.5)
        resp.raise_for_status()

        results = parse_search_results(resp.text)
        if not results:
            results = parse_listing_html(resp.text)
        if results:
            return results
    except Exception:
        pass

    # Fallback to cloud bridge if direct local search was blocked by ISP
    try:
        cloud_url = f"https://tamilmv-4vej.onrender.com/api/search?q={quote_plus(query)}"
        r = requests.get(cloud_url, headers=HEADERS, timeout=3.5)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return data
    except Exception:
        pass

    return []


# ---------------------------------------------------------------------------
# Topic (download) page parsing
# ---------------------------------------------------------------------------

# Resolution + size hints used to build a clean per-quality label.
RES_RE = re.compile(r"\b(4K|2160p|1080p|720p|480p|360p)\b", re.IGNORECASE)


def strip_site_domains(text: str) -> str:
    """Strip website domains, site branding, and URLs so only clean movie name and specs remain."""
    if not text:
        return ""
    t = re.sub(r'https?://\S+', '', text, flags=re.I)
    t = re.sub(r'(?:www\d*\.)?(?:1tamilmv|tamilmv|tamilblasters|isaidub|isaimini|moviesda|tamilplay|tamilyogi|kuttymovies|masstamilan|skymovies|bolly4u|cinemavilla)\.[a-z0-9\-]+(?:\.[a-z0-9\-]+)?', '', t, flags=re.I)
    t = re.sub(r'\b[a-zA-Z0-9_\-]+\.(?:cz|world|solar|capital|reisen|cards|pm|vip|win|net|org|com|co|green|in|to|ch|xyz|me|tv|cc|pw|info|biz|site|live|app|icu|ws|top|pro|club|click|link)\b', '', t, flags=re.I)
    t = re.sub(r'[\[\(\{]?(?:1tamilmv|tamilmv|tamilblasters|isaidub|isaimini|moviesda|tamilyogi|kuttymovies)[\]\)\}]?', '', t, flags=re.I)
    t = re.sub(r'\b(?:www|1tamilmv|tamilmv|tamilblasters|isaidub|isaimini|moviesda)\b', '', t, flags=re.I)
    t = re.sub(r'^[\s\-_:•|–—]+|[\s\-_:•|–—]+$', '', t)
    t = re.sub(r'\s{2,}', ' ', t).strip()
    return t


def _label_from_magnet_dn(magnet_href: str) -> str | None:
    """Extract the human-readable 'dn' (display name) from a magnet URI."""
    m = re.search(r"[?&]dn=([^&]+)", magnet_href)
    if not m:
        return None
    return unquote(m.group(1))


def parse_download_page(html: str) -> list[dict]:
    """
    Parse a topic page into a list of downloadable quality options, e.g.:

        [
          {
            "quality": "1080p",
            "label": "The Hawk (2026) ... 1080p ... 4.8GB - ESub",
            "size": "4.8GB",
            "torrent_url": "https://.../attachment.php?id=156238&key=...",
            "magnet": "magnet:?xt=urn:btih:...",
          },
          ...
        ]

    Strategy: walk the page in document order. A quality is anchored by its
    magnet link (always present, unambiguous). For each magnet we grab the
    nearest preceding .torrent attachment link and the descriptive text from
    the magnet's own dn= parameter, which contains resolution + size.
    """
    soup = BeautifulSoup(html, "lxml")
    anchors = soup.find_all("a", href=True)

    options = []
    pending_torrent = None

    for a in anchors:
        href = a.get("href", "")

        # A .torrent attachment link (either flagged via data-fileext or the
        # attachment.php path). Hold onto it for the next magnet we see.
        is_torrent = (
            a.get("data-fileext") == "torrent"
            or ("attachment.php" in href and "id=" in href)
        )
        if is_torrent:
            pending_torrent = href
            continue

        if href.startswith("magnet:"):
            raw_label = _label_from_magnet_dn(href) or a.get_text(strip=True)
            label = strip_site_domains(raw_label) or raw_label
            res_match = RES_RE.search(label)
            size_match = SIZE_RE.search(label)
            options.append(
                {
                    "quality": res_match.group(1) if res_match else None,
                    "label": label,
                    "size": size_match.group(1) if size_match else None,
                    "torrent_url": pending_torrent,
                    "magnet": href,
                }
            )
            pending_torrent = None

    return options


def extract_poster_url(soup: BeautifulSoup) -> str | None:
    """Extract movie poster image URL from topic page HTML."""
    if not soup:
        return None

    # 1. First check explicit OpenGraph and Twitter card meta tags (often highest quality)
    og_img = (
        soup.find("meta", property="og:image")
        or soup.find("meta", attrs={"name": "twitter:image"})
        or soup.find("meta", property="og:image:secure_url")
    )
    if og_img and og_img.get("content"):
        c = og_img["content"].strip()
        if c.startswith("//"):
            c = "https:" + c
        if c.startswith("http") and not any(bad in c.lower() for bad in ["logo", "badge", "favicon", "avatar", "reaction", "staff", "default"]):
            return c

    # 2. Find the post content area
    container = (
        soup.find("div", attrs={"data-role": "commentContent"})
        or soup.find("div", class_=re.compile(r"cPost_contentWrap|ipsType_richText", re.I))
        or soup
    )

    # 3. Walk through all <img> tags in document order within the post
    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or img.get("data-lazy-src")
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        if not src.startswith("http"):
            continue

        lower = src.lower()

        # Skip UI elements, badges, reaction icons, torrent logos, gifs, smileys
        if (
            lower.endswith(".gif")
            or any(bad in lower for bad in ["torrborder", "utorrent", "reaction", "badge", "emoticon", "staff", "logo", "smiley", "default_photo", "avatar", "icon"])
        ):
            continue

        # Accept any valid movie poster image or CDN URL (including Twitter, ibb, postimg, etc.)
        if (
            any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", "format=jpg", "format=png", "format=webp", "jpg", "jpeg", "png", "webp"])
            or any(host in lower for host in ["twimg", "pixelbb", "imageride", "cdn", "ibb", "postimg", "imagebam", "media-amazon", "tmdb", "imdb", "wp-content", "uploads"])
        ):
            return src

    # 4. Check <a> links in post content pointing directly to image hosting services
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        lower_href = href.lower()
        if any(h in lower_href for h in ["imageride.net/images/", "i.postimg.cc/", "i.ibb.co/", "imagebam.com/view/", "m.media-amazon.com/images/"]):
            if any(ext in lower_href for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                return href

    return None


def _clean_topic_fetch_url(topic_url: str) -> str:
    tid_match = re.search(r"/topic/(\d+)", topic_url)
    if tid_match:
        return f"{BASE_URL}/index.php?/topic/{tid_match.group(1)}-t/"
    return re.sub(r"^https?://[^/]+", BASE_URL, topic_url.strip())


def fetch_download_details(topic_url: str) -> dict:
    """Fetch download links and poster_url from a topic page."""
    clean_url = _clean_topic_fetch_url(topic_url)
    html = ""

    # 1. Direct fetch with 8s timeout (verify content contains real torrent attachment or magnet)
    for candidate in [clean_url, topic_url]:
        try:
            resp = _SESSION.get(candidate, timeout=8)
            if resp.status_code == 200 and ("attachment.php" in resp.text or "magnet:?" in resp.text):
                html = resp.text
                break
        except Exception:
            pass

    # 2. High-speed fallback to cloud proxy bridge (Render) if local machine is ISP-blocked
    # (Only runs when not already on Render to prevent self-deadlock)
    if not html:
        try:
            import os
            if not os.environ.get("RENDER"):
                cloud_proxy = f"https://tamilmv-4vej.onrender.com/api/downloads?url={quote_plus(clean_url)}"
                r = requests.get(cloud_proxy, headers=HEADERS, timeout=8)
                if r.status_code == 200:
                    data = r.json()
                    if data and (data.get("options") or data.get("poster_url")):
                        return {
                            "options": data.get("options", []),
                            "poster_url": data.get("poster_url"),
                        }
        except Exception:
            pass

    # 3. Fallback to alternate mirrors if primary host is blocked/offline
    if not html:
        try:
            import domain_manager
            cfg = domain_manager.load_config()
            mirrors = cfg.get("tamilmv", {}).get("mirrors", [])
            for m in mirrors:
                if m.lower() in clean_url.lower():
                    continue
                mirror_url = re.sub(r"^https?://[^/]+", f"https://{m}", clean_url)
                try:
                    resp = _SESSION.get(mirror_url, timeout=5)
                    if resp.status_code == 200 and ("attachment.php" in resp.text or "magnet:?" in resp.text):
                        html = resp.text
                        set_base_host(m)
                        domain_manager.set_active_host("tamilmv", m)
                        break
                except Exception:
                    continue
        except Exception:
            pass

    # 4. Fallback to system curl if requests connection failed
    if not html:
        try:
            import shutil
            import subprocess
            curl_bin = "curl" if shutil.which("curl") else "curl.exe"
            res = subprocess.run(
                [curl_bin, "-s", "-L", "-A", HEADERS["User-Agent"], clean_url],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
            )
            if res.stdout and ("attachment.php" in res.stdout or "magnet:?" in res.stdout):
                html = res.stdout
        except Exception:
            pass

    if not html:
        raise RuntimeError(
            f"Could not connect to 1TamilMV ({BASE_HOST}). "
            "Torrent server timed out or is blocked. Please check active domain settings."
        )

    soup = BeautifulSoup(html, "lxml")
    options = parse_download_page(html)
    poster_url = extract_poster_url(soup)
    return {
        "options": options,
        "poster_url": poster_url,
    }


def fetch_download_links(topic_url: str) -> list[dict]:
    details = fetch_download_details(topic_url)
    return details["options"]

