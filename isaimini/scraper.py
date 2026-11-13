"""
isaimini/scraper.py — High-performance scraper & anti-leech resolver for Moviesda (Isaimini).
Extracts movie catalogs, metadata, posters, quality variants, and resolves direct MP4 links.
"""
import base64
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, parse_qs, urlparse

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("isaimini")

MOVIESDA_BASE_URL = "https://moviezda.me"
ISAIDUB_BASE_URL = "https://isaidub.green"
BASE_URL = MOVIESDA_BASE_URL

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 20.0


def is_gateway_or_cdn_host(host_or_url: str) -> bool:
    """Detect if a host/URL belongs to a CDN or intermediate download gateway rather than a browsing site."""
    if not host_or_url:
        return False
    h = str(host_or_url).strip().lower()
    if h.startswith("http://") or h.startswith("https://"):
        try:
            parsed = urlparse(h)
            if (parsed.path or "").lower().startswith("/download.php"):
                return True
            h = (parsed.netloc or "").lower()
        except Exception:
            pass
    return (
        h.startswith("download.")
        or h.startswith("cdn.")
        or h.startswith("stream.")
        or any(k in h for k in ("dubpage", "dubmv", "dubshare", "uptodub", "onestream", "uptomkv", "moviespage", "downloadpage", "r2.cloudflarestorage.com"))
    )


def rewrite_url(raw_url: str) -> str:
    """Rewrite obsolete or old domains to the active live domain."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    u = raw_url.strip()
    if is_gateway_or_cdn_host(u):
        return u
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(u)
        if not parsed.netloc:
            return u
        netloc_low = parsed.netloc.lower()
        if is_gateway_or_cdn_host(netloc_low):
            return u
        if "isaidub" in netloc_low:
            active = get_isaidub_host()
            if active and netloc_low != active.lower():
                return urlunparse(parsed._replace(netloc=active, scheme="https"))
        elif any(k in netloc_low for k in ("moviesda", "isaimini", "moviezda")):
            active = get_moviesda_host()
            if active and netloc_low != active.lower():
                return urlunparse(parsed._replace(netloc=active, scheme="https"))
    except Exception:
        pass
    return u


def check_response_redirect(resp: requests.Response):
    """Auto-detect if site redirected to a new domain and update live state."""
    if not resp or not hasattr(resp, "url"):
        return
    try:
        final_host = urlparse(resp.url).netloc.lower()
        if not final_host or is_gateway_or_cdn_host(final_host):
            return
        if "isaidub" in resp.url.lower():
            if final_host != get_isaidub_host().lower():
                set_isaidub_host(final_host)
                try:
                    import domain_manager
                    domain_manager.set_active_host("isaidub", final_host)
                except Exception:
                    pass
        elif any(k in resp.url.lower() for k in ("moviesda", "moviezda", "isaimini")):
            if final_host != get_moviesda_host().lower():
                set_moviesda_host(final_host)
                try:
                    import domain_manager
                    domain_manager.set_active_host("moviesda", final_host)
                except Exception:
                    pass
    except Exception:
        pass


def get_base_url() -> str:
    return MOVIESDA_BASE_URL


def set_base_url(new_url: str) -> str:
    return set_moviesda_host(new_url)


def get_moviesda_host() -> str:
    return MOVIESDA_BASE_URL.replace("https://", "").replace("http://", "").strip("/")


def get_isaidub_host() -> str:
    return ISAIDUB_BASE_URL.replace("https://", "").replace("http://", "").strip("/")


def set_moviesda_host(host: str) -> str:
    global MOVIESDA_BASE_URL, BASE_URL
    clean = host.strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if not clean:
        raise ValueError("Invalid Moviesda host")
    MOVIESDA_BASE_URL = f"https://{clean}"
    BASE_URL = MOVIESDA_BASE_URL
    return clean


def set_isaidub_host(host: str) -> str:
    global ISAIDUB_BASE_URL
    clean = host.strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if not clean:
        raise ValueError("Invalid isaiDub host")
    ISAIDUB_BASE_URL = f"https://{clean}"
    return clean


def check_site_health(host_or_url: str | None = None) -> dict:
    raw = (host_or_url or MOVIESDA_BASE_URL).strip().lower()
    host = raw.replace("https://", "").replace("http://", "").strip("/")
    target_url = f"https://{host}"
    try:
        s = create_session()
        resp = s.get(target_url, timeout=6.0)
        return {
            "host": host,
            "url": target_url,
            "status": "online" if resp.status_code == 200 else f"HTTP {resp.status_code}",
            "status_code": resp.status_code,
            "latency_ms": int(resp.elapsed.total_seconds() * 1000),
        }
    except Exception as exc:
        return {
            "host": host,
            "url": target_url,
            "status": "offline",
            "error": str(exc),
            "status_code": 0,
            "latency_ms": None,
        }


def create_session() -> requests.Session:
    """Create a configured requests session with desktop headers, robust retry adapter, and auto-domain resolver."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    retries = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=30, pool_maxsize=30)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Wrap session.get for dynamic URL rewriting and auto-domain resolution
    _orig_get = session.get

    def _wrapped_get(url, *args, **kwargs):
        clean_url = rewrite_url(url)
        try:
            r = _orig_get(clean_url, *args, **kwargs)
            check_response_redirect(r)
            return r
        except (requests.ConnectionError, requests.Timeout) as exc:
            try:
                import domain_manager
                src = "isaidub" if "isaidub" in clean_url else "moviesda"
                resolved = domain_manager.auto_resolve_source(src)
                if resolved.get("online") and resolved.get("active_host"):
                    new_url = rewrite_url(clean_url)
                    r = _orig_get(new_url, *args, **kwargs)
                    check_response_redirect(r)
                    return r
            except Exception:
                pass
            raise exc

    session.get = _wrapped_get
    return session


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    # Strip zero-width spaces, joiners, and BOM
    t = re.sub(r"[\u200b-\u200f\ufeff]", "", str(text))
    return re.sub(r"\s+", " ", t).strip()


