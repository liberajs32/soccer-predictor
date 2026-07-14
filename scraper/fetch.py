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
MIN_DELAY_SECONDS = 1.5


def _cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.html"


def get_html(url: str, use_cache: bool = True) -> str:
    """Fetch a page, respecting a minimum delay between live requests.

    Cached copies (under data/html_cache) are reused across runs so repeated
    scraper invocations during development don't hammer the source site.
    """
    global _last_request_at

    cache_file = _cache_path(url)
    if use_cache and cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_DELAY_SECONDS:
        time.sleep(MIN_DELAY_SECONDS - elapsed)

    resp = _session.get(url, timeout=20)
    resp.raise_for_status()
    _last_request_at = time.monotonic()

    cache_file.write_text(resp.text, encoding="utf-8")
    return resp.text
