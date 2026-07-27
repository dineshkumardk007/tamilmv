"""
Scraper for 1tamilmv.reisen listing/search pages.

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

BASE_HOST = "www.1tamilmv.reisen"
BASE_URL = f"https://{BASE_HOST}"
SEARCH_URL = BASE_URL + "/search/?q={query}"
# The search page itself now renders results client-side: it fetches this
# JSON endpoint (see /search/assets/js/search.js) and injects HTML into
# <div id="results"> after load. Scraping the static page HTML finds nothing
# for any query not already cached, since that div is empty server-side.
SEARCH_API_URL = BASE_URL + "/search/api/search.php"

# Matches both "/topic/12345-slug/" and "/forums/topic/12345-slug/"
TOPIC_URL_RE = re.compile(r"/(?:forums/)?topic/\d+")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

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

    return {
        "title": clean_title or title_text,
        "year": int(year_match.group(1)) if year_match else None,
        "language": ", ".join(langs) if langs else None,
        "quality": quality,
        "size": size_match.group(1) if size_match else None,
        "topic_url": url,
        "raw_text": title_text,
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


def fetch_homepage() -> list[dict]:
    resp = requests.get(BASE_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return parse_listing_html(resp.text)


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


def _fetch_search_api(query: str, page: int = 1, per_page: int = 25) -> list[dict]:
    params = {
        "q": query,
        "priority": "1",
        "sort": "title_asc",
        "page": str(page),
        "per_page": str(per_page),
    }
    resp = requests.get(SEARCH_API_URL, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return [_entry_from_api_result(r) for r in data.get("results", [])]


def fetch_search(query: str) -> list[dict]:
    """Query the site's search JSON API (what the search page's own JS calls)."""
    try:
        results = _fetch_search_api(query)
        if results:
            return results
    except (requests.RequestException, ValueError, KeyError):
        pass

    # Fall back to scraping the static search page in case the API shape
    # changes again — unlikely to find anything since results are normally
    # injected client-side, but cheap to try.
    url = SEARCH_URL.format(query=quote_plus(query))
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    results = parse_search_results(resp.text)
    if not results:
        results = parse_listing_html(resp.text)
    return results


# ---------------------------------------------------------------------------
# Topic (download) page parsing
# ---------------------------------------------------------------------------

# Resolution + size hints used to build a clean per-quality label.
RES_RE = re.compile(r"\b(4K|2160p|1080p|720p|480p|360p)\b", re.IGNORECASE)


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
            label = _label_from_magnet_dn(href) or a.get_text(strip=True)
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
    # Find the post content area
    container = (
        soup.find("div", attrs={"data-role": "commentContent"})
        or soup.find("div", class_=re.compile(r"cPost_contentWrap|ipsType_richText", re.I))
        or soup
    )

    # Walk through all <img> tags in document order within the post
    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        if not src.startswith("http"):
            continue

        lower = src.lower()

        # Skip UI elements, badges, reaction icons, torrent logos, gifs
        if (
            lower.endswith(".gif")
            or "torrborder" in lower
            or "utorrent" in lower
            or "reaction" in lower
            or "badge" in lower
            or "emoticon" in lower
            or "staff" in lower
            or "logo" in lower
        ):
            continue

        # Accept any valid movie poster image (.jpg, .jpeg, .png, .webp) or CDN URL
        if any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp"]) or "cdn" in lower:
            return src

    return None


def fetch_download_details(topic_url: str) -> dict:
    """Fetch download links and poster_url from a topic page."""
    resp = requests.get(topic_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    options = parse_download_page(resp.text)
    poster_url = extract_poster_url(soup)
    return {
        "options": options,
        "poster_url": poster_url,
    }


def fetch_download_links(topic_url: str) -> list[dict]:
    details = fetch_download_details(topic_url)
    return details["options"]

