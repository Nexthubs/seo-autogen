"""Secret redaction (SEO-AUTO-DEV-SPEC.md section 60, audit M10).

Spec 60: tokens / keys must never appear in logs, the Web UI or any
persisted field. Audit M10 found the old single-string regex pass
missed the common shapes:

* serialized JSON — ``"api_key": "sk-..."`` (regex only matched
  ``api_key=...`` / ``api_key: ...`` without the JSON quotes);
* URL query parameters — ``https://host/pull?key=sk-...``;
* Bearer tokens echoed in response bodies;
* structured provider payloads (dicts/lists) passed via
  ``extra=`` to the log handler.

This module is the ONE redaction home. ``redact_value`` walks any
JSON-shaped structure recursively:

1. **field names** — a dict key whose name looks secret-ish
   (``api_key``, ``password``, ``authorization``, ``secret`` …)
   replaces the whole value with :data:`REDACTED`;
2. **strings** — each string goes through :func:`redact`, which redacts
   Bearer tokens, ``password=/password: ...`` style pairs, JSON quoted
   forms of the same, and sensitive **URL query parameters**.

Persisted error fields (``job.error_message`` / ``job.error_raw``) and
log records both pass through this module so the same guarantee holds
in the database and on disk.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: The replacement used for every redacted secret value.
REDACTED = "<redacted>"

# ----------------------------------------------------------------------
# field-name based redaction (structured payloads)
# ----------------------------------------------------------------------
_SECRET_KEY_TOKENS = (
    "key",
    "password",
    "passwd",
    "token",
    "secret",
    "credential",
    "authorization",
    "bearer",
)

# keys that are sensitive by NAME (case-insensitive substring match)
def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    name = key.strip().lower().replace("-", "_").replace(" ", "_")
    if not name:
        return False
    if name in ("keys", "token_count", "tokens", "api_keys_available"):
        return False
    for token in _SECRET_KEY_TOKENS:
        if token in name:
            return True
    return False


# ----------------------------------------------------------------------
# string redaction
# ----------------------------------------------------------------------
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+")
# ``password: x`` / ``api_key = x`` — including the JSON quoted form
# ``"api_key": "x"`` (the separator group tolerates surrounding quotes).
_KV_PAIRS = re.compile(
    r'''(?i)("?(?:api[_-]?key|access[_-]?key|secret|password|passwd|token|session)"?\s*[:=]\s*"?)([^"&\s,;}{)\]]+)'''
)
# sensitive URL query parameter values (?key=... / &token=...)
_URL_SECRET_QUERY_KEYS = (
    "key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "password",
    "secret",
    "authorization",
    "bearer",
)


def _redact_query(query: str) -> str:
    """Redact the values of sensitive query parameters in a query string.

    The query is split on ``&`` / ``=`` and only the *value* of a sensitive
    parameter is replaced, so the surrounding text (and the percent-encoding
    of every other parameter) is preserved byte-for-byte.
    """
    if not query:
        return query
    parts = query.split("&")
    changed = False
    out: list[str] = []
    for part in parts:
        name, sep, value = part.partition("=")
        if sep and value and any(s in name.lower() for s in _URL_SECRET_QUERY_KEYS):
            out.append(f"{name}={REDACTED}")
            changed = True
        else:
            out.append(part)
    return "&".join(out) if changed else query


def _redact_url(value: str) -> str:
    """If the string (or a substring of it) is a URL, redact its query."""
    parts = urlsplit(value)
    if not parts.query:
        return value
    new_query = _redact_query(parts.query)
    if new_query == parts.query:
        return value
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def redact(value: str) -> str:
    """Redact every supported secret shape in one string (M10)."""
    if not value:
        return value
    out = _BEARER.sub(r"\1" + REDACTED, value)
    out = _KV_PAIRS.sub(r"\1" + REDACTED, out)
    out = _redact_url(out)
    return out


# ----------------------------------------------------------------------
# recursive structured redaction
# ----------------------------------------------------------------------
def redact_value(value: Any) -> Any:
    """Recursively redact secrets from any JSON-shaped structure.

    Dicts are rebuilt with secret-named keys emptied; lists/tuples are
    walked; strings go through :func:`redact`; other scalars pass
    through unchanged.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact_value(v)
        return out
    if isinstance(value, (list, tuple, set)):
        return type(value)(redact_value(item) for item in value)  # type: ignore[call-arg]
    if isinstance(value, str):
        return redact(value)
    return value


def redact_dict(value: dict) -> dict:
    """``redact_value`` specialized to dict input (always returns a dict)."""
    out = redact_value(value)
    return out if isinstance(out, dict) else {}
