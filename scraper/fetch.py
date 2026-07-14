"""Rate-limited HTTP fetcher with a simple on-disk cache for BetExplorer pages."""
import hashlib
import time
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "html_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

_last_request_at = 0.0
MIN_DELAY_SECONDS = 2.5
MAX_RETRIES = 4


def _cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.html"


def get_html(url: str, use_cache: bool = True) -> str:
    """Fetch a page, respecting a minimum delay between live requests.

    Cached copies (under data/html_cache) are reused across runs so repeated
    scraper invocations during development don't hammer the source site.

    On a 429 (rate limited), backs off and retries a few times instead of
    failing the whole scrape outright -- CI runs share IP ranges with other
    GitHub Actions jobs, so occasional rate limiting is expected, not just a
    symptom of scraping too aggressively ourselves.
    """
    global _last_request_at

    cache_file = _cache_path(url)
    if use_cache and cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    for attempt in range(MAX_RETRIES):
        elapsed = time.monotonic() - _last_request_at
        if elapsed < MIN_DELAY_SECONDS:
            time.sleep(MIN_DELAY_SECONDS - elapsed)

        resp = _session.get(url, timeout=20)
        _last_request_at = time.monotonic()

        if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
            backoff = 10 * (attempt + 1)
            print(f"429 rate limited, retrying in {backoff}s: {url}")
            time.sleep(backoff)
            continue

        resp.raise_for_status()
        cache_file.write_text(resp.text, encoding="utf-8")
        return resp.text

    raise RuntimeError("unreachable")  # loop always returns or raises above
