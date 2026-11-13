"""
domain_manager.py — Centralized Auto-Domain Discovery, Health Check & Self-Healing Resolver.

Supports automatic domain failover, dynamic redirect tracking, and persistent domain configuration
across:
1. 'tamilmv'   — Tamil(Torrents) (Primary default: https://www.1tamilmv.solar/)
2. 'isaidub'   — TamDub (Dubbed MP4) (Primary default: https://isaidub.green/)
3. 'moviesda'  — TamilMov (Direct MP4) (Primary default: https://moviezda.me/)
4. 'toonplay'  — Anime & Cartoons API (Primary default: https://animesalt.streamindia.co.in/api)
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
from pathlib import Path
import re
import time
from urllib.parse import urlparse, urlunparse

import requests

logger = logging.getLogger("domain_manager")

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "domains.json"

DEFAULT_DOMAINS = {
    "tamilmv": {
        "active_host": "www.1tamilmv.io",
        "protocol": "https",
        "label": "Tamil(Torrents)",
        "mirrors": [
            "www.1tamilmv.io",
            "1tamilmv.io",
            "www.1tamilmv.solar",
            "www.1tamilmv.insure",
            "1tamilmv.insure",
            "www.1tamilmv.capital",
            "www.1tamilmv.style",
            "1tamilmv.style",
            "1tamilmv.fi",
            "www.1tamilmv.cz",
            "www.1tamilmv.pm",
            "www.1tamilmv.reisen",
            "www.1tamilmv.cards",
            "www.1tamilmv.wf",
            "www.1tamilmv.yt",
            "www.1tamilmv.lat",
            "www.1tamilmv.biz",
            "www.1tamilmv.click",
        ],
    },
    "isaidub": {
        "active_host": "isaidub.green",
        "protocol": "https",
        "label": "TamDub (Dubbed)",
        "mirrors": [
            "isaidub.green",
            "www.isaidub.video",
            "isaidub.co",
            "isaidub.in",
            "isaidub.com",
            "isaidub.net",
        ],
    },
    "moviesda": {
        "active_host": "moviezda.me",
        "protocol": "https",
        "label": "TamilMov (Direct)",
        "mirrors": [
            "moviezda.me",
            "moviesda.ski",
            "isaimini.com.co",
            "moviesda.net",
            "isaimini.io",
            "isaimini.co",
            "isaiminimob.com",
        ],
    },
    "toonplay": {
        "active_host": "https://animesalt.streamindia.co.in/api",
        "protocol": "https",
        "label": "Anime & Cartoons",
        "mirrors": [
            "https://animesalt.streamindia.co.in/api",
            "https://anime.streamindia.co.in/api",
            "https://toonplay.in/api",
        ],
    },
}

_DOMAINS_STATE = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def is_gateway_or_cdn_host(host_or_url: str) -> bool:
    """Detect if a host/URL belongs to a CDN, file-server, or intermediate download gateway rather than a main site."""
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
        or any(k in h for k in ("dubpage", "dubmv", "dubshare", "uptodub", "onestream", "uptomkv", "moviespage", "downloadpage", "r2.cloudflarestorage.com", "googleusercontent"))
    )


def load_config() -> dict:
    """Load persisted domains configuration from domains.json or populate with defaults."""
    global _DOMAINS_STATE
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    # Merge defaults so any missing fields or new mirrors are preserved
                    merged = dict(DEFAULT_DOMAINS)
                    for k, v in data.items():
                        if k in merged and isinstance(v, dict):
                            merged[k]["active_host"] = v.get("active_host", merged[k]["active_host"])
                            if "mirrors" in v and isinstance(v["mirrors"], list):
                                for m in v["mirrors"]:
                                    if m not in merged[k]["mirrors"]:
                                        merged[k]["mirrors"].append(m)

                    # Always sanitize out hijacked / expired domains
                    if "tamilmv" in merged:
                        merged["tamilmv"]["mirrors"] = [
                            m for m in merged["tamilmv"].get("mirrors", [])
                            if "1tamilmv.win" not in m.lower()
                        ]
                        if "1tamilmv.win" in merged["tamilmv"].get("active_host", "").lower():
                            merged["tamilmv"]["active_host"] = "www.1tamilmv.io"

                    # Sanitize isaidub (never allow CDN download gateways as main browsing host)
                    if "isaidub" in merged:
                        merged["isaidub"]["mirrors"] = [
                            m for m in merged["isaidub"].get("mirrors", [])
                            if not is_gateway_or_cdn_host(m)
                        ]
                        if is_gateway_or_cdn_host(merged["isaidub"].get("active_host", "")) or not merged["isaidub"].get("active_host"):
                            merged["isaidub"]["active_host"] = "isaidub.green"

                    _DOMAINS_STATE = merged
                    return _DOMAINS_STATE
        except Exception as exc:
            logger.warning("Error reading %s, falling back to defaults: %s", CONFIG_FILE, exc)

    _DOMAINS_STATE = dict(DEFAULT_DOMAINS)
    save_config(_DOMAINS_STATE)
    return _DOMAINS_STATE


def save_config(state: dict | None = None) -> bool:
    """Persist current domains state to domains.json."""
    data = state or _DOMAINS_STATE
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as exc:
        logger.error("Failed to write %s: %s", CONFIG_FILE, exc)
        return False


def get_active_host(source: str) -> str:
    """Get currently active host for a source (e.g. 'www.1tamilmv.solar')."""
    src = _normalize_source_key(source)
    if not _DOMAINS_STATE:
        load_config()
    cfg = _DOMAINS_STATE.get(src, DEFAULT_DOMAINS.get(src, {}))
    return cfg.get("active_host", "")


def get_active_base_url(source: str) -> str:
    """Get complete base URL with scheme for a source (e.g. 'https://www.1tamilmv.solar')."""
    host = get_active_host(source)
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    return f"https://{host}".rstrip("/")


def set_active_host(source: str, host: str, persist: bool = True) -> str:
    """
    Set active host for a source, propagate to the corresponding scraper, and persist.
    """
    src = _normalize_source_key(source)
    if not _DOMAINS_STATE:
        load_config()

    clean_host = host.strip()
    if src != "toonplay":
        clean_host = clean_host.replace("https://", "").replace("http://", "").strip("/")
    else:
        clean_host = clean_host.rstrip("/")

    if not clean_host:
        raise ValueError(f"Invalid host for {source}: '{host}'")

    if is_gateway_or_cdn_host(clean_host):
        logger.warning("[Domain Manager] Rejected setting CDN/gateway domain '%s' as active host for %s", clean_host, source)
        return get_active_host(source)

    if src not in _DOMAINS_STATE:
        _DOMAINS_STATE[src] = {"active_host": clean_host, "mirrors": [clean_host]}
    else:
        _DOMAINS_STATE[src]["active_host"] = clean_host
        if clean_host not in _DOMAINS_STATE[src].get("mirrors", []):
            _DOMAINS_STATE[src].setdefault("mirrors", []).insert(0, clean_host)

    # Propagate to individual scraper modules
    _sync_to_scrapers(src, clean_host)

    if persist:
        save_config()

    logger.info("[Domain Manager] Active domain for '%s' updated to: %s", src, clean_host)
    return clean_host


def _sync_to_scrapers(source: str, host: str):
    """Synchronize domain change into scraper modules."""
    src = _normalize_source_key(source)
    try:
        if src == "tamilmv":
            import scraper
            scraper.set_base_host(host)
        elif src == "isaidub":
            from isaimini import scraper as isai_scraper
            isai_scraper.set_isaidub_host(host)
        elif src == "moviesda":
            from isaimini import scraper as isai_scraper
            isai_scraper.set_moviesda_host(host)
        elif src == "toonplay":
            from toonplay import scraper as toon_scraper
            toon_scraper.set_api_host(host)
    except Exception as exc:
        logger.warning("[Domain Manager] Error syncing '%s' to scraper module: %s", src, exc)


def _normalize_source_key(source: str) -> str:
    s = (source or "").strip().lower()
    if s in ("tamilmv", "torrents", "torrent"):
        return "tamilmv"
    if s in ("isaidub", "tamdub", "dubbed"):
        return "isaidub"
    if s in ("moviesda", "tamilmov", "isaimini"):
        return "moviesda"
    if s in ("toonplay", "anime", "toon", "cartoons"):
        return "toonplay"
    return s


def rewrite_url_to_active(raw_url: str, source: str | None = None) -> str:
    """
    Rewrite old/dead domains in scraped topic URLs, download links, or image URLs
    to point to the currently active live domain.
    """
    if not raw_url or not raw_url.strip():
        return ""
    u = raw_url.strip()

    # Never rewrite CDN, file-server, or intermediate download gateways
    if is_gateway_or_cdn_host(u):
        return u

    # Identify source if not provided
    src = _normalize_source_key(source) if source else None
    if not src:
        if "1tamilmv" in u or "tamilmv" in u:
            src = "tamilmv"
        elif "isaidub" in u:
            src = "isaidub"
        elif "moviesda" in u or "isaimini" in u or "moviezda" in u:
            src = "moviesda"
        elif "toonplay" in u or "streamindia" in u:
            src = "toonplay"

    if not src or src == "toonplay":
        return u

    active_host = get_active_host(src)
    if not active_host:
        return u

    parsed = urlparse(u)
    if not parsed.netloc:
        return u

    # Replace host while preserving path, query, and fragment
    new_parsed = parsed._replace(netloc=active_host, scheme="https")
    return urlunparse(new_parsed)


def check_candidate_health(source: str, host_or_url: str, timeout: float = 2.5) -> dict:
    """
    Check if a candidate domain is responsive, alive, and serves valid content.
    Follows redirects and detects if the site moved to a new TLD.
    """
    src = _normalize_source_key(source)
    if is_gateway_or_cdn_host(host_or_url):
        return {
            "online": False,
            "status": "gateway_excluded",
            "initial_host": host_or_url,
            "latency_ms": None,
        }

    target = host_or_url.strip()
    if not target.startswith("http"):
        target = f"https://{target}"

    check_url = target
    if src == "toonplay" and not target.endswith("/home"):
        check_url = f"{target.rstrip('/')}/home"

    start_t = time.time()
    try:
        r = requests.get(check_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        elapsed_ms = int((time.time() - start_t) * 1000)

        # Check if the request was redirected to a new domain
        final_parsed = urlparse(r.url)
        final_host = final_parsed.netloc

        if r.status_code == 200:
            html_low = r.text[:3000].lower() if r.text else ""

            # Validate domain-specific signature
            is_valid = True
            if src == "tamilmv":
                # Reject known squatted / gambling domains
                if "1tamilmv.win" in (final_host or host_or_url).lower():
                    is_valid = False
                elif any(spam in html_low for spam in ("taruhan", "kasino", "judi", "slot", "betting", "poker")):
                    is_valid = False
                else:
                    # Valid TamilMV pages contain forum/topic structures or banger/attachment links
                    is_valid = (
                        ("attachment.php" in html_low or "banger" in html_low or "forum" in html_low or "topic" in html_low)
                        and ("tamilmv" in html_low or "banger" in html_low)
                    )
            elif src == "isaidub":
                is_valid = ("isaidub" in html_low or "tamil" in html_low or "dubbed" in html_low or "mv-content" in html_low)
            elif src == "moviesda":
                is_valid = ("moviesda" in html_low or "isaimini" in html_low or "moviezda" in html_low or "tamil" in html_low)
            elif src == "toonplay":
                is_valid = ("success" in html_low or "data" in html_low or "_fromcache" in html_low)

            if is_valid:
                active_res = host_or_url if src == "toonplay" else (final_host or host_or_url)
                return {
                    "online": True,
                    "status": "online",
                    "status_code": r.status_code,
                    "initial_host": host_or_url,
                    "active_host": active_res,
                    "final_url": r.url,
                    "redirected": bool(final_host and final_host.lower() not in host_or_url.lower()),
                    "latency_ms": elapsed_ms,
                }

        return {
            "online": False,
            "status": f"HTTP {r.status_code}",
            "status_code": r.status_code,
            "initial_host": host_or_url,
            "latency_ms": elapsed_ms,
        }
    except Exception as exc:
        return {
            "online": False,
            "status": "offline",
            "error": type(exc).__name__,
            "initial_host": host_or_url,
            "latency_ms": None,
        }


def auto_resolve_source(source: str, force: bool = False) -> dict:
    """
    Automatically discovers and sets the working live domain for a given source.
    Pings candidate mirrors concurrently; the first healthy responder is selected.
    """
    src = _normalize_source_key(source)
    if not _DOMAINS_STATE:
        load_config()

    cfg = _DOMAINS_STATE.get(src, DEFAULT_DOMAINS.get(src, {}))
    current_host = cfg.get("active_host", "")
    mirrors = list(cfg.get("mirrors", []))

    # Ensure current host is checked first
    if current_host and current_host in mirrors:
        mirrors.remove(current_host)
        mirrors.insert(0, current_host)
    elif current_host:
        mirrors.insert(0, current_host)

    # If not force, test current host first quickly
    if not force and current_host:
        res = check_candidate_health(src, current_host, timeout=2.0)
        if res.get("online"):
            # Check if current host redirected to a new domain
            final_h = res.get("active_host")
            if final_h and final_h != current_host:
                set_active_host(src, final_h)
                res["updated"] = True
            return res

    # Check mirrors concurrently
    best_candidate = None
    with ThreadPoolExecutor(max_workers=min(len(mirrors), 6)) as executor:
        futures = {executor.submit(check_candidate_health, src, m, 2.5): m for m in mirrors}
        for future in as_completed(futures):
            try:
                res = future.result()
                if res.get("online"):
                    best_candidate = res
                    break
            except Exception:
                pass

    if best_candidate:
        winning_host = best_candidate.get("active_host") or best_candidate.get("initial_host")
        if winning_host:
            set_active_host(src, winning_host)
            best_candidate["updated"] = True
            return best_candidate

    return {
        "online": False,
        "source": src,
        "current_host": current_host,
        "status": "all mirrors offline or timed out",
    }


def auto_resolve_all() -> dict:
    """Auto-resolve all 4 media sources in parallel."""
    results = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(auto_resolve_source, src): src
            for src in ("tamilmv", "isaidub", "moviesda", "toonplay")
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                results[src] = future.result()
            except Exception as exc:
                results[src] = {"error": str(exc), "online": False}
    return results


def intercept_redirect_and_update(source: str, resp: requests.Response) -> str | None:
    """
    Hook to inspect any HTTP response. If the site redirected to a new domain,
    update the active domain dynamically and persist it.
    """
    if not resp or not hasattr(resp, "url"):
        return None
    src = _normalize_source_key(source)
    try:
        final_parsed = urlparse(resp.url)
        final_host = final_parsed.netloc.lower()
        if not final_host or is_gateway_or_cdn_host(final_host):
            return None

        current = get_active_host(src).lower()
        if current and final_host != current:
            # Domain shifted via redirect! Update state immediately.
            clean = set_active_host(src, final_host)
            logger.info("[Domain Manager] Automatically detected live redirect: %s -> %s", current, clean)
            return clean
    except Exception as exc:
        logger.debug("[Domain Manager] Error in intercept_redirect: %s", exc)
    return None


def init_domains():
    """Initialize domains state on app boot and sync to scrapers."""
    cfg = load_config()
    for src in ("tamilmv", "isaidub", "moviesda", "toonplay"):
        host = cfg.get(src, {}).get("active_host")
        if host:
            _sync_to_scrapers(src, host)
    return cfg
