"""URL normalization helpers shared by crawl and evidence binding."""

from __future__ import annotations

from urllib.parse import unquote_plus, urlsplit, urlunsplit


NON_CONTENT_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "ref",
    "ref_",
    "ref_cta",
    "ref_loc",
    "ref_page",
    "ref_src",
    "referer",
    "referrer",
    "scid",
    "spm",
    "vero_id",
    "_hsenc",
    "_hsmi",
    "hl",
    "lang",
    "language",
    "locale",
}

NON_CONTENT_QUERY_PREFIXES = ("utm_",)


def is_non_content_query_key(key: str) -> bool:
    normalized = str(key or "").strip().casefold()
    return normalized in NON_CONTENT_QUERY_KEYS or any(
        normalized.startswith(prefix) for prefix in NON_CONTENT_QUERY_PREFIXES
    )


def normalize_url_for_evidence(url: str) -> str:
    """Normalize URL identity without merging content-bearing query resources.

    The audit sees plenty of navigation links that differ only by tracking or
    locale/presentation parameters. Those should not break evidence binding or
    cause duplicate crawl work, while content parameters such as ``page`` or
    ``q`` remain intact.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.split("#", 1)[0]

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return raw.split("#", 1)[0]

    port = parsed.port
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if not port or default_port else f"{host}:{port}"

    kept_query = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        raw_key = part.split("=", 1)[0]
        try:
            key = unquote_plus(raw_key)
        except ValueError:
            key = raw_key
        if not is_non_content_query_key(key):
            kept_query.append(part)
    query = "&".join(kept_query)
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))