def parse_clean_title(title: str) -> tuple[str, int | None]:
    """Extract clean title and 4-digit release year."""
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    year = int(year_match.group(1)) if year_match else None
    cleaned = re.sub(r"\(.*?\)|\[.*?\]", "", title)
    cleaned = re.sub(r"\b(720p|1080p|360p|480p|HD|HQ|Original|PreDVD|DVDRip|WEB-DL|Mp4|Movie)\b", "", cleaned, flags=re.I)
    cleaned = clean_text(cleaned)
    return cleaned, year


def is_series(text: str) -> bool:
    """Determine if a title or URL points to a web series / TV show."""
    return bool(re.search(r"\b(series|season|s\d+|ep\d+|episode|hotstar|zee5|aha|sonyliv)\b", text, re.I))


# ---------------------------------------------------------------------------
# 1. CATEGORIES & HOMEPAGE DISCOVERY
# ---------------------------------------------------------------------------

def fetch_categories(base_url: str | None = None, session: requests.Session | None = None) -> dict[str, list[dict]]:
    """
    Parse all available categories from the homepage:
    Years (2026, 2025...), Collections, Web Series, Dubbed, and A-Z index.
    Supports both Moviesda (https://moviesda.ski) and IsaiDub (https://isaidub.green).
    """
    target_base = (base_url or BASE_URL).rstrip("/")
    s = session or create_session()
    r = s.get(target_base, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    categories = {
        "years": [],
        "collections": [],
        "web_series": [],
        "dubbed": [],
        "atoz": [],
    }

    # Parse standard folder categories in <div class="f"> and <div class="folder">
    for f in soup.select("div.f, div.folder"):
        if f.select_one(".mv-content"):
            continue
        a = f.find("a")
        if not a or not a.get("href"):
            continue
        href = urljoin(target_base, a["href"])
        name = clean_text(a.text)

        if re.search(r"\b\d{4}\s+(?:Movies|Dubbed\s+Movies)\b", name, re.I):
            categories["years"].append({"name": name, "url": href})
        elif "series" in name.lower() or "series" in href.lower():
            categories["web_series"].append({"name": name, "url": href})
        elif "dubbed" in name.lower() and "atoz" not in href.lower() and not re.search(r"\b\d{4}\b", name):
            categories["dubbed"].append({"name": name, "url": href})
        elif "collection" in name.lower():
            categories["collections"].append({"name": name, "url": href})

    # For isaidub, fetch all 47 release years (1980-2026) from the dedicated yearly archive
    if "isaidub" in target_base:
        try:
            yr_res = s.get(f"{target_base}/tamil-yearly-dubbed-movies/", timeout=DEFAULT_TIMEOUT)
            if yr_res.status_code == 200:
                yr_soup = BeautifulSoup(yr_res.text, "html.parser")
                existing_urls = {y["url"] for y in categories["years"]}
                for f in yr_soup.select("div.f, div.folder"):
                    a = f.find("a")
                    if a and a.get("href"):
                        href = urljoin(target_base, a["href"])
                        name = clean_text(a.text)
                        if re.search(r"\b\d{4}\b", name) and href not in existing_urls:
                            existing_urls.add(href)
                            categories["years"].append({"name": name, "url": href})
        except Exception as yr_err:
            logger.debug("Could not fetch full yearly archive: %s", yr_err)

    # Parse A-Z categories from the inline-list-atoz or alpha-list block
    for a in soup.select(".inline-list-atoz a, .alpha-list a"):
        letter = clean_text(a.text)
        if letter and len(letter) == 1:
            categories["atoz"].append({"letter": letter.upper(), "url": urljoin(target_base, a["href"])})

    if not categories["atoz"]:
        if "isaidub" in target_base:
            categories["atoz"] = [
                {"letter": c.upper(), "url": f"{target_base}/tamil-atoz-dubbed-movies/{c.lower()}"}
                for c in "abcdefghijklmnopqrstuvwxyz"
            ]
        else:
            categories["atoz"] = [
                {"letter": c.upper(), "url": f"{target_base}/tamil-movies/{c.lower()}/"}
                for c in "abcdefghijklmnopqrstuvwxyz"
            ]

    return categories


def fetch_latest_updates(site_url: str = BASE_URL, session: requests.Session | None = None) -> list[dict]:
    """
    Parse homepage `<div class="latest">` news ticker / recent release additions.
    Supports Moviesda or isaiDub base URLs.
    """
    s = session or create_session()
    target_base = site_url or BASE_URL
    r = s.get(target_base, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    updates = []
    for item in soup.select("div.latest"):
        b_tag = item.find("b")
        raw_title = clean_text(b_tag.text if b_tag else item.text)
        a_tag = item.find("a")
        href = urljoin(target_base, a_tag["href"]) if a_tag and a_tag.get("href") else ""
        desc = clean_text(item.text)
        clean_name, year = parse_clean_title(raw_title)

        updates.append({
            "title": raw_title,
            "clean_title": clean_name,
            "year": year,
            "url": href,
            "description": desc,
            "content_type": "series" if is_series(raw_title) else "movie",
        })

    return updates


# ---------------------------------------------------------------------------
# 2. CATEGORY LISTING & PAGINATION
# ---------------------------------------------------------------------------

def fetch_category_movies(
    category_url: str,
    page: int = 1,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Extract movies listed on a category / year / alphabet page.
    Supports pagination: ?page=N (Moviesda) or ?get-page=N (IsaiDub).
    """
    s = session or create_session()
    page_url = category_url
    if page > 1:
        param = "get-page" if "isaidub" in category_url else "page"
        sep = "&" if "?" in category_url else "?"
        page_url = f"{category_url.rstrip('/')}/{sep}{param}={page}"

    r = s.get(page_url, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    movies = []
    for f in soup.select("div.f, div.folder"):
        a = f.find("a")
        if not a or not a.get("href"):
            continue
        href = urljoin(page_url, a["href"])
        raw_title = clean_text(a.text)

        # Ignore navigation loops (e.g. A-Z header or parent folders)
        if "(" in raw_title and "திரைப்படங்களுக்கு" in raw_title:
            continue

        clean_name, year = parse_clean_title(raw_title)
        movies.append({
            "title": raw_title,
            "clean_title": clean_name,
            "year": year,
            "url": href,
            "content_type": "series" if is_series(raw_title) or is_series(href) else "movie",
        })

    # Pagination parsing
    current_page = page
    total_pages = 1
    has_next = False

    pages_info = soup.select_one(".pages-info")
    if pages_info:
        cur_el = pages_info.find(id="currentPage")
        tot_el = pages_info.find(id="totalPages")
        if cur_el and cur_el.text.isdigit():
            current_page = int(cur_el.text)
        if tot_el and tot_el.text.isdigit():
            total_pages = int(tot_el.text)

    # Check for next page button
    next_btn = soup.select_one(".pagination a.next")
    if next_btn:
        has_next = True
    elif current_page < total_pages:
        has_next = True

    return {
        "movies": movies,
        "current_page": current_page,
        "total_pages": total_pages,
        "has_next": has_next,
        "page_url": page_url,
    }


# ---------------------------------------------------------------------------
# 3. MOVIE DETAILS & METADATA
# ---------------------------------------------------------------------------

def fetch_movie_details(
    movie_url: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Scrape detailed movie page: poster, director, cast, synopsis,
    screenshots, ratings, and subfolder version options.
    """
    s = session or create_session()
    r = s.get(movie_url, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Robust title extraction (prioritize og:title or main h1 over generic site headers)
    raw_title = ""
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        raw_title = clean_text(og_title["content"])
    elif soup.select_one("main h1"):
        raw_title = clean_text(soup.select_one("main h1").text)
    elif soup.title and soup.title.text:
        raw_title = clean_text(soup.title.text)

    # Strip SEO suffixes (e.g. "Tamil Movie Download Moviesda", "Tamil Dubbed Movie Download isaiDub", etc.)
    raw_title = re.sub(r"(?:Tamil\s+)?(?:Dubbed\s+)?(?:Movie|Web\s+Series|HD|Full\s+Movie)?\s*(?:Download)?\s*(?:Moviesda|isaiDub|isaimini).*$", "", raw_title, flags=re.I).strip()
    raw_title = re.sub(r"\s+Tamil\s+Movie.*$", "", raw_title, flags=re.I).strip()
    raw_title = re.sub(r"\s+Tamil\s+Web\s+Series.*$", "", raw_title, flags=re.I).strip()
    raw_title = re.sub(r"-\s*(?:isaimini|isaidub|moviesda).*$", "", raw_title, flags=re.I).strip()
    clean_name, year = parse_clean_title(raw_title)

    # Poster
    poster_url = None
    poster_img = (
        soup.select_one('meta[property="og:image"]')
        or soup.select_one('meta[name="twitter:image"]')
        or soup.select_one('img[src*="/uploads/posters/"]')
        or soup.select_one('picture img[src*="/uploads/posters/"]')
        or soup.select_one('picture source[srcset*="/uploads/posters/"]')
        or soup.select_one('img[alt*="Movie Poster"]')
        or soup.select_one('img[alt*="Poster"]')
        or soup.select_one(".movie-info-container picture img")
        or soup.select_one(".movie-info-container img")
        or soup.select_one("picture img")
    )
    if poster_img:
        src = poster_img.get("content") if poster_img.name == "meta" else (poster_img.get("src") or poster_img.get("data-src") or poster_img.get("data-original"))
        if not src and poster_img.get("srcset"):
            src = poster_img["srcset"].split()[0]
        if src and not src.endswith("folder.svg"):
            poster_url = urljoin(movie_url, src)

    # Fallback to standard /uploads/posters/<slug>.jpg
    if not poster_url and movie_url:
        parsed_p = urlparse(movie_url)
        seg = parsed_p.path.strip("/").split("/")[-1] if parsed_p.path else ""
        if seg and seg != "movie":
            slug_clean = re.sub(
                r"-(tamil-dubbed-movie-\d+|tamil-dubbed-movie|tamil-dubbed-web-series-\d+|tamil-dubbed-web-series|tamil-movie-\d+|tamil-movie|tamil-web-series-\d+|tamil-web-series|original-movie|movie-\d+|movie|web-series-\d+|web-series|hd-\d+|hd)$",
                "",
                seg,
                flags=re.I,
            )
            base_target = "https://isaidub.green" if "isaidub" in movie_url else (MOVIESDA_BASE_URL or "https://moviezda.me")
            poster_url = f"{base_target}/uploads/posters/{slug_clean}.jpg"

    # Metadata fields from <ul class="movie-info">
    info: dict[str, Any] = {}
    for li in soup.select("ul.movie-info li"):
        strong = li.find("strong")
        span = li.find("span")
        if strong and span:
            key = clean_text(strong.text).rstrip(":").lower()
            val = clean_text(span.text)
            info[key] = val

    # Rating
    rating_val = None
    if "movie rating" in info:
        m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", info["movie rating"])
        if m:
            rating_val = float(m.group(1))

    # Synopsis
    synopsis = None
    syn_el = soup.select_one(".movie-synopsis")
    if syn_el:
        synopsis = clean_text(syn_el.text)
        synopsis = re.sub(r"^Synopsis:\s*", "", synopsis, flags=re.I)

    # Screenshots
    screenshots = []
    for shot in soup.select(".screenshot-container img, .screenshot-img"):
        if shot.get("src"):
            screenshots.append(urljoin(movie_url, shot["src"]))

    # Subfolders (e.g. Original / PreDVD / Dubbed folders or Seasons)
    folders = []
    seen_folder_urls = set()

    def _collect_folders(s_el: BeautifulSoup, base_p_url: str):
        for f in s_el.select("div.f, div.folder"):
            if f.select_one(".mv-content"):
                continue
            a = f.find("a")
            if a and a.get("href"):
                h = urljoin(base_p_url, a["href"])
                if h.rstrip("/") == base_p_url.rstrip("/") or a.get("href") in ("/", "#"):
                    continue
                if h not in seen_folder_urls:
                    seen_folder_urls.add(h)
                    folders.append({
                        "title": clean_text(a.text),
                        "url": h,
                    })

    _collect_folders(soup, movie_url)

    # Check for pagination on the movie/series main page itself
    for p_url in _extract_pagination_urls(soup, movie_url):
        try:
            r_pag = s.get(p_url, timeout=DEFAULT_TIMEOUT)
            if r_pag.ok:
                s_pag = BeautifulSoup(r_pag.text, "html.parser")
                _collect_folders(s_pag, p_url)
        except Exception:
            pass

    return {
        "title": raw_title,
        "clean_title": clean_name,
        "year": year or (int(info.get("year")) if info.get("year", "").isdigit() else None),
        "url": movie_url,
        "poster": poster_url,
        "director": info.get("director"),
        "starring": info.get("starring"),
        "genres": info.get("genres"),
        "quality": info.get("quality"),
        "language": info.get("language", "Tamil"),
        "rating": rating_val,
        "last_updated": info.get("last updated"),
        "synopsis": synopsis,
        "screenshots": screenshots,
        "folders": folders,
        "content_type": "series" if is_series(raw_title) or is_series(movie_url) else "movie",
    }


# ---------------------------------------------------------------------------
# 4. FOLDER TRAVERSAL & DOWNLOADABLE FILE DISCOVERY WITH FULL PAGINATION
# ---------------------------------------------------------------------------

def _extract_pagination_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Discover all page URLs in a paginated folder or movie page (e.g. ?get-page=2, ?page=2)."""
    page_urls = []
    seen = {base_url}
    for a in soup.select(".pagination a, .pages a, [class*='pag'] a"):
        href = a.get("href")
        if not href or href in ("/", "#", "javascript:void(0)"):
            continue
        full_url = urljoin(base_url, href)
        if full_url not in seen and re.search(r"[?&](?:page|get-page)=\d+", full_url):
            seen.add(full_url)
            page_urls.append(full_url)

    pages_info = soup.select_one(".pages-info")
    if pages_info:
        tot_el = pages_info.find(id="totalPages")
        if tot_el and tot_el.text.strip().isdigit():
            total = int(tot_el.text.strip())
            param = "get-page" if "isaidub" in base_url else "page"
            clean_base = re.sub(rf"[?&]{param}=\d+", "", base_url)
            sep = "&" if "?" in clean_base else "?"
            for p in range(2, min(total + 1, 25)):  # Support up to 25 pages of episodes
                p_url = f"{clean_base}{sep}{param}={p}"
                if p_url not in seen:
                    seen.add(p_url)
                    page_urls.append(p_url)
    return page_urls


def parse_media_meta(file_name: str, parent_title: str = "", quality_label: str = "") -> dict[str, Any]:
    """
    Extract structured metadata (movie title, year, season, episode, sample status, clean display title)
    from movie collection items, series releases, or standalone filenames.
    """
    fn = clean_text(file_name or "")
    pt = clean_text(parent_title or "")
    combined = f"{pt} - {fn}".strip(" -")

    # 1. Sample clip detection
    is_sample = bool(re.search(r"\b(sample|preview|trailer)\b", fn, re.I))

    # 2. Season & Episode detection
    s_match = re.search(r"\b(?:Season|S)\s*(\d{1,2})\b", fn, re.I) or re.search(r"\b(?:Season|S)\s*(\d{1,2})\b", pt, re.I)
    e_match = re.search(r"\b(?:Episode|Epi|EP|E)\s*[\(\[]?\s*(\d{1,3})(?:\s*[-–to]+\s*(?:Episode|Epi|EP|E)?\s*(\d{1,3}))?[\)\]]?", fn, re.I)
    is_series = bool(s_match and e_match) or bool(e_match) or ("season" in combined.lower() and bool(re.search(r"\b\d{1,2}\b", combined)))
    season = int(s_match.group(1)) if s_match else None
    episode = int(e_match.group(1)) if e_match else None
    episode_end = int(e_match.group(2)) if (e_match and e_match.group(2)) else None

    # 3. Year detection
    y_match = re.search(r"\(?(19\d\d|20\d\d)\)?", fn) or re.search(r"\(?(19\d\d|20\d\d)\)?", pt)
    year = int(y_match.group(1)) if y_match else None

    # 4. Movie name in collection detection (e.g. "Die Hard 4 (2007)", "Species 2 (1998)")
    movie_name = None
    m_name_match = re.search(r"^([^\(]+?)\s*\(?(19\d\d|20\d\d)\)?", fn)
    if m_name_match:
        cand = m_name_match.group(1).strip(" -_")
        if cand.lower() not in ("mp4", "sample", "hd", "rip", "single part", "www", "isaidub", "isaimini", "tamilmv", "moviesda"):
            movie_name = cand
    if not movie_name and pt:
        pt_match = re.search(r"^([^\(]+?)\s*\(?(19\d\d|20\d\d)\)?", pt)
        if pt_match:
            cand = pt_match.group(1).strip(" -_")
            if not re.search(r"\b(collection|trilogy|quadrilogy|pentalogy|duology|movies|all)\b", cand, re.I):
                movie_name = cand

    # Clean quality label
    q = quality_label or "HD"
    for cand_q in ("2160p", "4K", "1080p", "720p", "640x360", "480p", "480x320", "360p"):
        if cand_q.lower() in combined.lower():
            q = cand_q
            break

    # Construct clean display title, badge, and friendly download filename
    if is_series and (season or episode):
        ep_str = f"Episodes {episode}–{episode_end}" if episode_end else (f"Episode {episode}" if episode else "")
        s_str = f"Season {season}" if season else ""
        display_badge = f"📺 {s_str} {ep_str}".strip()
        display_title = f"{s_str} • {ep_str}".strip(" •")
        clean_dl = f"{(movie_name or pt or 'Series')} {('S' + str(season).zfill(2)) if season else ''}{('E' + str(episode).zfill(2)) if episode else ''} [{q}].mp4"
    elif movie_name and year:
        display_badge = f"🎬 {movie_name} ({year})"
        display_title = f"{movie_name} ({year})"
        clean_dl = f"{movie_name} ({year}) [{q}]" + (".sample" if is_sample else "") + ".mp4"
    elif movie_name:
        display_badge = f"🎬 {movie_name}"
        display_title = movie_name
        clean_dl = f"{movie_name} [{q}]" + (".sample" if is_sample else "") + ".mp4"
    else:
        display_badge = f"🎬 {q}"
        display_title = fn or pt or "Movie Stream"
        clean_dl = f"{(pt or 'Movie')} [{q}]" + (".sample" if is_sample else "") + ".mp4"

    return {
        "movie_name": movie_name,
        "year": year,
        "is_series": is_series,
        "season": season,
        "episode": episode,
        "episode_end": episode_end,
        "is_sample": is_sample,
        "quality": q,
        "display_badge": display_badge,
        "display_title": display_title,
        "clean_download_name": clean_dl
    }


def _parse_folder_page_items(
    soup: BeautifulSoup,
    folder_url: str,
    parent_folder_title: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse direct files and subfolders from a single folder page."""
    files = []
    child_folders = []

    # 1. File items (<div class="folder"> or <div class="f"> with .mv-content)
    for item in soup.select("div.folder, div.f"):
        mv_content = item.select_one(".mv-content")
        if not mv_content:
            continue

        a = mv_content.select_one("a.coral, ul li a")
        if not a or not a.get("href"):
            continue

        file_url = urljoin(folder_url, a["href"])
        file_name = clean_text(a.text)

        # Prepend Season or Movie Title from parent folder if file_name doesn't mention it
        if parent_folder_title:
            if re.search(r"\b(?:Season\s*\d+|S\d{1,2})\b", parent_folder_title, re.I):
                s_match = re.search(r"\b(?:Season\s*\d+|S\d{1,2})\b", parent_folder_title, re.I)
                if s_match and not re.search(r"\b(?:Season\s*\d+|S\d{1,2})\b", file_name, re.I):
                    file_name = f"{s_match.group(0)} - {file_name}"
            elif re.search(r"\b(19\d\d|20\d\d)\b", parent_folder_title) and not re.search(r"\b(19\d\d|20\d\d)\b", file_name):
                file_name = f"{parent_folder_title} - {file_name}"

        # Extract size and format from <li> items
        file_size = None
        file_format = "Mp4"
        for li in mv_content.select("ul li"):
            txt = clean_text(li.text)
            if "file size:" in txt.lower():
                file_size = clean_text(re.sub(r"^file size:\s*", "", txt, flags=re.I))
            elif "download format:" in txt.lower():
                file_format = clean_text(re.sub(r"^download format:\s*", "", txt, flags=re.I))

        # Thumbnail
        thumb_img = mv_content.select_one(".tblimg img")
        thumb_url = urljoin(folder_url, thumb_img["src"]) if thumb_img and thumb_img.get("src") else None

        # Determine quality tag from folder, parent, or filename
        quality_label = "HD"
        combined_text = f"{folder_url} {parent_folder_title or ''} {file_name}".lower()
        for q in ("1080p", "720p", "360p", "480p", "4K"):
            if q.lower() in combined_text:
                quality_label = q
                break

        meta = parse_media_meta(file_name, parent_folder_title or "", quality_label)
        files.append({
            "quality_label": quality_label,
            "file_name": file_name,
            "file_size": file_size,
            "format": file_format,
            "download_page_url": file_url,
            "thumbnail": thumb_url,
            "movie_name": meta.get("movie_name"),
            "movie_year": meta.get("year"),
            "is_sample": meta.get("is_sample", False),
            "is_series": meta.get("is_series", False),
            "season": meta.get("season"),
            "episode": meta.get("episode"),
            "clean_title": meta.get("display_title"),
            "display_badge": meta.get("display_badge"),
            "download_filename": meta.get("clean_download_name")
        })

    # 2. Child folders (<div class="f"> or <div class="folder"> without .mv-content)
    for f in soup.select("div.f, div.folder"):
        if f.select_one(".mv-content"):
            continue
        a = f.find("a")
        if not a or not a.get("href"):
            continue
        child_url = urljoin(folder_url, a["href"])
        if child_url.rstrip("/") == folder_url.rstrip("/") or a.get("href") in ("/", "#"):
            continue
        child_folders.append({
            "title": clean_text(a.text),
            "url": child_url,
        })

    return files, child_folders


def fetch_folder_files(
    folder_url: str,
    session: requests.Session | None = None,
    depth: int = 0,
    max_depth: int = 6,
    parent_folder_title: str | None = None,
) -> list[dict[str, Any]]:
    """
    Recursively traverse movie version folders, season folders, and episode subfolders,
    fully supporting multi-page pagination (page 1, 2, 3...) to collect all downloadable file records.
    """
    if depth > max_depth:
        return []

    s = session or create_session()
    try:
        r = s.get(folder_url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        logger.debug("Failed to fetch folder %s: %s", folder_url, exc)
        return []

    all_files = []
    all_children = []

    # 1. Parse initial folder page
    f0, c0 = _parse_folder_page_items(soup, folder_url, parent_folder_title)
    all_files.extend(f0)
    all_children.extend(c0)

    # 2. Check for pagination pages (e.g. ?get-page=2, ?page=2)
    pag_urls = _extract_pagination_urls(soup, folder_url)
    for p_url in pag_urls:
        try:
            r_p = s.get(p_url, timeout=DEFAULT_TIMEOUT)
            if r_p.ok:
                soup_p = BeautifulSoup(r_p.text, "html.parser")
                f_p, c_p = _parse_folder_page_items(soup_p, p_url, parent_folder_title)
                all_files.extend(f_p)
                all_children.extend(c_p)
        except Exception as pag_err:
            logger.debug("Failed pagination page %s: %s", p_url, pag_err)

    # 3. Recurse into child folders
    if all_children and depth < max_depth:
        def _get_ctx_title(child_title: str) -> str | None:
            # 1. Season folder (e.g. "Season 1", "S02")
            if re.search(r"\b(?:Season\s*\d+|S\d{1,2})\b", child_title, re.I):
                return child_title
            # 2. Movie folder inside a collection (e.g. "Die Hard 2 (1990)", "Species 2 (1998)")
            if re.search(r"\b(19\d\d|20\d\d)\b", child_title):
                return child_title
            # 3. Movie title folder (not a technical quality tag like "Mp4 HD (640x360)", "720p HD", "Single Part")
            if not re.search(r"^(?:mp4|mkv|avi|hd|sample|480p|720p|1080p|360p|single\s*part|\d+x\d+)", child_title.strip(), re.I):
                return child_title
            return parent_folder_title

        if len(all_children) > 1:
            with ThreadPoolExecutor(max_workers=min(len(all_children), 6)) as executor:
                future_to_child = {
                    executor.submit(
                        fetch_folder_files,
                        child["url"],
                        session=s,
                        depth=depth + 1,
                        max_depth=max_depth,
                        parent_folder_title=_get_ctx_title(child["title"]),
                    ): child
                    for child in all_children
                }
                for fut in as_completed(future_to_child):
                    try:
                        all_files.extend(fut.result())
                    except Exception:
                        pass
        else:
            child = all_children[0]
            sub = fetch_folder_files(
                child["url"],
                session=s,
                depth=depth + 1,
                max_depth=max_depth,
                parent_folder_title=_get_ctx_title(child["title"]),
            )
            all_files.extend(sub)

    return all_files


def fetch_movie_download_options(
    movie_url: str,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """
    Convenience method: Given a movie page URL, inspects its subfolders
    and discovers all downloadable MP4 file variants across all seasons and paginated pages.
    """
    s = session or create_session()
    details = fetch_movie_details(movie_url, session=s)
    all_files = []

    if details.get("folders"):
        with ThreadPoolExecutor(max_workers=min(len(details["folders"]), 6)) as executor:
            future_to_folder = {
                executor.submit(
                    fetch_folder_files,
                    folder["url"],
                    session=s,
                    depth=1,
                    max_depth=6,
                    parent_folder_title=folder["title"],
                ): folder
                for folder in details["folders"]
            }
            for fut in as_completed(future_to_folder):
                try:
                    files = fut.result()
                    all_files.extend(files)
                except Exception:
                    pass
    else:
        # Some movies might have direct files on the movie page
        all_files = fetch_folder_files(movie_url, session=s, depth=1, max_depth=6)

    # Deduplicate by download_page_url
    seen = set()
    unique_files = []
    for f in all_files:
        if f["download_page_url"] not in seen:
            seen.add(f["download_page_url"])
            unique_files.append(f)

    return unique_files


# ---------------------------------------------------------------------------
# 5. ANTI-LEECH 3-STAGE DOWNLOAD GATEWAY RESOLVER
# ---------------------------------------------------------------------------

def resolve_download_link(
    download_page_url: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Traverses the full 3-stage anti-leech gateway to obtain the direct high-speed
    download / streaming MP4 URL:
      Stage 0: moviesda.ski/download/... (Extracts file metadata & Server 1 link)
      Stage 1: download.moviespage.xyz/download/file/<id>
      Stage 2: movies.downloadpage.xyz/download/page/<id> (CDN dl=base64 + Onestream player)
      Stage 3: Fastly / UptoMKV CDN download.php?dl=... (302 Redirect to direct MP4)
    """
    s = session or create_session()

    # --- STAGE 0: Initial Download Info Page ---
    r0 = s.get(download_page_url, timeout=DEFAULT_TIMEOUT)
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, "html.parser")

    # Extract metadata details
    meta_info: dict[str, str] = {}
    for d in soup0.select(".songinfo .details, .details"):
        strong = d.find("strong")
        if strong:
            k = clean_text(strong.text).rstrip(":").lower()
            v = clean_text(d.text)
            v = re.sub(rf"^{re.escape(strong.text)}\s*", "", v).strip()
            meta_info[k] = v

    stage1_tag = soup0.select_one(".download .dlink a")
    if not stage1_tag or not stage1_tag.get("href"):
        raise ValueError(f"Could not find Stage 1 download link on {download_page_url}")
    stage1_url = stage1_tag["href"]

    # --- STAGE 1: moviespage.xyz Gateway ---
    r1 = s.get(stage1_url, headers={"Referer": download_page_url}, timeout=DEFAULT_TIMEOUT)
    r1.raise_for_status()
    soup1 = BeautifulSoup(r1.text, "html.parser")

    stage2_tag = soup1.select_one(".download .dlink a")
    if not stage2_tag or not stage2_tag.get("href"):
        raise ValueError(f"Could not find Stage 2 link on {stage1_url}")
    stage2_url = stage2_tag["href"]

    # --- STAGE 2: downloadpage.xyz Gateway ---
    r2 = s.get(stage2_url, headers={"Referer": stage1_url}, timeout=DEFAULT_TIMEOUT)
    r2.raise_for_status()
    soup2 = BeautifulSoup(r2.text, "html.parser")

    # CDN Download Link (with dl=base64)
    cdn_tag = soup2.select_one(".download .dlink a")
    cdn_url = cdn_tag["href"] if cdn_tag and cdn_tag.get("href") else None

    # Watch Online Link
    watch_online_url = None
    for a in soup2.select("a"):
        href = a.get("href", "")
        if "onestream" in href or "stream/page" in href:
            watch_online_url = href
            break

    # --- STAGE 3: Resolve Direct MP4 URL from CDN 302 Header ---
    direct_mp4_url = None
    raw_cdn_params = {}

    if cdn_url:
        parsed_cdn = urlparse(cdn_url)
        query_dict = parse_qs(parsed_cdn.query)
        if "dl" in query_dict:
            b64_val = query_dict["dl"][0]
            try:
                # Add padding if needed
                padded = b64_val + "=" * (-len(b64_val) % 4)
                decoded_str = base64.b64decode(padded).decode("utf-8", errors="ignore")
                raw_cdn_params = parse_qs(decoded_str)
            except Exception as b64_err:
                logger.debug("Base64 decode error: %s", b64_err)

        # Send HEAD or GET with allow_redirects=False to read the 302 Location header
        try:
            r3 = s.get(
                cdn_url,
                headers={"Referer": stage2_url},
                allow_redirects=False,
                timeout=DEFAULT_TIMEOUT,
            )
            if r3.status_code in (301, 302, 303, 307, 308) and "Location" in r3.headers:
                direct_mp4_url = r3.headers["Location"]
            elif r3.status_code == 200:
                # Direct download stream returned immediately
                direct_mp4_url = cdn_url
            elif (r3.status_code == 404 or not direct_mp4_url) and "stream=1" not in cdn_url:
                # Some gateways (like download.isaidub.dad) require stream=1
                sep = "&" if "?" in cdn_url else "?"
                alt_url = f"{cdn_url}{sep}stream=1"
                stage2_parsed = urlparse(stage2_url)
                r3_alt = s.get(
                    alt_url,
                    headers={"Referer": f"{stage2_parsed.scheme}://{stage2_parsed.netloc}/"},
                    allow_redirects=False,
                    timeout=DEFAULT_TIMEOUT,
                )
                if r3_alt.status_code in (301, 302, 303, 307, 308) and "Location" in r3_alt.headers:
                    direct_mp4_url = r3_alt.headers["Location"]
                elif r3_alt.status_code == 200:
                    direct_mp4_url = alt_url
        except Exception as cdn_req_err:
            logger.warning("Could not resolve final 302 location: %s", cdn_req_err)

    # Fallback: if direct_mp4_url is still None, scrape watch_online_url player
    if not direct_mp4_url and watch_online_url:
        try:
            r_watch = s.get(
                watch_online_url,
                headers={"Referer": stage2_url},
                timeout=DEFAULT_TIMEOUT,
            )
            if r_watch.ok:
                soup_watch = BeautifulSoup(r_watch.text, "html.parser")
                for src_tag in soup_watch.select("video source, video, source"):
                    candidate_src = src_tag.get("src")
                    if candidate_src:
                        r_stream = s.get(
                            candidate_src,
                            headers={"Referer": watch_online_url},
                            allow_redirects=False,
                            timeout=DEFAULT_TIMEOUT,
                        )
                        if r_stream.status_code in (301, 302, 303, 307, 308) and "Location" in r_stream.headers:
                            direct_mp4_url = r_stream.headers["Location"]
                            break
                        elif r_stream.status_code == 200:
                            direct_mp4_url = candidate_src
                            break
        except Exception as watch_err:
            logger.debug("Could not resolve from watch_online_url: %s", watch_err)

    return {
        "file_name": meta_info.get("file name") or (raw_cdn_params.get("path", [None])[0] if raw_cdn_params else None),
        "file_size": meta_info.get("file size"),
        "duration": meta_info.get("duration"),
        "resolution": meta_info.get("video resolution") or meta_info.get("video size"),
        "format": meta_info.get("download format") or meta_info.get("format") or "Mp4",
        "added_on": meta_info.get("added on"),
        "download_page_url": download_page_url,
        "stage1_url": stage1_url,
        "stage2_url": stage2_url,
        "cdn_url": cdn_url,
        "watch_online_url": watch_online_url,
        "direct_url": direct_mp4_url,
        "resolved_at": time.time(),
    }


# ---------------------------------------------------------------------------
# 6. MULTI-FOLDER CONCURRENT CRAWLER & LIVE SITE SEARCH
# ---------------------------------------------------------------------------

def crawl_category(
    category_url: str,
    max_pages: int | None = None,
    max_workers: int = 6,
    progress_cb: Callable[[int, int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Crawls a category across all its pages concurrently using a ThreadPool.
    Returns the list of all movies found in that category/folder.
    """
    session = create_session()
    first_res = fetch_category_movies(category_url, page=1, session=session)
    total_pages = first_res.get("total_pages", 1)
    if max_pages and max_pages < total_pages:
        total_pages = max_pages

    all_movies = list(first_res.get("movies", []))
    if total_pages <= 1:
        return all_movies

    def _fetch_p(p):
        try:
            res = fetch_category_movies(category_url, page=p, session=session)
            return res.get("movies", [])
        except Exception:
            return []

    pages_to_fetch = list(range(2, total_pages + 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_page = {executor.submit(_fetch_p, p): p for p in pages_to_fetch}
        for future in as_completed(future_to_page):
            movies = future.result()
            all_movies.extend(movies)
            if progress_cb:
                progress_cb(len(all_movies), len(movies), total_pages)

    # Deduplicate by URL
    seen = set()
    unique = []
    for m in all_movies:
        if m["url"] not in seen:
            seen.add(m["url"])
            unique.append(m)
    return unique


def get_all_folder_targets(site_url: str | None = None) -> dict[str, list[dict[str, str]]]:
    """
    Returns a comprehensive catalog of all known folders on moviesda.ski or isaidub.green:
    - Years: 2026, 2025, 2024, 2023...
    - A-Z: A through Z (all movies categorized alphabetically)
    - Web Series
    - Dubbed
    - Collections
    """
    cats = fetch_categories(base_url=site_url)
    return {
        "years": cats.get("years", []),
        "web_series": cats.get("web_series", []),
        "dubbed": cats.get("dubbed", []),
        "collections": cats.get("collections", []),
        "atoz": cats.get("atoz", []),
    }


def crawl_all_folders(
    folder_type: str = "all",
    site_url: str | None = None,
    max_pages_per_folder: int | None = None,
    max_workers: int = 6,
    on_movie_batch: Callable[[list[dict]], None] | None = None,
    on_folder_status: Callable[[str, int], None] | None = None,
) -> int:
    """
    Crawl movies from every folder on the site and pass them to on_movie_batch.
    folder_type: 'years', 'atoz', 'series', 'dubbed', or 'all'
    """
    all_targets = get_all_folder_targets(site_url=site_url)
    folders_to_crawl: list[dict[str, str]] = []

    if folder_type in ("years", "all"):
        folders_to_crawl.extend(all_targets["years"])
    if folder_type in ("series", "all"):
        folders_to_crawl.extend(all_targets["web_series"])
    if folder_type in ("dubbed", "all"):
        folders_to_crawl.extend(all_targets["dubbed"])
    if folder_type in ("collections", "all"):
        folders_to_crawl.extend(all_targets["collections"])
    if folder_type in ("atoz", "all"):
        folders_to_crawl.extend(all_targets["atoz"])

    total_discovered = 0
    seen_urls = set()

    for idx, folder in enumerate(folders_to_crawl, start=1):
        name = folder.get("name") or folder.get("letter") or f"Folder {idx}"
        url = folder["url"]
        if on_folder_status:
            on_folder_status(f"Crawling '{name}' ({idx}/{len(folders_to_crawl)})...", total_discovered)

        try:
            movies = crawl_category(url, max_pages=max_pages_per_folder, max_workers=max_workers)
            new_movies = [m for m in movies if m["url"] not in seen_urls]
            for m in new_movies:
                seen_urls.add(m["url"])

            if new_movies and on_movie_batch:
                on_movie_batch(new_movies)

            total_discovered += len(new_movies)
        except Exception as folder_err:
            logger.warning("Error crawling folder %s: %s", url, folder_err)

    return total_discovered


def search_site(
    query: str,
    site_url: str | None = None,
    max_pages: int | None = None,
    full_crawl: bool = False,
    on_movie_found: Callable[[dict], None] | None = None,
    on_folder_crawled: Callable[[list[dict]], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Powerful Live Search across folders on moviesda.ski and/or isaidub.green:
    1. Extracts all potential first letters from query words (e.g. 'Bird Box' -> 'b', 'The Dark Knight' -> 'd', 'k').
    2. Identifies explicit release years in the query (e.g. '2018', '2024') to scan specific year folders.
    3. Crawls all pages of each candidate folder with max_workers=10 without restrictive page limits.
    4. If full_crawl=True, crawls ALL letters A-Z and ALL year archives.
    5. Discovered movies are yielded or passed to on_folder_crawled for auto-indexing into SQLite.
    """
    q = query.strip()
    if not q:
        return []

    words = [w.lower() for w in re.sub(r"[^\w\s]", "", q).split() if w]
    sites_to_search = [site_url.rstrip("/")] if site_url else ["https://moviesda.ski", "https://isaidub.green"]

    # Extract all starting letters from keywords (skipping small articles)
    stopwords = {"the", "a", "an", "in", "of", "on", "at", "to", "for", "and"}
    candidate_letters = set()
    for w in words:
        if w not in stopwords and w[0].isalpha():
            candidate_letters.add(w[0])
    if not candidate_letters and words:
        candidate_letters.add(words[0][0])

    # Check for release year in query
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", q)
    detected_year = int(year_match.group(1)) if year_match else None

    folders_to_search = []

    for target_base in sites_to_search:
        is_isaidub = "isaidub" in target_base
        if full_crawl:
            # Full A-Z exhaustive search across all 26 letters
            for c in "abcdefghijklmnopqrstuvwxyz":
                if is_isaidub:
                    folders_to_search.append(f"{target_base}/tamil-atoz-dubbed-movies/{c}")
                else:
                    folders_to_search.append(f"{target_base}/tamil-movies/{c}/")
        else:
            for c in sorted(candidate_letters):
                if is_isaidub:
                    folders_to_search.append(f"{target_base}/tamil-atoz-dubbed-movies/{c}")
                else:
                    folders_to_search.append(f"{target_base}/tamil-movies/{c}/")

        # Add detected release year folder if specified
        if detected_year:
            if is_isaidub:
                folders_to_search.append(f"{target_base}/tamil-{detected_year}-dubbed-movies/")
            else:
                folders_to_search.append(f"{target_base}/tamil-{detected_year}-movies/")

        # Always check recent release years (2026, 2025)
        if is_isaidub:
            folders_to_search.append(f"{target_base}/tamil-2026-dubbed-movies/")
            folders_to_search.append(f"{target_base}/tamil-2025-dubbed-movies/")
            if is_series(q):
                folders_to_search.append(f"{target_base}/tamil-dubbed-web-series/")
        else:
            folders_to_search.append(f"{target_base}/tamil-2026-movies/")
            folders_to_search.append(f"{target_base}/tamil-2025-movies/")
            if is_series(q):
                folders_to_search.append(f"{target_base}/tamil-web-series-download/")

    # Deduplicate folder URLs while keeping order
    seen_folders = set()
    unique_folders = []
    for f in folders_to_search:
        if f not in seen_folders:
            seen_folders.add(f)
            unique_folders.append(f)

    matched_movies = []
    seen_movie_urls = set()

    def _match(movie):
        title_lower = movie["title"].lower()
        return all(w in title_lower for w in words)

    for folder_url in unique_folders:
        try:
            movies = crawl_category(folder_url, max_pages=max_pages, max_workers=10)
            if on_folder_crawled and movies:
                on_folder_crawled(movies)

            for m in movies:
                if m["url"] not in seen_movie_urls and _match(m):
                    seen_movie_urls.add(m["url"])
                    matched_movies.append(m)
                    if on_movie_found:
                        on_movie_found(m)
        except Exception:
            continue

    return matched_movies


