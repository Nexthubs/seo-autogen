"""URL deduplication (SEO-AUTO-DEV-SPEC.md section 14).

URL dedup must never be delegated to the LLM. Programmatic rules:

1. lowercase hostname
2. remove fragment
3. remove default port (80/443)
4. remove trailing ``/`` where safe
5. remove tracking query params
6. sort remaining query params

Stored identifiers:

- ``normalized_url``
- ``url_hash``  = SHA256(normalized_url)
- ``content_hash`` = SHA256(whitespace-normalized content)
"""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

#: Tracking / redirect params that must be dropped (spec section 14).
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "ref",
    "source",
}

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_SCHEME_PREFIX = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Normalize a URL per spec section 14. Deterministic."""
    url = url.strip()
    if not url:
        raise ValueError("empty url")

    if not _SCHEME_PREFIX.match(url):
        url = "https://" + url

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme!r}")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError(f"no hostname in url: {url!r}")

    netloc = hostname
    if parsed.port is not None and str(parsed.port) != _DEFAULT_PORTS[scheme]:
        netloc = f"{hostname}:{parsed.port}"

    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Drop tracking params, keep the rest, sort for canonical order.
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in TRACKING_PARAMS:
            continue
        kept.append((key, value))
    kept.sort()
    query = urlencode(kept)

    # Fragment is intentionally dropped (spec rule 2).
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")


def url_hash(url: str) -> str:
    """SHA256 of the normalized URL (spec section 14: UNIQUE(url_hash))."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def content_hash(content: str) -> str:
    """SHA256 of whitespace-normalized content (spec section 14.1).

    Used to detect duplicate pages served at different URLs.
    """
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
